"""Run the API and append its log to a file when a log directory is set.

Docker stdout still receives the same lines. The file is how a recreated
container keeps the earlier lines.
"""

from __future__ import annotations

import copy
import os
import socket

import uvicorn
from uvicorn.config import LOGGING_CONFIG


def log_path() -> str | None:
    """The append-only file for this API copy, or None on a laptop run."""
    directory = (os.getenv("API_LOG_DIR") or "").strip()
    if not directory:
        return None
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"api-{socket.gethostname()}.log")


def build_log_config() -> dict:
    """Uvicorn's log config, plus one file that keeps growing."""
    config = copy.deepcopy(LOGGING_CONFIG)
    path = log_path()
    if path is None:
        return config
    config["formatters"]["file_default"] = {
        "()": "uvicorn.logging.DefaultFormatter",
        "fmt": "%(asctime)s %(levelprefix)s %(message)s",
        "use_colors": False,
    }
    config["formatters"]["file_access"] = {
        "()": "uvicorn.logging.AccessFormatter",
        "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        "use_colors": False,
    }
    config["handlers"]["file"] = {
        "formatter": "file_default",
        "class": "logging.FileHandler",
        "filename": path,
        "mode": "a",
    }
    config["handlers"]["access_file"] = {
        "formatter": "file_access",
        "class": "logging.FileHandler",
        "filename": path,
        "mode": "a",
    }
    config["loggers"]["uvicorn"]["handlers"].append("file")
    error_logger = config["loggers"].setdefault("uvicorn.error", {})
    error_logger.setdefault("handlers", []).append("file")
    config["loggers"]["uvicorn.access"]["handlers"].append("access_file")
    return config


def main() -> None:
    uvicorn.run(
        "services.api:app",
        host="0.0.0.0",
        port=8000,
        log_config=build_log_config(),
    )


if __name__ == "__main__":
    main()
