"""Logging: rotating file under logs/ plus stderr. No print() anywhere in the package."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from jobhunt.config import ENV_LOG_LEVEL, Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_HANDLER_NAME = "jobhunt-file"


def _already_configured(root: logging.Logger) -> bool:
    return any(handler.get_name() == FILE_HANDLER_NAME for handler in root.handlers)


def configure_logging(settings: Settings, verbose: bool = False) -> None:
    if _already_configured(logging.getLogger()):
        return
    cfg = settings.section("logging")
    level_name = os.getenv(ENV_LOG_LEVEL) or ("DEBUG" if verbose else cfg.get("level", "INFO"))
    level = logging.getLevelNamesMapping().get(str(level_name).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)

    log_path = settings.resolve(cfg.get("file", "logs/jobhunt.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(cfg.get("max_bytes", 2_000_000)),
        backupCount=int(cfg.get("backup_count", 3)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.set_name(FILE_HANDLER_NAME)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING if not verbose else logging.DEBUG)
    root.addHandler(stream_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
