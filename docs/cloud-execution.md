# Cloud only execution on GCP

NEXUS can run coding tasks without a developer machine, Docker daemon, or local workspace. The API dispatches each task to a Cloud Run Job. That worker clones the selected GitHub repository, calls the configured coding model through Vertex AI, and asks Cloud Build to run the fixed PayFlow validation profile. Firestore holds task and activity state, and GCS holds attachments plus a temporary validated workspace when a PR needs human approval or a retry.

The API and worker run as separate Cloud Run identities. Do not put GitHub credentials or model credentials in Firebase Hosting or `VITE_*` settings. Use a GitHub App installation for user/repository scoped access, or configure a server managed fine grained PAT in Secret Manager for an explicitly allowlisted repository. The PAT connection entered in the UI is process local; for Cloud Run, use Secret Manager and configure `APPROVED_REPOSITORIES` so the worker can access the same fixed repository after instances restart.

## 1. Choose the GCP project and region

Use the Firebase project ID if Firebase Auth and Firestore are already enabled in that project. The commands below use `asia-south1` (Mumbai); Cloud Run Jobs, Cloud Build private pools, and Artifact Registry should use a compatible region. Set `PROJECT_ID` to your actual project ID:

```sh
gcloud config set project PROJECT_ID
gcloud config set run/region asia-south1
REGION=asia-south1
PROJECT_ID="$(gcloud config get-value project)"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
```

Confirm billing is active before enabling services. Cloud Run, Cloud Build private pools, Artifact Registry, Vertex AI, Firestore, Cloud Logging, and Cloud Storage can consume the free credits.

## 2. Enable APIs and create the image repository

```sh
gcloud services enable \
  run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  compute.googleapis.com aiplatform.googleapis.com firestore.googleapis.com \
  storage.googleapis.com secretmanager.googleapis.com

gcloud artifacts repositories create nexus \
  --repository-format=docker --location="$REGION" \
  --description="NEXUS API, worker, and validation images"
```

The project must already have a Firestore database and Firebase Authentication configured. Add the Firebase Hosting domain to Firebase Authentication's authorized domains and configure the Google sign-in provider in Firebase Console.

## 3. Create separate service identities

```sh
gcloud iam service-accounts create nexus-api \
  --display-name="NEXUS API"
gcloud iam service-accounts create nexus-worker \
  --display-name="NEXUS task worker"
gcloud iam service-accounts create nexus-build \
  --display-name="NEXUS validation build steps"

API_SA="nexus-api@$PROJECT_ID.iam.gserviceaccount.com"
WORKER_SA="nexus-worker@$PROJECT_ID.iam.gserviceaccount.com"
BUILD_SA="nexus-build@$PROJECT_ID.iam.gserviceaccount.com"
```

Grant the API identity Firestore access, object read/write access to the attachments bucket, and permission to start/read/cancel executions of the one NEXUS Cloud Run Job. A project-level `roles/run.developer` is a quick start; replace it with a custom role limited to `run.jobs.runWithOverrides`, `run.executions.get`, and `run.executions.cancel` once deployed.

The worker identity needs Firestore read/write, Vertex AI User (`roles/aiplatform.user`), access to the private attachments bucket, permission to create Cloud Builds (`roles/cloudbuild.builds.editor`), Cloud Logging Viewer (`roles/logging.viewer`) to read failed validation diagnostics for automated repair, and `roles/iam.serviceAccountUser` on `nexus-build`. It also needs temporary bucket creation and bucket-IAM management for per-validation source buckets; grant `roles/storage.admin` only to this worker identity, not the API. The worker account needs access to the GitHub App secrets described below.

The build-step identity needs `roles/logging.logWriter` and `roles/artifactregistry.reader`. For each validation, both this identity and the Cloud Build service agent receive `roles/storage.objectViewer` on that build's temporary source bucket so Cloud Build can stage the source before starting the steps. The bucket is deleted after the build; this does not grant access to other active task sources. Cloud Build's service agent must be enabled for the project and allowed to use the selected private worker pool.

After the private pool exists, grant the worker permission to use that pool. Grant the API bucket access on the existing private attachments bucket:

```sh
gcloud builds worker-pools add-iam-policy-binding POOL_NAME --region="$REGION" \
  --member="serviceAccount:$WORKER_SA" --role=roles/cloudbuild.workerPoolUser
gcloud storage buckets add-iam-policy-binding "gs://YOUR_PRIVATE_BUCKET" \
  --member="serviceAccount:$API_SA" --role=roles/storage.objectAdmin
```

