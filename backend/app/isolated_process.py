"""Standard-library-only process entry used for isolated Antigravity calls."""

from __future__ import annotations

import asyncio
import inspect
import os
import pickle
import site
import sys
import traceback


def run_serialized_callable(connection, payload: bytes, module_search_paths: list[str]) -> None:
    """Load and run a serialized callable after restoring installed package paths."""
    try:
        os.setsid()
        paths = [*sys.path, *module_search_paths, *site.getsitepackages()]
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            paths.append(user_site)
        sys.path[:] = list(dict.fromkeys(path for path in paths if path))
        target, args, kwargs = pickle.loads(payload)
        result = target(*args, **kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        connection.send(("ok", result))
    except BaseException as exc:
        try:
            detail = str(exc)
            if isinstance(exc, ModuleNotFoundError):
                detail = (
                    f"{detail}; child executable={sys.executable!r}; "
                    f"child prefix={sys.prefix!r}; child sys.path={sys.path!r}; "
                    f"traceback={traceback.format_exc()}"
                )
            connection.send(("error", type(exc).__name__, detail))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
