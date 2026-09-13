"""Infinite-retry-with-backoff wrapper for transient network failures.

Used around CMR search, Earthdata granule download, and FIRMS HTTP calls so
that a DNS failure / connection error / timeout / 5xx server error blocks in
place and retries forever instead of being treated as "no data" and moving on
to the next date, window, or region.

Explicit errors (HTTP 401/403, bad credentials, malformed requests,
programming errors) are NOT retried -- they propagate immediately so callers
keep raising them as real errors.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Optional, TypeVar

import requests

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# 1st failure -> 5s, 2nd -> 10s, 3rd -> 20s, 4th -> 30s, 5th+ -> 60s (capped).
_BACKOFF_SCHEDULE_SECONDS = (5, 10, 20, 30, 60)

_TRANSIENT_MESSAGE_SIGNATURES = (
    "name or service not known",
    "nodename nor servname provided",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "failed to resolve",
    "name resolution",
    "connection reset",
    "connection aborted",
    "connection refused",
    "max retries exceeded",
    "read timed out",
    "connect timeout",
    "connection timed out",
    "network is unreachable",
    "remote end closed connection",
    "eof occurred in violation of protocol",
    "server disconnected",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection broken",
)

_TRANSIENT_EXCEPTION_TYPES = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    socket.timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _http_status_from_exception(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None
    if status_code is None:
        return None
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return None


def _classify(exc: BaseException, *, _depth: int = 0) -> Optional[str]:
    """Return a short reason string if `exc` is a transient network failure
    that should be retried forever, or None if it should propagate as a real
    error (auth failure, malformed request, programming error, ...).
    """
    if _depth > 5:
        return None

    status_code = _http_status_from_exception(exc)
    if status_code is not None:
        if 500 <= status_code < 600:
            return f"HTTP{status_code}"
        return None  # 4xx (401/403/malformed request/...) is explicit, not transient

    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return type(exc).__name__

    message = str(exc).lower()
    for signature in _TRANSIENT_MESSAGE_SIGNATURES:
        if signature in message:
            return signature.replace(" ", "_")

    for chained in (exc.__cause__, exc.__context__):
        if chained is not None:
            reason = _classify(chained, _depth=_depth + 1)
            if reason is not None:
                return reason

    return None


def call_with_network_retry(
    func: Callable[[], T],
    *,
    service: str,
    logger: logging.Logger = LOGGER,
) -> T:
    """Call func(), retrying forever with capped backoff on transient network
    failures. Non-network exceptions (auth, malformed request, programming
    errors) propagate immediately on the first attempt.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except Exception as exc:
            reason = _classify(exc)
            if reason is None:
                raise
            wait_seconds = _BACKOFF_SCHEDULE_SECONDS[
                min(attempt - 1, len(_BACKOFF_SCHEDULE_SECONDS) - 1)
            ]
            logger.warning(
                "NETWORK RETRY service=%s attempt=%s retry_in=%ss reason=%s",
                service,
                attempt,
                wait_seconds,
                reason,
            )
            time.sleep(wait_seconds)
