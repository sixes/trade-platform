from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from backend.config import Settings

_configured = False


def setup_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(settings.get("logging.max_bytes", 5 * 1024 * 1024)),
        backupCount=int(settings.get("logging.backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
