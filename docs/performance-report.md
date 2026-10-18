# NEXUS prototype performance report

Benchmark date: 2026-10-18. This report records what the NEXUS prototype actually measures on a
developer machine, so the Google AI Builder Cup submission can cite real numbers instead of
estimates. Every figure below comes from the reproducible harness in `scripts/benchmark_api.py`
(backend) and the standard project checks (frontend build and test suites). Raw machine-readable
output is committed next to the harness as `scripts/benchmark-results.json`.

## Scope

Measured:

- Cold start of the FastAPI service (import, app factory, first healthy HTTP response).
- Sequential request latency for the endpoints the UI uses, with authentication and role checks
  exercised on every request.
- Write-path throughput: task creation, workflow transitions, and attachment upload/download.
- Dataset scaling of task and activity reads at 100 / 1,000 / 5,000 tasks.
- Sustained concurrent load against the task list endpoint.
- Frontend production build time, emitted bundle sizes, and test-suite runtime.
- Backend test-suite runtime and slowest cases.

Not measured (out of scope for a local prototype benchmark):

- Live Gemini/Vertex AI model calls and GitHub API writes. Both need project credentials and are
  subject to external network latency that the prototype does not control.
- Firestore-backed request latency. The benchmark uses the in-memory adapters, which are permitted
  only under `NEXUS_ENV=testing`; production adds one network round trip per Firestore operation.
- Cloud Run cold starts, container image build time, and CDN delivery of the hosted frontend.

## Environment

| Item | Value |
| --- | --- |
| CPU | AMD Ryzen 5 4600H, 6 cores / 12 threads |
| RAM | 7 GB total (roughly 1 GB available during the run) |
| OS | Linux 6.6 (Ubuntu-based, x86_64, glibc 2.35) |
| Python | 3.11.17 |
| API server | uvicorn 0.54.0, single process, single event loop, `127.0.0.1` |
| Storage under test | in-memory task/workflow adapters, local-disk attachment storage |
| Node.js | 20.20.2, Vite 6.4.4 |

Client and server run in the same Python process, so latency figures include HTTP over loopback,
bearer-token verification, Pydantic validation, and the storage adapter — but no network distance
and no Firestore round trip.

## How to reproduce

```sh
cd backend
NEXUS_ENV=testing NEXUS_STORAGE=memory .venv/bin/python ../scripts/benchmark_api.py
```

The harness starts the real application (`app.main:create_app`) behind a real uvicorn server and
authenticates every request through the supported test seam
(`app.state.firebase_token_verifier`), so the bearer-token and role-check paths are executed, not
bypassed. It never contacts Vertex AI, GitHub, or Cloud Storage.

Backend and frontend checks used for the build/test sections:

```sh
cd backend && .venv/bin/python -m pytest --durations=10
cd frontend && time npm run build && time npm test
```

## Results

### Cold start

| Phase | Seconds |
| --- | --- |
| Python import of the API package | 0.695 |
| App factory (repositories, services, middleware) | 0.042 |
| First healthy `/health` response after server start | 0.109 |
| **Total, process start to first 200** | **0.846** |

Sub-second cold start means a single Cloud Run instance scales from zero to serving without a
perceptible pause; the imported stack includes FastAPI, Firebase Admin, the Google client libraries,
and Google ADK.

### Endpoint latency (sequential, 200 samples after 20 warm-up requests, 5,803 tasks resident)

| Endpoint | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| `GET /health` | 1.73 | 2.31 | 3.39 |
| `GET /api/ready` | 3.09 | 4.07 | 4.47 |
| `GET /api/models` | 2.63 | 4.81 | 5.10 |
| `GET /api/repositories` | 2.33 | 4.14 | 5.04 |
| `GET /api/tasks?limit=25` | 7.58 | 8.92 | 11.79 |
| `POST /api/tasks` | 2.77 | 5.12 | 5.83 |
| `GET /api/tasks/{id}` | 3.85 | 4.74 | 5.11 |
| `PATCH /api/tasks/{id}` | 3.22 | 5.66 | 6.22 |
| `GET /api/tasks/{id}/history` | 2.42 | 3.05 | 3.26 |
| `GET /api/skills` | 2.43 | 3.41 | 4.42 |
| `POST /api/tasks/{id}/transitions` | 3.24 | 5.81 | 6.48 |
| `GET /api/activity?limit=50` | **131.11** | **189.72** | **215.89** |

Every interactive endpoint the task board and workflow pages poll stays in low single-digit
milliseconds even with nearly 6,000 tasks resident, which keeps the 20–30 second UI polling cadence
comfortably transparent. The one exception is the recent-activity feed, analyzed below.

### Dataset scaling (fresh server per size, 100 samples)

| Dataset | Task list p50 (ms) | Task list p95 (ms) | Deep page `offset=200` p50 (ms) | Recent activity p50 (ms) |
| --- | --- | --- | --- | --- |
| 100 tasks | 3.40 | 6.98 | 2.39 | 4.81 |
| 1,000 tasks | 4.00 | 6.98 | 3.88 | 26.03 |
| 5,000 tasks | 7.89 | 9.33 | 7.55 | 125.13 |

Task listing scales mildly: doubling the dataset adds well under a millisecond, because pagination
and filtering are applied before serialization. The recent-activity feed is linear in the number of
stored events — the endpoint loads up to 5,000 events plus the caller's visible tasks and filters in
process — which is the single clearest optimization target in the prototype (see Findings).

### Attachment throughput (local private storage, 5 samples after warm-up)

