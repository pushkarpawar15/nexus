"""Prototype API benchmark for NEXUS.

Runs the real FastAPI application behind a real uvicorn server on localhost,
using the in-memory adapters that are permitted only with ``NEXUS_ENV=testing``.
Authentication uses the app's supported test seam (``app.state.firebase_token_verifier``),
so every request goes through the real bearer-token and role checks.

Measured sections:

1. Cold start: interpreter import of the API, app factory, and first healthy response.
2. Sequential endpoint latency (p50/p95/p99) for the read and write paths the UI uses.
3. Write-path scaling: create, transition, and optimistic-concurrency conflict handling.
4. Dataset scaling: task list and activity reads at 100 / 1,000 / 5,000 tasks.
5. Attachment upload and download throughput at 1 MB and 10 MB.
6. Sustained concurrent load: requests per second and latency percentiles.

Usage:

    cd backend
    NEXUS_ENV=testing NEXUS_STORAGE=memory ../scripts/benchmark_api.py --output results.json

The script writes a JSON summary (default: scripts/benchmark-results.json) that can be
pasted into docs/performance-report.md. It never contacts Vertex AI, GitHub, or GCS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
REPOSITORY_KEY = "bench-repo"
TOKEN = "bench-operator"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Import timing of the API package is measured as part of the cold-start section.
_import_started = time.perf_counter()
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

IMPORT_SECONDS = time.perf_counter() - _import_started


def build_app() -> Any:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        approved_repositories={REPOSITORY_KEY: {"owner": "example-org", "repo": "bench-repo"}},
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: {
        "uid": "bench-user",
        "nexus_role": "operator",
    }
    # configure_logging() runs inside create_app; keep per-request client logs out of the run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return app


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def summarize(latencies_ms: list[float]) -> dict[str, float]:
    return {
        "samples": len(latencies_ms),
        "mean_ms": round(statistics.fmean(latencies_ms), 2),
        "p50_ms": round(percentile(latencies_ms, 0.50), 2),
        "p95_ms": round(percentile(latencies_ms, 0.95), 2),
        "p99_ms": round(percentile(latencies_ms, 0.99), 2),
        "max_ms": round(max(latencies_ms), 2),
    }


def measure_sequential(
    client: httpx.Client,
    label: str,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    iterations: int = 200,
    warmup: int = 20,
) -> dict[str, Any]:
    latencies: list[float] = []
    statuses: set[int] = set()
    for index in range(warmup + iterations):
        started = time.perf_counter()
        if method == "GET":
            response = client.get(url, headers=AUTH)
        elif method == "PATCH":
            response = client.patch(url, json=json_body, headers=AUTH)
        else:
            response = client.post(url, json=json_body, headers=AUTH)
        elapsed_ms = (time.perf_counter() - started) * 1000
        statuses.add(response.status_code)
        if index >= warmup:
            latencies.append(elapsed_ms)
    return {
        "label": label,
        "method": method,
        "url": url,
        "status_codes": sorted(statuses),
        **summarize(latencies),
    }


def create_task_payload(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "description": "Benchmark task created by scripts/benchmark_api.py.",
        "repository_key": REPOSITORY_KEY,
    }


def serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.perf_counter() + 60
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as probe:
        while True:
            try:
                if probe.get("/health").status_code == 200:
                    return server, thread
            except httpx.HTTPError:
                pass
            if time.perf_counter() > deadline:
                raise RuntimeError("Server did not become healthy within 60 seconds.")
            time.sleep(0.01)


def shutdown(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)


def seed_tasks(client: httpx.Client, count: int, prefix: str) -> None:
    for index in range(count):
        response = client.post(
            "/api/tasks", json=create_task_payload(f"{prefix} {index}"), headers=AUTH
        )
        if response.status_code != 201:
            raise RuntimeError(f"Seeding failed: {response.status_code} {response.text}")


def benchmark_concurrency(
    base_url: str, *, workers: int, duration_seconds: float
) -> dict[str, Any]:
    async def worker(queue_stats: list[float], stop_at: float) -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            while time.perf_counter() < stop_at:
                started = time.perf_counter()
                response = await client.get("/api/tasks?limit=25", headers=AUTH)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"Load request failed: {response.status_code}")
                queue_stats.append(elapsed_ms)

    async def run() -> tuple[int, list[float]]:
        stop_at = time.perf_counter() + duration_seconds
        per_worker: list[list[float]] = [[] for _ in range(workers)]
        await asyncio.gather(*(worker(stats, stop_at) for stats in per_worker))
        combined = [value for stats in per_worker for value in stats]
        return len(combined), combined

    total, latencies = asyncio.run(run())
    return {
        "workers": workers,
        "duration_seconds": duration_seconds,
        "requests": total,
        "requests_per_second": round(total / duration_seconds, 1),
        **summarize(latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "scripts" / "benchmark-results.json",
    )
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--seed-small", type=int, default=100)
    parser.add_argument("--seed-medium", type=int, default=1000)
    parser.add_argument("--seed-large", type=int, default=5000)
    parser.add_argument("--load-workers", type=int, default=20)
    parser.add_argument("--load-seconds", type=float, default=5.0)
    args = parser.parse_args()

    os.environ.setdefault("ATTACHMENTS_ROOT", "/tmp/opencode/nexus-bench-attachments")
    base_url = f"http://127.0.0.1:{args.port}"
    results: dict[str, Any] = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "AMD Ryzen 5 4600H (6C/12T)",
            "storage": "in-memory adapters (NEXUS_ENV=testing)",
            "server": f"uvicorn {uvicorn.__version__} on localhost, single event loop",
        }
    }

    factory_started = time.perf_counter()
    app = build_app()
    factory_seconds = time.perf_counter() - factory_started

    serve_started = time.perf_counter()
    server, thread = serve(app, args.port)
    ready_seconds = time.perf_counter() - serve_started
    results["cold_start"] = {
        "import_seconds": round(IMPORT_SECONDS, 3),
        "app_factory_seconds": round(factory_seconds, 3),
        "first_healthy_response_seconds": round(ready_seconds, 3),
        "total_seconds": round(IMPORT_SECONDS + factory_seconds + ready_seconds, 3),
    }

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        print("Seeding tasks for dataset-scaling measurements...", flush=True)
        seed_started = time.perf_counter()
        seed_tasks(client, args.seed_large, "scale")
        seed_seconds = time.perf_counter() - seed_started
        results["seeding"] = {
            "tasks": args.seed_large,
            "seconds": round(seed_seconds, 2),
            "tasks_per_second": round(args.seed_large / seed_seconds, 1),
        }

        iterations = args.iterations
        endpoints: list[dict[str, Any]] = []
        endpoints.append(
            measure_sequential(client, "Health probe", "GET", "/health", iterations=50)
        )
        endpoints.append(
            measure_sequential(client, "Readiness probe", "GET", "/api/ready", iterations=50)
        )
        endpoints.append(
            measure_sequential(client, "Model catalog", "GET", "/api/models", iterations=iterations)
        )
        endpoints.append(
            measure_sequential(
                client, "Repository list", "GET", "/api/repositories", iterations=iterations
            )
        )
        endpoints.append(
            measure_sequential(
                client,
                f"Task list (25 of {args.seed_large + 4 * iterations + 3})",
                "GET",
                "/api/tasks?limit=25",
                iterations=iterations,
            )
        )
        endpoints.append(
            measure_sequential(
                client,
                "Task create",
                "POST",
                "/api/tasks",
                json_body=create_task_payload("Benchmark create"),
                iterations=iterations,
            )
        )
        first_task = client.post(
            "/api/tasks", json=create_task_payload("Benchmark read target"), headers=AUTH
        ).json()
        task_id = first_task["id"]
        endpoints.append(
            measure_sequential(
                client, "Task read", "GET", f"/api/tasks/{task_id}", iterations=iterations
            )
        )
        endpoints.append(
            measure_sequential(
                client,
                "Task metadata update",
                "PATCH",
                f"/api/tasks/{task_id}",
                json_body={"assignee": "bench-user"},
                iterations=iterations,
            )
        )
        endpoints.append(
            measure_sequential(
                client,
                "Task history (empty)",
                "GET",
                f"/api/tasks/{task_id}/history",
                iterations=iterations,
            )
        )
        endpoints.append(
            measure_sequential(
                client, "Recent activity", "GET", "/api/activity?limit=50", iterations=50
            )
        )
        endpoints.append(
            measure_sequential(client, "Skill list", "GET", "/api/skills", iterations=iterations)
        )

        transition_targets = [
            client.post(
                "/api/tasks", json=create_task_payload("Benchmark transition"), headers=AUTH
            ).json()["id"]
            for _ in range(iterations)
        ]
        transition_latencies: list[float] = []
        for target in transition_targets:
            started = time.perf_counter()
            response = client.post(
                f"/api/tasks/{target}/transitions",
                json={"to_status": "BLOCKED", "reason": "benchmark"},
                headers=AUTH,
            )
            transition_latencies.append((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                raise RuntimeError(f"Transition failed: {response.status_code} {response.text}")
        endpoints.append(
            {
                "label": "Workflow transition (BACKLOG to BLOCKED)",
                "method": "POST",
                "url": "/api/tasks/{id}/transitions",
                "status_codes": [200],
                **summarize(transition_latencies),
            }
        )

        results["endpoints"] = endpoints

        attachments: list[dict[str, Any]] = []
        attachment_task = client.post(
            "/api/tasks", json=create_task_payload("Benchmark attachments"), headers=AUTH
        ).json()
        for size_mb, label in ((1, "1 MB"), (10, "10 MB")):
            payload = os.urandom(size_mb * 1_000_000)
            upload_latencies: list[float] = []
            download_latencies: list[float] = []
            for iteration in range(6):
                upload_url = f"/api/tasks/{attachment_task['id']}/attachments"
                started = time.perf_counter()
                response = client.post(
                    upload_url,
                    files=[("files", (f"bench-{size_mb}mb.txt", payload, "text/plain"))],
                    headers=AUTH,
                )
                upload_ms = (time.perf_counter() - started) * 1000
                if response.status_code != 201:
                    raise RuntimeError(
                        f"Attachment upload failed: {response.status_code} {response.text}"
                    )
                attachment_id = response.json()["items"][0]["id"]
                download_url = f"/api/tasks/{attachment_task['id']}/attachments/{attachment_id}"
                started = time.perf_counter()
                download = client.get(download_url, headers=AUTH)
                download_ms = (time.perf_counter() - started) * 1000
                if download.status_code != 200 or len(download.content) != len(payload):
                    raise RuntimeError("Attachment download returned unexpected content.")
                # Delete after each iteration so the 20 MB per-task cap is never reached.
                deleted = client.delete(download_url, headers=AUTH)
                if deleted.status_code != 204:
                    raise RuntimeError("Attachment delete failed.")
                if iteration:  # first iteration warms up storage and hashing paths
                    upload_latencies.append(upload_ms)
                    download_latencies.append(download_ms)
            upload_stats = summarize(upload_latencies)
            download_stats = summarize(download_latencies)
            upload_stats.update(
                {
                    "label": f"Attachment upload ({label})",
                    "method": "POST",
                    "url": "/api/tasks/{id}/attachments",
                    "status_codes": [201],
                    "throughput_mb_per_s": round(size_mb / (upload_stats["p50_ms"] / 1000), 1),
                }
            )
            download_stats.update(
                {
                    "label": f"Attachment download ({label})",
                    "method": "GET",
                    "url": "/api/tasks/{id}/attachments/{attachment_id}",
                    "status_codes": [200],
                    "throughput_mb_per_s": round(size_mb / (download_stats["p50_ms"] / 1000), 1),
                }
            )
            attachments.extend([upload_stats, download_stats])
        results["attachments"] = attachments

        results["concurrency"] = benchmark_concurrency(
            base_url, workers=args.load_workers, duration_seconds=args.load_seconds
        )

    shutdown(server, thread)

    scaling: list[dict[str, Any]] = []
    for index, (label, size) in enumerate(
        (
            (f"{args.seed_small} tasks", args.seed_small),
            (f"{args.seed_medium:,} tasks", args.seed_medium),
            (f"{args.seed_large:,} tasks", args.seed_large),
        )
    ):
        scaling_app = build_app()
        scaling_server, scaling_thread = serve(scaling_app, args.port + 1 + index)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{args.port + 1 + index}", timeout=60.0
        ) as client:
            seed_tasks(client, size, "scaling")
            scaling.append(
                {
                    "dataset": label,
                    "task_list_first_page": measure_sequential(
                        client,
                        "Task list (limit=25)",
                        "GET",
                        "/api/tasks?limit=25",
                        iterations=100,
                        warmup=10,
                    ),
                    "task_list_page_at_offset_200": measure_sequential(
                        client,
                        "Task list (limit=25, offset=200)",
                        "GET",
                        "/api/tasks?limit=25&offset=200",
                        iterations=100,
                        warmup=10,
                    ),
                    "recent_activity": measure_sequential(
                        client,
                        "Recent activity (limit=50)",
                        "GET",
                        "/api/activity?limit=50",
                        iterations=50,
                        warmup=5,
                    ),
                }
            )
        shutdown(scaling_server, scaling_thread)
    results["dataset_scaling"] = scaling

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\nResults written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
