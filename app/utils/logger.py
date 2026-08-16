"""Logging Configuration"""
import logging
import sys
from typing import Optional


def setup_logger(
    name: str,
    level: int = logging.INFO,
    format_string: Optional[str] = None
) -> logging.Logger:
    """Configure and return a logger"""
    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    logger = logging.getLogger(name)
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(format_string)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger
