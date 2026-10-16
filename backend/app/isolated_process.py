"""Standard-library-only process entry used for isolated Antigravity calls."""

from __future__ import annotations

import asyncio
import inspect
import os
import pickle
import site
import sys


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
            connection.send(("error", type(exc).__name__, str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
