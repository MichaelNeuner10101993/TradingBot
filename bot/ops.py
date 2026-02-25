"""
Ops: Logging-Setup, Retry/Backoff-Decorator, Circuit Breaker.
"""
import logging
import os
import time
import functools
from pathlib import Path
from bot.config import OpsConfig


def setup_logging(cfg: OpsConfig) -> logging.Logger:
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    log_file = os.path.join(cfg.log_dir, "bot.log")

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("tradingbot")


def retry_backoff(
    retries: int = 3,
    base_delay: float = 2.0,
    exceptions: tuple = (Exception,),
    no_retry: tuple = (),
    logger: logging.Logger = None,
):
    """Decorator: wiederholt eine Funktion bei bestimmten Exceptions mit exponentiellem Backoff.
    no_retry: Diese Exceptions werden sofort weitergegeben, ohne Retry-Versuche."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    if no_retry and isinstance(e, no_retry):
                        raise  # sofort weitergeben, kein Retry
                    if attempt == retries:
                        raise
                    msg = f"[retry {attempt}/{retries}] {fn.__name__} fehlgeschlagen: {e} – warte {delay}s"
                    if logger:
                        logger.warning(msg)
                    else:
                        print(msg)
                    time.sleep(delay)
                    delay *= 2
        return wrapper
    return decorator


class CircuitBreaker:
    """Zählt konsekutive Fehler; wirft nach max_errors eine Exception."""

    def __init__(self, max_errors: int, logger: logging.Logger = None):
        self.max_errors = max_errors
        self.error_count = 0
        self.logger = logger

    def success(self):
        self.error_count = 0

    def failure(self, exc: Exception):
        self.error_count += 1
        if self.logger:
            self.logger.error(f"Fehler #{self.error_count}/{self.max_errors}: {exc}")
        if self.error_count >= self.max_errors:
            raise RuntimeError(
                f"Circuit Breaker ausgelöst nach {self.max_errors} konsekutiven Fehlern."
            ) from exc
