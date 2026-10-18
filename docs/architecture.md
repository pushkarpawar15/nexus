# NEXUS architecture

## Current implementation

The rebuilt backend is the `backend/app` package. It has validated configuration, Firebase ID-token authentication and role checks, GitHub App installation and repository allowlisting, task-scoped attachments, task CRUD, analysis, evidence-based workflow transitions, execution orchestration, report/history APIs, admin approvals, and GitHub pull request publication. Firestore and test-memory adapters persist task and workflow records. The earlier `backend/nexus_backend` sample remains for its existing tests; the documented server entry point is `app.main:create_app`.

The React/Vite app has the workflow routes, Firebase sign-in and role context, authenticated API calls, responsive navigation, task intake and attachment controls, evidence and activity views, repository management, approval and execution actions. The screen gates follow server-provided capabilities and current task evidence.

## Target architecture

| Area | Component | Responsibility |
| --- | --- | --- |
| Web app | React + TypeScript + Vite, Firebase Hosting | Task intake, execution progress, evidence, approval and review experience. |
| API | Python + FastAPI, Cloud Run | Authentication, configuration, task intake, workflow coordination and status reads. |
| Model access | Gemini through Vertex AI or Gemini API | Structured analysis, contextual security findings, and schema-constrained coding proposals. |
| Agent orchestration | NEXUS workflow services and Google ADK | NEXUS services own task transitions and side effects; an ADK human-input workflow pauses operator questions and persists the pause in Firestore sessions; the coding adapter returns bounded file operations through Vertex AI structured output. |
| Durable workflow state | Firestore | Tasks, executions, activity, validation/security reports, approvals, PR records and ADK operator-input sessions. |
| Coding and validation | Isolated local worker and prebuilt Docker validators | Disposable workspaces, bounded checks, actual test evidence. |
| PR publication | GitHub App and GitHub API | Publish approved, validated changes to repositories selected during installation. |
| Task attachments | Local private storage or Google Cloud Storage | Store bounded attachments and provide authorized download and model access. |
| Credentials | ADC, service identity and backend-only GitHub App secrets | Keep credentials out of source and browser bundles. |

Incident Investigator is deferred and excluded from the initial workflow.

## Persistence collections

The Firestore adapters use these top-level collection names:

| Collection | Record |
| --- | --- |
| `tasks` | Task state, analysis and transition history. |
| `executions` | Execution attempts. |
| `activity_events` | Task and worker activity. |
| `validation_reports` | Command results and patch fingerprint. |
| `security_reviews` | Static/model findings and patch fingerprint. |
| `approvals` | Immutable, patch-specific human decisions. |
| `pull_requests` | Commit, push and confirmed PR checkpoints. |
| `adk_sessions` | ADK sessions that persist operator-input questions and answers, one document per execution. |

The app factory selects Firestore explicitly outside test mode. A Firestore connectivity failure is surfaced by `/api/ready`; it never falls back to memory. Memory adapters are restricted to `NEXUS_ENV=testing`.

## Repair and human-input flow

