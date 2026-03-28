from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


@contextmanager
def log_timed_step(logger: logging.Logger, label: str) -> Iterator[None]:
    started = time.perf_counter()
    logger.info("Start: %s", label)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        logger.info("Done: %s (%.2fs)", label, elapsed)