| Operation | p50 (ms) | p95 (ms) | Throughput |
| --- | --- | --- | --- |
| Upload 1 MB | 8.73 | 15.37 | 114.5 MB/s |
| Download 1 MB | 7.09 | 10.62 | 141.0 MB/s |
| Upload 10 MB | 76.44 | 92.70 | 130.8 MB/s |
| Download 10 MB | 35.44 | 39.36 | 282.2 MB/s |

Uploads include SHA-256 fingerprinting and the optimistic-concurrency task write, so the effective
cost is hashing plus storage. A full 10 MB attachment — the per-file ceiling — moves in under
100 ms locally, well inside the envelope for multimodal Gemini analysis of screenshots, PDFs, and
log files. In production the same bytes travel through a private GCS bucket, which adds network
time proportional to the operator's uplink.

### Sustained concurrent load (20 workers, 5 seconds, `GET /api/tasks?limit=25`)

| Metric | Value |
| --- | --- |
| Requests completed | 556 |
| Throughput | 111.2 requests/second |
| Latency p50 | 166.41 ms |
| Latency p95 | 249.82 ms |
| Latency p99 | 308.12 ms |

Caveat: the load generator and the API share one Python interpreter, so the GIL couples client-side
JSON parsing with server-side work; the numbers understate what a separate client machine would
observe, and they represent a worst-case local arrangement rather than a deployed topology. Even
so, 111 req/s of authenticated task reads against a 5,800-task dataset means a single instance
absorbs the polling traffic of hundreds of concurrent operators. Task creation throughput, measured
while seeding, was 277.5 writes/second through the full authenticated create path.

### Frontend build and bundle

| Metric | Value |
| --- | --- |
| Production build wall time (`tsc -b && vite build`) | 12.6 s (Vite transform+emit: 4.3 s) |
| Modules transformed | 1,877 |
| `index.js` (app code) | 264.3 kB raw / 76.1 kB gzip |
| `react-vendor.js` (React, React DOM, Router) | 262.6 kB raw / 83.3 kB gzip |
| `firebase.js` (Firebase Auth) | 168.5 kB raw / 34.3 kB gzip |
| `icons.js` (lucide-react, tree-shaken) | 13.9 kB raw / 3.1 kB gzip |
| CSS | 54.4 kB raw / 10.0 kB gzip |
| **Total first-load payload (gzip)** | **≈ 207 kB** |

The vendor split keeps the long-lived React and Firebase chunks cacheable while the app chunk
changes per release. A ~207 kB gzipped first load renders the workflow shell on a mid-range mobile
connection without a visible blank screen.

### Automated test suites

| Suite | Cases | Runtime |
| --- | --- | --- |
| Backend (pytest) | 130 passed, 0 failed | 10.0 s in-suite (12.7 s wall including collection) |
| Frontend (vitest) | 4 passed, 0 failed | 2.35 s duration (3.4 s wall) |

Slowest backend cases confirm the expensive paths are the real ones, not stubs: the full local
pipeline test (3.12 s) runs a genuine temporary Git repository with fake model/validator boundaries,
and the validation-runner isolation tests (1.03 s / 0.40 s) start and time out real child
processes. A developer gets a complete correctness signal in well under a minute.

## Findings

1. **Recent-activity feed is the scaling hotspot.** It grows linearly with stored events (4.8 ms at
   100 tasks, 125 ms at 5,000). The fix is already half-built: `list_for_task_page` provides cursor
   pagination for per-task history, and the activity endpoint should adopt the same cursor pattern
   with server-side visibility filtering instead of loading up to 5,000 events into memory. Under
   Firestore this also bounds read costs.
2. **Everything else scales well.** Task list, read, create, update, transition, skills, and model
   catalog all stay under 10 ms p95 at 6,000 tasks, so the interactive surface has roughly 10×
   headroom before the 20–30 s UI poll would ever be noticeable.
3. **Attachment pipeline is not a bottleneck.** Effective throughput of 115–282 MB/s means the
   10 MB/20 MB per-task limits are policy bounds for model context, not performance bounds.
4. **Reproducibility gap closed.** The benchmark found `app/services/execution_pipeline.py` and
   `app/services/langgraph_workflow.py` importing `langgraph` without it being declared in
   `backend/pyproject.toml`/`uv.lock`. The operator-input graph has since been replaced with a
   Google ADK human-input workflow: `google-adk` and `google-genai>=2.30` are declared dependencies,
   `langgraph` is removed, and a fresh environment imports `app.main` and runs the backend test
   suite without manual installs.
5. **Concurrency headroom is CPU-bound, not framework-bound.** With one uvicorn worker the service
   saturated its shared-interpreter budget at ~111 req/s of authenticated reads. Cloud Run's
   multi-instance scaling and a higher worker count are the natural next measurements once hosted
   execution is enabled.

## Limitations

- Storage under test is the in-memory adapter restricted to `NEXUS_ENV=testing`. Production
  Firestore adds one network round trip per read/write; expect single-digit-millisecond endpoints
  to move into the tens of milliseconds and size the activity-feed fix accordingly.
- No live Gemini/Vertex AI or GitHub traffic was generated; model and PR-publication latency is
  dominated by those external services and is documented qualitatively in `docs/architecture.md`
  instead.
- Figures come from one machine and one run window on laptop-class hardware with about 1 GB of free
  RAM; they characterize the prototype's shape (what is fast, what scales how), not a production
  SLO. Re-running the harness on the deployment target is the next step.
- The load test shares one Python process between client and server, as noted above; treat the
  throughput figure as a conservative floor.