- **Analysis proposal rejected:** one repair follows the initial proposal. A second invalid result records the rejection reason and asks an operator to clarify scope; the answer is sent to the planner for re-analysis.
- **Coding proposal rejected:** two repairs follow the initial proposal. A coding error or exhausted repair count asks for operator guidance. A live worker applies it in the same workspace; if the worker already stopped, the answer starts a fresh isolated execution.
- **Validation fails:** the validation agent receives the task plan's steps and validation checklist, and reports each failed command to the coding agent. The coding agent reviews the patch against those diagnostics before returning a repair. Up to three repairs run, each followed by repository validation again. At the limit, the task asks for guidance and continues the repair loop after the answer.
- **Security findings:** up to two code repairs run, with fresh validation and security review each time. At the limit, the task asks for guidance, then returns through coding, validation, and security review.
- **Security review cannot complete:** the task shows the review reason and asks an operator to resolve the reported issue or request another review.
- **Workspace or repository setup fails:** the worker reports the setup error and blocks execution. After credentials or configuration are fixed, an operator can retry.
- **PR publication fails:** publication retries three times using its saved GitHub checkpoint. After that, the task page offers a manual PR retry; code is not silently marked as published.
- **Approval is required:** validation and security must pass first. The existing admin approval gate remains in place before PR creation.
- **Published pull request follow-up:** after manual testing, an operator can ask for a fix or provide more information while the task is in `PR_CREATED` (or in `APPROVED`, e.g. when publication failed before a pull request was created). The answer is saved as operator guidance tagged with the follow-up intent and the task reopens through `PR_CREATED → ANALYZING` or `APPROVED → ANALYZING` (trusted, planner-only transitions); the planner re-analyzes with the new instructions. A follow-up execution reuses the task's latest pull request branch (including branches from publications that failed mid-way), and after fresh validation, security review, and an approval for the new patch fingerprint, publication pushes the new commit to that branch with `--force-with-lease` (leased against the branch's current remote commit from `ls-remote`) so the open pull request is updated instead of a second one being created; the pull request is then confirmed with a short re-read loop, because GitHub's pull-request listing can briefly serve the pre-push head commit right after a force-push. The merge webhook completes the task from the newest execution record that references the pull request. The input endpoint also still reopens a `COMPLETED` task for a fresh attempt with accumulated guidance.

After a bounded repair window, the task enters `AWAITING_INPUT`. The task page shows the question and related validation or security evidence. An operator answer is saved as coder guidance and shown in activity details. For an active local worker, the Google ADK workflow pauses at a human-input node and persists the pause in a Firestore session; the answer resumes the same execution and workspace. If the local worker stops, the answer starts a fresh execution because an uncommitted local workspace cannot survive process shutdown.

The workflow activity and typed task/execution records remain the user-facing evidence. ADK sessions store execution-control state, not a replacement for those records.

## Trust boundaries

1. The API accepts task text and a repository key, never a clone URL or shell command.
2. Repository keys map to server-selected targets. Static `APPROVED_REPOSITORIES` and GitHub App installations are workspace-wide; PAT connections are scoped to the Firebase user and resolve their token from Secret Manager. Task and execution APIs filter by the user's active repository keys.
3. Gemini recommendations are typed and remain subject to deterministic policy. Model output cannot grant permissions or approve work.
4. Coding runs in a disposable workspace separated from the NEXUS checkout and target checkout.
5. Validation uses fixed argv arrays inside prebuilt images with no network, nonroot users, dropped capabilities, no-new-privileges, read-only source mounts, bounded resources and command timeouts. Missing images and failed commands block the workflow.
6. Security findings and approval decisions refer to the patch fingerprint. Approvals are append-only.
7. GitHub publication is backend-only, allowlisted, URL-verified and idempotent. The browser never receives a GitHub token. Removing a PAT connection deletes its user connection and secret but retains task and execution records; reconnecting the same GitHub repository ID restores access to those records.

## Local and hosted boundaries

Local examples select the Auth and Firestore emulators and leave coding/validation disabled. Local coding requires a private workspace root, an available Antigravity command sandbox, project runtimes, and explicit policy permissions. A Gemini API key can be used locally while GCP billing is unavailable.

The Cloud Run image supports the API and Firestore readiness check. This version does not include a durable queue, isolated hosted worker or durable workspace handoff. Production configuration rejects memory storage, emulator hosts and enabled coding/validation flags. Production task attachments require a private GCS bucket. A deployment must not be represented as having run coding, validation, security review or PR creation.

## Delivery limits

- Gemini and GitHub integrations need valid project credentials and were not exercised against real external services during local verification.
- Coding and validation are disabled in the sample environment. Enabling them requires Vertex AI ADC, an Antigravity command sandbox, project runtimes, and a private workspace directory.
- The dispatcher is an in-process local worker. Hosted Cloud Run execution remains disabled until a durable queue and isolated worker handoff are provided.
- Firebase Hosting and Firestore rules are configured, but no cloud resources have been provisioned or deployed by this reconstruction.