Example baseline grants (review before running; these are project-level grants):

```sh
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$API_SA" --role=roles/datastore.user
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$API_SA" --role=roles/run.developer
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/datastore.user
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/aiplatform.user
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/cloudbuild.builds.editor
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/logging.viewer
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WORKER_SA" --role=roles/storage.admin
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILD_SA" --role=roles/logging.logWriter
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILD_SA" --role=roles/artifactregistry.reader
gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" \
  --member="serviceAccount:$WORKER_SA" --role=roles/iam.serviceAccountUser
```

Scope Firestore, bucket, Secret Manager, and `actAs` grants to the smallest resources possible. For production, replace broad project-level roles with custom roles and bucket-level bindings.

## 4. Create the private Cloud Build worker pool

The validator uses prebuilt, offline dependency images. First make the image builder available in Cloud Build by cloning the target PayFlow checkout in your secured build environment, then run the NEXUS-owned `backend/validation_images/build_images.py` script against it. Build and push the images to Artifact Registry. Only manifests enter the validator image build context; do not put repository secrets in those manifests.

```sh
PYTHON_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/nexus/payflow-python-validator:1"
NODE_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/nexus/payflow-node-validator:1"
```

Create a private pool in the same region. Private pools have a separate cost; check the current GCP pricing and your remaining credits first. Set its egress option to `NO_PUBLIC_EGRESS` and provide private access to Google APIs, Artifact Registry, and Cloud Logging. Cloud Build pools otherwise allow public egress by default. The validation steps use offline dependency images and do not need GitHub credentials. Do not add Cloud NAT or a public proxy for this pool.

Create a pool from Cloud Console (Cloud Build → Worker pools), or use `gcloud builds worker-pools create` with a private pool config that sets `egressOption: NO_PUBLIC_EGRESS`. Record its full name:

```text
projects/PROJECT_ID/locations/asia-south1/workerPools/POOL_NAME
```

## 5. Configure repository credentials in Secret Manager

Choose one of these options:

**GitHub App (recommended for multiple users or repositories).** Install your GitHub App on the repositories NEXUS should be allowed to modify. It needs repository Contents read/write and Pull requests read/write permissions. Save the App ID, slug, PEM private key, state secret, and optional webhook secret in Secret Manager. The API needs the App credentials for installation/callback handling; the worker needs the App ID and private key to mint short lived installation tokens.

**Server managed PAT (one allowlisted repository).** Create a fine grained PAT with access to only the repository NEXUS should modify. Grant Contents read/write and Pull requests read/write; Metadata read is required by GitHub. Store it in Secret Manager, never in Firebase Hosting. Find the repository ID with `gh api repos/OWNER/REPO --jq .id`, then configure this mapping on both Cloud Run resources so it matches the repository key used in Firestore:

```json
{"github-REPOSITORY_ID":{"owner":"OWNER","repo":"REPO","label":"OWNER/REPO","base_branch":"main","github_repository_id":REPOSITORY_ID}}
```

With a PAT, the backend uses this one server managed credential for the allowlisted repository. The interactive PAT pasted into a UI session is not persisted or shared with the worker. Set the JSON mapping above as `APPROVED_REPOSITORIES` on both the API and worker, and bind `GITHUB_TOKEN` from Secret Manager on both resources. For example, set the repository map in Cloud Console's **Variables & Secrets** page, or use `--update-env-vars '^@^APPROVED_REPOSITORIES={"github-123456":{"owner":"OWNER","repo":"REPO","label":"OWNER/REPO","base_branch":"main","github_repository_id":123456}}@'` (replace the ID and names) on each resource.

```sh
printf %s 'APP_ID' | gcloud secrets create nexus-github-app-id --data-file=-
printf %s 'APP_SLUG' | gcloud secrets create nexus-github-app-slug --data-file=-
gcloud secrets create nexus-github-app-private-key --data-file=/secure/path/app-private-key.pem
openssl rand -hex 32 | gcloud secrets create nexus-github-app-state-secret --data-file=-

gcloud secrets add-iam-policy-binding nexus-github-app-id --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-slug --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-private-key --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-state-secret --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-id --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-slug --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-app-private-key --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor
```

For the PAT option, run this separately (instead of creating the App secrets):

```sh
read -rsp 'Fine grained PAT: ' GH_PAT; printf '\n'
printf %s "$GH_PAT" | gcloud secrets create nexus-github-token --data-file=-
unset GH_PAT
gcloud secrets add-iam-policy-binding nexus-github-token --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding nexus-github-token --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor
```

