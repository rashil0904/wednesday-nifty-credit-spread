"""Local file logging for wednesday_nifty. No external notification channels."""
import logging
from logging.handlers import RotatingFileHandler

from . import config

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    logger = logging.getLogger(f"wednesday_nifty.{name}")
    logger.setLevel(logging.INFO)

    if not _configured:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

        file_handler = RotatingFileHandler(
            config.LOG_FILE, maxBytes=5_000_000, backupCount=10
        )
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        root = logging.getLogger("wednesday_nifty")
        root.setLevel(logging.INFO)
        root.addHandler(file_handler)
        root.addHandler(console_handler)
        root.propagate = False
        _configured = True

    return logger
