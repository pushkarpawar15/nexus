# NEXUS

NEXUS is a human-supervised engineering workflow. Connect repositories through a GitHub App or a fine-grained PAT in the UI. PATs are held in backend memory for local development; the example configuration starts disconnected and keeps coding and validation disabled.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer and npm
- Firebase CLI and Java 21 or newer to run the local Auth and Firestore emulators

Task analysis and coding expose per-task model selectors, with Gemini 3.8 Flash recommended by default. Analysis can use a backend-only `GEMINI_API_KEY` locally or Vertex AI. Coding is configured to use Vertex AI with Application Default Credentials, `GOOGLE_CLOUD_PROJECT`, and `CODING_MODEL_LOCATION=global`; it does not use the API key. The Cloud Run service identity needs Vertex AI access.

## Local setup

From the repository root, copy the examples:

```sh
test -e .env || cp .env.example .env
test -e frontend/.env.local || cp frontend/.env.example frontend/.env.local
```

The examples use `demo-nexus` consistently for the backend and frontend Firebase project IDs. They select Firestore, configure the local emulators, start with no configured target repositories, and leave `NEXUS_CODING_ENABLED` and `NEXUS_VALIDATION_ENABLED` set to `false`. Do not put GitHub or Gemini credentials in frontend variables.

For a hosted setup that runs task coding and validation entirely on GCP, follow [the Cloud Run and Vertex AI deployment guide](docs/cloud-execution.md). It configures a Cloud Run Job worker, Cloud Build validation, Artifact Registry, service identities, and GitHub credentials stored in Secret Manager.

Start the emulators:

```sh
firebase emulators:start --only auth,firestore --project demo-nexus
```

In a second terminal, install and start the API:

```sh
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
uvicorn app.main:create_app --factory --reload --port 8000
```

In a third terminal, install and start the browser app:

```sh
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api`, `/health`, and `/ready` to `127.0.0.1:8000`. `GET /health` checks process liveness; `GET /api/ready` checks the configured store and returns 503 if Firestore is unavailable. API task and repository reads require a Firebase ID token.

The app includes task and workflow views, GitHub App repository connection, attachment upload/download/removal, Gemini analysis and structured coding proposals, isolated validation, approval, and PR publication. Files are stored privately and passed to Vertex AI as multimodal input. See [GitHub and task attachments setup](docs/github-and-attachments.md) before enabling the local worker. The example configuration intentionally keeps coding and validation disabled until a local workspace and validator images are configured.

## Configuration boundaries

- `NEXUS_STORAGE` is required. `memory` is permitted only with `NEXUS_ENV=testing`; development and production use Firestore.
- Production requires Firestore, Google and Firebase project IDs, and either an approved repository map or a configured GitHub App. Emulator hosts and local execution flags are rejected in production.
- The Cloud Run service identity needs `roles/firebaseauth.viewer` in the Firebase project because production ID-token verification checks revocation status. Enable the Identity Toolkit API if it is not already enabled. Verification dependency errors are logged with their exception type to distinguish a missing permission/API from an invalid token.
- GitHub App access is server-side. Repository choices come from repositories selected during the GitHub installation.
- `WORKSPACE_ROOT` must be outside both the NEXUS checkout and the target checkout when local execution is enabled.
- Keep GitHub and Gemini credentials backend-only. Use ADC locally and a narrowly scoped service identity when deployed.

## Checks

Backend checks (run with Python 3.11 or newer):

```sh
cd backend
python -m pytest
ruff check .
ruff format --check .
```

Frontend checks:

```sh
cd frontend
npm ci
npm run lint
npm run build
npm test
npm run format:check
```

The backend test suite covers policy enforcement, authenticated API behavior, and a full local pipeline using a temporary Git repository and fake model/validator boundaries. The frontend suite checks the app shell and formatting helpers. Emulator-backed Auth/Firestore verification is described in `docs/verification.md`.

## Hosted boundary

`backend/Dockerfile` and Firebase Hosting configuration describe the deployment shape. Hosted coding and validation are supported through the Cloud Run Job worker and Cloud Build; follow the [deployment guide](docs/cloud-execution.md) before enabling both execution flags in production. Local execution uses an in-process worker and requires a configured workspace and validator images. Production attachments require a private GCS bucket. A task must not be reported as completed until its isolated worker and required validation gates have finished.
