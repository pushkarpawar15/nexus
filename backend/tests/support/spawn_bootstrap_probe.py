from __future__ import annotations

import asyncio
import site
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))


def _import_pydantic_in_isolated_child() -> str:
    import pydantic

    return pydantic.__version__


if __name__ == "__main__":
    from app.integrations.validation_runner import _run_isolated_antigravity_call

    site_paths = set(site.getsitepackages())
    sys.path[:] = [path for path in sys.path if path not in site_paths]
    version = asyncio.run(
        _run_isolated_antigravity_call(
            _import_pydantic_in_isolated_child,
            timeout_seconds=10,
            stage="bootstrap probe",
        )
    )
    print(f"pydantic={version}")
