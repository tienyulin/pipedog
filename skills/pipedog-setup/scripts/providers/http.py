"""Thin urllib wrapper shared by the providers.

All failures are raised as :class:`HttpError` with a short, human readable
message that never contains the credentials passed in ``headers``.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

_USER_AGENT = "pipedog"
_MAX_TEXT_BYTES = 4 * 1024 * 1024  # keep only the tail of huge logs
_AUTH_HEADERS = ("authorization", "private-token")

RATE_LIMITS: dict[str, dict] = {}


class HttpError(Exception):
    """Network / HTTP / decoding failure. ``status`` is None for non-HTTP errors."""

    def __init__(self, message: str | None = None, status: int | None = None) -> None:
        if message is None and isinstance(status, str):
            message, status = status, None
        super().__init__(message or "")
        self.status = status
        self.message = message or ""


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects but never forward credentials to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            for store in (new.headers, new.unredirected_hdrs):
                for key in list(store):
                    if key.lower() in _AUTH_HEADERS:
                        del store[key]
        return new


_OPENER = urllib.request.build_opener(_RedirectHandler)


def _scrub(message: str, headers: dict[str, str]) -> str:
    """Defensively remove any credential-looking header value from a message."""
    for value in headers.values():
        for part in str(value).split():
            if len(part) >= 8:
                message = message.replace(part, "***")
    return message


def _header_val(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get"):
        val = headers.get(name)
        if val is not None:
            return str(val)
        target = name.lower()
        if hasattr(headers, "items"):
            for k, v in headers.items():
                if str(k).lower() == target:
                    return str(v)
    return None


def _record_rate_limit(url: str, resp_headers: Any) -> None:
    if resp_headers is None:
        return
    rem_val = _header_val(resp_headers, "X-RateLimit-Remaining")
    if rem_val is None:
        rem_val = _header_val(resp_headers, "RateLimit-Remaining")
    if rem_val is None:
        return
    try:
        remaining = int(rem_val)
    except (ValueError, TypeError):
        return

    lim_val = _header_val(resp_headers, "X-RateLimit-Limit")
    if lim_val is None:
        lim_val = _header_val(resp_headers, "RateLimit-Limit")
    limit = None
    if lim_val is not None:
        try:
            limit = int(lim_val)
        except (ValueError, TypeError):
            pass

    reset_val = _header_val(resp_headers, "X-RateLimit-Reset")
    if reset_val is None:
        reset_val = _header_val(resp_headers, "RateLimit-Reset")
    reset = None
    if reset_val is not None:
        try:
            reset = float(reset_val)
        except (ValueError, TypeError):
            pass

    host = urlsplit(url).netloc
    RATE_LIMITS[host] = {
        "remaining": remaining,
        "limit": limit,
        "reset": reset,
        "at": time.time(),
    }


def _open(
    url: str,
    headers: dict[str, str],
    timeout: float,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[bytes, Any]:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": _USER_AGENT, **headers},
        method=method,
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            _record_rate_limit(url, resp.headers)
            return resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        _record_rate_limit(url, e.headers)
        phrase = e.reason or _phrase(e.code)
        raise HttpError(_scrub(f"HTTP {e.code} {phrase}".strip(), headers), e.code) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise HttpError("timeout") from None
        raise HttpError(_scrub(f"URLError: {e.reason}", headers)) from None
    except (TimeoutError, socket.timeout):
        raise HttpError("timeout") from None
    except (http.client.HTTPException, OSError, ValueError) as e:
        raise HttpError(_scrub(f"{type(e).__name__}: {e}", headers)) from None


def _phrase(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""


def get_json(url: str, headers: dict[str, str], timeout: float = 10) -> Any:
    """GET ``url`` and decode the JSON body."""
    body, _ = _open(url, headers, timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError("invalid JSON response") from None


def get_json_with_headers(
    url: str, headers: dict[str, str], timeout: float = 10
) -> tuple[Any, dict[str, str]]:
    """GET ``url`` and decode the JSON body, returning (body, lower_cased_headers)."""
    body, resp_headers = _open(url, headers, timeout)
    hdrs: dict[str, str] = {}
    if resp_headers is not None:
        if hasattr(resp_headers, "items"):
            hdrs = {str(k).lower(): str(v) for k, v in resp_headers.items()}
        elif hasattr(resp_headers, "keys"):
            hdrs = {str(k).lower(): str(resp_headers[k]) for k in resp_headers.keys()}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError("invalid JSON response") from None
    return data, hdrs


def post_json(url: str, headers: dict[str, str], body: Any, timeout: float = 15) -> Any:
    """POST ``body`` as JSON to ``url`` and decode the JSON response."""
    data = json.dumps(body).encode("utf-8")
    req_headers = {"Content-Type": "application/json", **headers}
    raw, _ = _open(url, req_headers, timeout, data=data, method="POST")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError("invalid JSON response") from None


def put_json(url: str, headers: dict[str, str], body: Any, timeout: float = 15) -> Any:
    """PUT ``body`` as JSON to ``url`` and decode the JSON response."""
    data = json.dumps(body).encode("utf-8")
    req_headers = {"Content-Type": "application/json", **headers}
    raw, _ = _open(url, req_headers, timeout, data=data, method="PUT")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpError("invalid JSON response") from None


def delete(url: str, headers: dict[str, str], timeout: float = 15) -> Any:
    """DELETE ``url``."""
    raw, _ = _open(url, headers, timeout, method="DELETE")
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def post_graphql(
    url: str,
    headers: dict[str, str],
    query: str,
    variables: dict,
    timeout: float = 15,
) -> dict:
    """POST a GraphQL query to ``url`` and return the ``data`` field."""
    res = post_json(url, headers, {"query": query, "variables": variables}, timeout=timeout)
    if not isinstance(res, dict):
        raise HttpError("invalid GraphQL response")
    errors = res.get("errors")
    if errors:
        msg = "unknown error"
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                msg = first.get("message", "unknown error")
            else:
                msg = str(first)
        raise HttpError(f"GraphQL: {msg}")
    data = res.get("data")
    return data if isinstance(data, dict) else {}


def get_text(url: str, headers: dict[str, str], timeout: float = 20) -> str:
    """GET ``url`` as text, following redirects (GitHub job logs answer 302).

    Bodies larger than a few MiB are truncated to their tail, which is the
    part that holds the failure.
    """
    body, _ = _open(url, headers, timeout)
    if len(body) > _MAX_TEXT_BYTES:
        body = body[-_MAX_TEXT_BYTES:]
    return body.decode("utf-8", errors="replace")


def parse_iso(value: str | None) -> float | None:
    """Parse an ISO 8601 timestamp into epoch seconds (naive values are UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
