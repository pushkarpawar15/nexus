# Verification record

Verification was run from the NEXUS workspace on 2026-10-10. The PayFlow checkout at `/media/yivi/Mine/google_ai_builder_cup/payflow` was used only as a read-only source for the validation runner; its patch fingerprint was unchanged.

## Automated checks

- Backend: Ruff lint, Ruff formatting, and 33 pytest cases passed. The suite includes a complete local workflow run using a temporary Git repository and fake model, validator, and security boundaries.
- Frontend: ESLint, TypeScript type checking, Vite production build, all 4 Vitest tests, and Prettier checks passed.
- Dependency audit: the Vitest test runner was updated to the patched 4.1.x line; the frontend audit now reports 0 vulnerabilities.
- PayFlow validation profile: all seven isolated Docker checks passed (`backend_pytest`, `backend_ruff`, `backend_format`, `frontend_test`, `frontend_lint`, `frontend_typecheck`, and `frontend_build`). The runner used fixed commands, read-only source mounts, and containers without network access.
- Backend image: `docker build -t nexus-api:verification ./backend` succeeded. A container smoke check returned 200 from `/health` and `/api/ready` and 401 for a signed-out `/api/tasks` request.
- Repository hygiene: `git diff --check` passed.

## Auth and Firestore emulator acceptance

Firebase Auth and Firestore emulators were started with project `demo-nexus` on alternate local ports because the standard ports were already occupied. The FastAPI app used the same emulator endpoints. A fresh Auth Emulator ID token was accepted; the API returned 200 from `/health` and `/api/ready`, returned 401 for a signed-out task read, created a task with 201, and read the persisted task back with 200.

The first acceptance attempt found that the readiness probe used a Firestore-reserved document ID. The probe now reads `tasks/nexus-readiness`, and the repeat emulator acceptance passed.

## External service boundary

No live Vertex AI/Gemini request, GitHub write, cloud provisioning, or hosted deployment was performed. Those operations need the user's project credentials and configured deployment services. The sample `.env.example` keeps coding and validation disabled; the local pipeline was exercised with explicit fakes in the backend suite and the actual PayFlow validators were run independently against the unchanged checkout.
