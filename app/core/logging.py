import logging
import os
from logging.handlers import RotatingFileHandler

import structlog

from app.config import settings


def setup_logging() -> None:
    log_dir = os.path.dirname(settings.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # FIX #18 — configure structlog ONCE with a coherent processor chain.
    # ConsoleRenderer and JSONRenderer must not appear together; JSONRenderer is
    # the final processor and produces a string that gets written to stdout/file.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
