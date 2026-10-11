# NEXUS implementation plan

This plan records the reconstructed code and delivery boundaries. Incident Investigator remains out of scope.

## Completed foundations

### 1. Runtime and project boot

- Python backend package and React 19 / TypeScript / Vite 6 frontend manifests.
- Node 20-compatible frontend dependencies, npm lockfile, Tailwind 4 theme, root styles, fonts and Vite API proxies.
- Local Firebase Auth/Firestore emulator settings and separate frontend `.env.example`.
- Frontend root render smoke test.

### 2. Configuration and access foundation

- Required `NEXUS_STORAGE`, `development`/`testing`/`production` validation, ignored blank optional values and allowlisted JSON repository targets.
- Production rejects memory storage, emulator hosts and enabled coding/validation. Workspace paths must be absolute, separate from checkouts, and point to an allowlisted PayFlow key when local execution is enabled.
- Firebase ID-token authentication and viewer/operator/admin role gates.
- Authenticated repository listing and task create/list/detail/update/delete API with Firestore and test-memory stores.

### 3. Workflow services and API

- Structured Gemini analysis with deterministic worker capability, risk, and approval policy.
- Evidence-gated task state transitions, idempotent local execution dispatch, task history and activity APIs.
- Disposable local Git workspaces, bounded structured code proposals, patch fingerprints, cleanup, and target checkout integrity checks.
- Fixed-profile isolated Docker validation, persisted reports, static and model-assisted security review, and persisted execution results.
- Immutable, patch-specific admin approvals and checkpointed GitHub branch, commit, push, and pull request creation.
- Consistent API error envelopes, correlation IDs, API-version headers, health and readiness endpoints.

### 4. Browser workflow

- Firebase authentication context, authenticated API client, role-aware controls, reusable dialogs and notifications.
- Command Center, Workforce Board, Tasks, task details, Activity, and Approvals routes.
- Task creation/editing, analysis/execution actions, evidence reports, approval decisions, and eligible PR action.

### 3. Local startup and readiness

- `GET /health` reports process liveness.
- `GET /api/ready` pings the selected store and surfaces Firestore outages as 503.
- The API image starts through `app.main:create_app`; its memory-mode health/readiness and signed-out 401 boundary were smoke checked.

### 5. Isolated validation foundation

- Validator builder reads only the four approved manifests from the separate PayFlow checkout and rejects symlinks, oversized files and credential-like contents.
- Built `nexus-payflow-python-validator:local` and `nexus-payflow-node-validator:local`.
- The runner stages bounded UTF-8 sources, checks the patch fingerprint before and after, uses fixed command argv in network-disabled, nonroot containers, limits output/resources/time, and stops on first failure.
- All seven configured PayFlow checks passed in the isolated runner.

### 6. Persistence adapters

- Firestore and memory record repositories are wired for `executions`, `activity_events`, `validation_reports`, `security_reviews`, `approvals` and `pull_requests`.
- Approval records are append-only and keyed PR records support replay-safe create-or-get.
- Stage services and API transitions write immutable reports, activity, approvals, and PR checkpoints.

### 7. Hosted foundation

- Cloud Run API Dockerfile, Firebase Hosting SPA fallback, emulator ports and deny-all browser Firestore rules are configured.
- The API image builds and the local container smoke check passes. No hosted deployment is claimed; production execution remains disabled.

## Verification and remaining deployment work

The backend tests cover the local workflow with fake model and validator boundaries; frontend checks cover lint, type checking, production build, smoke tests, and formatting. The Auth and Firestore emulator acceptance run is recorded in `verification.md`. Real Vertex AI and GitHub calls, cloud provisioning, and hosted worker execution require external credentials or infrastructure and remain deployment tasks.
