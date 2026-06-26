"""Logging configuration for the server entrypoint.

Default sink is **stderr**, which under systemd is captured by **journald**
(`journalctl -u live-memory`) — the idiomatic place for a service's logs, with
unit/PID metadata and journald's own rotation. Optionally ALSO write a rotating
plain-text file via `LIVE_MEMORY_LOG_FILE` (durable + greppable without
journalctl, and independent of whether journald is persistent vs volatile).

Env:
  LIVE_MEMORY_LOG_LEVEL   default INFO
  LIVE_MEMORY_LOG_FILE    unset → journald only; a path → also log there (rotating)
  LIVE_MEMORY_LOG_MAX_BYTES   per-file cap before rotation (default 10 MiB)
  LIVE_MEMORY_LOG_BACKUPS     rotated files kept (default 5)
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(logger: logging.Logger | None = None) -> logging.Logger:
    log = logger if logger is not None else logging.getLogger()
    log.setLevel(os.environ.get("LIVE_MEMORY_LOG_LEVEL", "INFO").upper())
    fmt = logging.Formatter(_FORMAT)

    stderr = logging.StreamHandler()  # → journald under systemd; → terminal when run by hand
    stderr.setFormatter(fmt)
    log.addHandler(stderr)

    log_file = os.environ.get("LIVE_MEMORY_LOG_FILE")
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=int(os.environ.get("LIVE_MEMORY_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
            backupCount=int(os.environ.get("LIVE_MEMORY_LOG_BACKUPS", "5")),
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log
