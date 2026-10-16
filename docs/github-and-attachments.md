# GitHub connection, attachments, and local coding

NEXUS connects repositories through either a GitHub App or a fine-grained personal access token (PAT). For a single repository, the PAT flow does not require registering a GitHub App. After a patch passes the configured validation and security gates, NEXUS opens a PR automatically when human approval is not required; approval-required work opens the PR after an administrator approves the patch. The task page keeps a manual retry action for transient GitHub failures.

## Add repositories with fine-grained PATs

1. On GitHub, open **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Choose the repository owner as the resource owner and select only the repository NEXUS should use. Repeat this flow to add more repositories.
3. Under repository permissions, set **Contents** and **Pull requests** to **Read and write**. Leave other permissions disabled.
4. Set a suitable expiration and generate the token. Copy it while GitHub shows it.
5. In NEXUS, open **Repositories**. Enter `owner/repository` and paste the token into **Add a repository with a fine-grained token**.

NEXUS verifies the token identity, repository identity, and write permission with GitHub before connecting. The token is sent to the authenticated backend and stored in Google Secret Manager; only its Secret Manager resource name and non-secret repository metadata are stored in Firestore. The token is never returned to the browser or written to logs. Do not put it in a `VITE_` variable or commit it to a file.

PAT connections belong to the Firebase user who added them. Repositories and their task history are visible only to users who have that repository connected (or have access through a GitHub App/configured repository). Removing a PAT repository first shows the number of pending tasks. Confirming removal revokes that user's saved credential and hides the repository's tasks from that user without deleting task, execution, activity, or pull-request records. Reconnecting the same GitHub repository restores visibility because task records use its stable GitHub repository ID.

## Configure repository validation

On a connected PAT repository card, an operator can select **NEXUS monorepo (backend + frontend)** when the checkout has `backend/` and `frontend/` directories. The profile runs the fixed NEXUS checks; it does not allow custom shell commands. The selection is persisted with the connected repository and is loaded by coding workers. Choose **Not configured** to clear it.

Without a configured profile, all validation checks are recorded as **SKIPPED** with the same explanation. The execution stops in a blocked state so validation, security review, and PR publication are not treated as successful. Configure a profile and retry the task to run checks. The built-in PayFlow monorepo target retains its existing default profile.

GitHub can require organization approval or SSO authorization before an organization token works. If verification fails, confirm the token is not expired, its resource owner and repository are correct, and both required permissions are enabled.

## 1. Register a GitHub App

Create an app under the GitHub account that will own it. Configure:

- Repository permissions: **Contents: Read and write**, **Pull requests: Read and write**. Metadata read access is implicit.
- Setup URL: `https://YOUR_PUBLIC_API_HOST/api/github/setup`.
- Webhook URL: `https://YOUR_PUBLIC_API_HOST/api/github/webhook`.
- Webhook secret: generate a unique value and subscribe to `installation` and `installation_repositories` events.
- Keep the app private while developing.

GitHub must reach the setup URL. For local development, expose port 8000 with a temporary HTTPS tunnel and use its URL for the setup and webhook URLs. Keep the Vite app at `http://localhost:5173`.

## 2. Configure backend credentials

Copy the GitHub App slug, App ID, and downloaded private key into the root `.env`. Generate the signed-state secret with:

```bash
openssl rand -hex 32
```

Set these backend-only values:

```dotenv
GITHUB_APP_SLUG=your-app-slug
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY=-----BEGIN...replace-newlines-with-\n...END-----
GITHUB_APP_STATE_SECRET=the-generated-random-value
GITHUB_APP_WEBHOOK_SECRET=the-webhook-secret
FRONTEND_URL=http://localhost:5173
```

Never use a `VITE_` prefix for these values. In production, store them in Secret Manager.

## Connect repositories with a GitHub App

Restart the backend, sign in, and open **Repositories → Connect GitHub App**. GitHub shows the account or organization installation screen; choose **Only select repositories** and grant the app access only to repositories NEXUS should use. GitHub redirects back to NEXUS, which then lists the installed repositories in the UI and task form.

Disconnecting removes the stored installation link. Uninstalling the app in GitHub also removes the link when the installation webhook is delivered.

## 4. Add task attachments

Task creation and task details accept up to ten files, 10 MB each and 20 MB total: PDF, PNG, JPEG, WebP, GIF, plain text, Markdown, CSV, and JSON. Files are private, scoped to their task, and available for authorized download/removal. Before coding, the selected Vertex AI coding runtime passes them to the agent as document/image inputs. Attachments are treated as untrusted context; the model has no shell or filesystem tools and can only return bounded, validated file operations.

Local development stores files under `ATTACHMENTS_ROOT`. Production requires a private `ATTACHMENTS_BUCKET` and grants the API service identity object access to that bucket.

## 5. Enable local model and worker stages

Analysis and coding model choices are available on each task. The recommended default for both is
`gemini-3.8-flash`. Selectable Gemini options include Gemini 3.1 Pro (preview), 3.6 Flash,
3.5 Flash, 3.5 Flash-Lite, and 3.1 Flash-Lite. Vertex AI MaaS also provides Qwen3 Coder and
DeepSeek V3.2 as open-model choices. These open models use Vertex AI with Application Default
Credentials and do not support task attachments, so they are hidden from the coding selector when
attachments are present. Analysis uses the configured Gemini provider. Coding uses Vertex AI with Application Default Credentials,
`GOOGLE_CLOUD_PROJECT`, and `CODING_MODEL_LOCATION` (default `global`); it does not use
`GEMINI_API_KEY`. The Cloud Run service identity needs `roles/aiplatform.user` and the Vertex AI API
enabled in the project. Open-model requests use Vertex AI's global endpoint and require the
Agent Platform API plus access to the selected MaaS model in the project.

When GCP billing is unavailable, get a Gemini API key from Google AI Studio and keep it in the backend `.env`:

```dotenv
GEMINI_API_KEY=your-backend-only-key
```

For local coding, install the Antigravity SDK and the runtimes needed by the repositories you connect. Use a private workspace outside the NEXUS checkout. Validation is delegated to Antigravity, which discovers the repository's existing checks and runs them in a sandboxed copy of the workspace:

```dotenv
WORKSPACE_ROOT=/tmp/nexus-workspaces
NEXUS_CODING_ENABLED=true
NEXUS_VALIDATION_ENABLED=true
NEXUS_VALIDATION_TIMEOUT_SECONDS=3600
NEXUS_AI_PERMISSIONS=ai_execute,ai_validate,security_review
```

Restart the backend after changing these settings. No per-repository validation profile or prebuilt language-specific image is needed. The worker must report an available Antigravity command sandbox; if it does not, validation blocks rather than running repository code without isolation. The example environment deliberately leaves the worker flags and AI permissions off.

When an execution is requested, the isolated worker creates a branch, asks the coding model for bounded file operations, asks Antigravity to discover and run the repository's existing checks, and performs security review. A validated task opens a PR automatically when human approval is not required. If approval is required, approving the patch opens the PR; transient publication failures keep a retry action on the task page.

A repository with no applicable runnable checks is reported as skipped and does not satisfy validation or publication gates. Antigravity cannot install dependencies or change the validation workspace; required tools and project dependencies must already be available in the worker environment.

## 6. Hosted boundary

Hosted execution uses a Cloud Run Job per task. The API, worker, repository credentials, and sandboxed Antigravity validation must all be configured before enabling coding and validation. Deploying the connection UI and task attachments alone does not enable hosted code execution or automatic PR publication.
