"""Mutiny local console launcher."""

import logging
import os
import sys
from pathlib import Path


def _ensure_project_venv() -> None:
    """Re-exec with the local project venv when one is present."""
    if sys.prefix != sys.base_prefix:
        return
    project_root = Path(__file__).resolve().parent
    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


_ensure_project_venv()

_KNOWN_RUNTIME_DEPENDENCIES = {
    "fastapi",
    "uvicorn",
    "aiosqlite",
    "dotenv",
    "apscheduler",
    "litellm",
    "sqlalchemy",
    "pydantic",
    "pypdf",
}

from core.privacy import bootstrap

bootstrap()

try:
    import uvicorn
    from config import BIND_HOST, PORT, bind_host_error, ollama_endpoint_error
    from web.app import create_app
except ModuleNotFoundError as dependency_error:
    missing_name = str(getattr(dependency_error, "name", "") or "")
    if missing_name in _KNOWN_RUNTIME_DEPENDENCIES:
        raise SystemExit(
            f"Missing required dependency '{missing_name}'. Install project dependencies with:\n"
            f"  {sys.executable} -m pip install -r requirements.txt"
        ) from dependency_error
    raise


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutiny_bot")


def main() -> None:
    host_error = bind_host_error()
    if host_error:
        raise SystemExit(host_error)
    endpoint_error = ollama_endpoint_error()
    if endpoint_error:
        raise SystemExit(endpoint_error)
    logger.info("Mutiny console on http://%s:%s", BIND_HOST, PORT)
    uvicorn.run(create_app(), host=BIND_HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