For GitHub App, set a strong, separate `GITHUB_APP_WEBHOOK_SECRET` only if you configure webhooks. Keep the state secret stable between API revisions so pending installation callbacks remain verifiable. Bind `GITHUB_TOKEN=nexus-github-token:latest` as a secret on both the API and worker only when using the PAT option. Do not configure a broad classic PAT.

## 6. Build and deploy the API and worker images

From the NEXUS repository root, submit the backend source to Cloud Build. The API and worker use separate Dockerfiles:

```sh
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/nexus/nexus-api:cloud"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/nexus/nexus-worker:cloud"

gcloud builds submit backend --tag "$API_IMAGE"
gcloud builds submit backend --config=backend/cloudbuild.worker.yaml \
  --substitutions="_IMAGE=$WORKER_IMAGE"
```

The worker job must share the backend's Firestore database, Firebase project ID, repository credentials, attachments bucket, and Vertex AI project. Set the following runtime environment on both the API and worker:

```text
NEXUS_ENV=production
NEXUS_STORAGE=firestore
GOOGLE_CLOUD_PROJECT=PROJECT_ID
FIREBASE_PROJECT_ID=PROJECT_ID
GOOGLE_CLOUD_LOCATION=asia-south1
GEMINI_ANALYSIS_MODEL=<supported Vertex model ID>
GEMINI_CODING_MODEL=<supported Vertex model ID>
CODING_MODEL_LOCATION=global
CODING_MODEL_TIMEOUT_SECONDS=180
ATTACHMENTS_BUCKET=<private GCS bucket already used by NEXUS>
NEXUS_EXECUTION_BACKEND=cloud_run_jobs
NEXUS_CODING_ENABLED=true
NEXUS_VALIDATION_ENABLED=true
CLOUD_RUN_EXECUTION_JOB=nexus-execution-worker
CLOUD_RUN_EXECUTION_REGION=asia-south1
CLOUD_BUILD_VALIDATION_LOCATION=asia-south1
CLOUD_BUILD_VALIDATION_SERVICE_ACCOUNT=nexus-build@PROJECT_ID.iam.gserviceaccount.com
CLOUD_BUILD_SERVICE_AGENT=service-PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com
CLOUD_BUILD_WORKER_POOL=projects/PROJECT_ID/locations/asia-south1/workerPools/POOL_NAME
VALIDATION_PYTHON_IMAGE=asia-south1-docker.pkg.dev/PROJECT_ID/nexus/payflow-python-validator:1
VALIDATION_NODE_IMAGE=asia-south1-docker.pkg.dev/PROJECT_ID/nexus/payflow-node-validator:1
```

Set `CORS_ORIGINS` to the Firebase Hosting URL, and set the GitHub App fields through Secret Manager. Set `NEXUS_AI_PERMISSIONS` to the permissions your team intends to allow. Keep model IDs explicit and verify that the chosen models are available in Vertex AI for the project and location.

For AI and hybrid task execution, set `NEXUS_AI_PERMISSIONS` on both resources to the intended set, such as `ai_execute,ai_validate,security_review`. You can omit permissions your workflow should not grant. When using `gcloud --set-env-vars`, pass comma-containing values with a custom delimiter or set them in Cloud Console.

Create the job first. Replace `PROJECT_ID` and secret names in the command:

```sh
gcloud run jobs create nexus-execution-worker \
  --image "$WORKER_IMAGE" --region "$REGION" --service-account "$WORKER_SA" \
  --task-timeout 2h --max-retries 0 --tasks 1 --cpu 2 --memory 4Gi \
  --set-env-vars NEXUS_ENV=production,NEXUS_STORAGE=firestore,GOOGLE_CLOUD_PROJECT="$PROJECT_ID",FIREBASE_PROJECT_ID="$PROJECT_ID",GOOGLE_CLOUD_LOCATION="$REGION",NEXUS_EXECUTION_BACKEND=cloud_run_jobs,NEXUS_CODING_ENABLED=true,NEXUS_VALIDATION_ENABLED=true,CLOUD_BUILD_VALIDATION_LOCATION="$REGION",CLOUD_BUILD_VALIDATION_SERVICE_ACCOUNT="$BUILD_SA",CLOUD_BUILD_SERVICE_AGENT="service-$PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com",CLOUD_BUILD_WORKER_POOL="projects/$PROJECT_ID/locations/$REGION/workerPools/POOL_NAME",ATTACHMENTS_BUCKET=YOUR_PRIVATE_BUCKET,WORKSPACE_ROOT=/tmp/nexus-workspaces \
  --set-secrets GITHUB_APP_ID=nexus-github-app-id:latest,GITHUB_APP_SLUG=nexus-github-app-slug:latest,GITHUB_APP_PRIVATE_KEY=nexus-github-app-private-key:latest,GITHUB_APP_STATE_SECRET=nexus-github-app-state-secret:latest
```

Then deploy the API as a Cloud Run service using the same production settings, but use `API_SA`, include `CORS_ORIGINS`, and grant it access to the GitHub App secrets plus the private bucket. Set Vertex model env vars on both resources. Deploy the API with `--allow-unauthenticated` only because Firebase ID-token checks protect API routes; keep admin endpoints behind application authorization. Do not set `WORKSPACE_ROOT` on the API.

For the PAT option, omit the `GITHUB_APP_*` bindings in the job and API examples and bind `GITHUB_TOKEN=nexus-github-token:latest` instead. Set `APPROVED_REPOSITORIES` on both resources using the repository mapping above. Keep the actual token in Secret Manager; never put it in an env-vars file or a shell command argument.

Example API deploy skeleton (add the model IDs, frontend URL, exact CORS origin, and storage bucket for your project):

```sh
gcloud run deploy nexus-api --image "$API_IMAGE" --region "$REGION" \
  --service-account "$API_SA" --allow-unauthenticated --min 0 --max 2 \
  --cpu 1 --memory 1Gi --timeout 300 \
  --set-env-vars NEXUS_ENV=production,NEXUS_STORAGE=firestore,GOOGLE_CLOUD_PROJECT="$PROJECT_ID",FIREBASE_PROJECT_ID="$PROJECT_ID",GOOGLE_CLOUD_LOCATION="$REGION",NEXUS_EXECUTION_BACKEND=cloud_run_jobs,NEXUS_CODING_ENABLED=true,NEXUS_VALIDATION_ENABLED=true,CLOUD_RUN_EXECUTION_JOB=nexus-execution-worker,CLOUD_RUN_EXECUTION_REGION="$REGION",CLOUD_BUILD_VALIDATION_LOCATION="$REGION",CLOUD_BUILD_VALIDATION_SERVICE_ACCOUNT="$BUILD_SA",CLOUD_BUILD_SERVICE_AGENT="service-$PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com",CLOUD_BUILD_WORKER_POOL="projects/$PROJECT_ID/locations/$REGION/workerPools/POOL_NAME",ATTACHMENTS_BUCKET=YOUR_PRIVATE_BUCKET,CORS_ORIGINS=https://YOUR_SITE.web.app,FRONTEND_URL=https://YOUR_SITE.web.app \
  --set-secrets GITHUB_APP_ID=nexus-github-app-id:latest,GITHUB_APP_SLUG=nexus-github-app-slug:latest,GITHUB_APP_PRIVATE_KEY=nexus-github-app-private-key:latest,GITHUB_APP_STATE_SECRET=nexus-github-app-state-secret:latest
```

## 7. Configure the hosted UI and verify

In Firebase Hosting build settings, set `VITE_API_BASE_URL` to the deployed Cloud Run API URL. Rebuild and redeploy the frontend after changing Vite environment variables; they are embedded at build time. Do not use `localhost` or an emulator URL in the hosted build.

Connect the GitHub App installation in NEXUS, select an installed repository, analyze a simple task, then run it. The task activity should show the remote workspace, coding stage, Cloud Build validation result, security review, and PR creation/approval state. Cloud Build logs are linked from the validation summary; execution logs are available from the Cloud Run Job execution in Cloud Console. Stopping a task cancels its Cloud Run Job execution and requests cancellation of any active Cloud Build.

## What runs where

| Component | GCP service | Persistent data |
| --- | --- | --- |
| API and Firebase auth verification | Cloud Run service | Firestore; private GCS attachments |
| Coding worker | One Cloud Run Job execution per task | Firestore activity; repository credentials from Secret Manager |
| Isolated validation | Cloud Build private pool | Logs in Cloud Logging; per-build source bucket removed after completion |
| Approved patch awaiting PR | Private GCS object | Deleted after publication only by a future cleanup policy |
| Models | Vertex AI | No local model server or API key required |

Add a GCS lifecycle rule for `execution-workspaces/` as a safety net after choosing a retention period. This implementation deletes the per-validation source bucket after each build; failed cleanup is logged and should be monitored. Cloud billing and GitHub App installation remain account setup tasks; this guide does not create resources automatically.
