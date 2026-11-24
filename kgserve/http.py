"""Small HTTP helpers on top of the standard library (no third-party dependencies)."""

from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import IO

from . import USER_AGENT


class HTTPError(Exception):
    def __init__(self, method: str, url: str, status: int, body: bytes):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500].decode('utf-8', 'replace')}")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def request(
    method: str,
    url: str,
    body: bytes | IO[bytes] | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    timeout: float = 3600,
    ok: tuple[int, ...] = (200, 201, 204),
) -> Response:
    """Send a request; a file-like ``body`` is streamed with chunked transfer encoding."""
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.netloc, timeout=timeout)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if auth and auth[0]:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        hdrs["Authorization"] = f"Basic {token}"
    streaming = body is not None and not isinstance(body, (bytes, bytearray))
    try:
        conn.request(method, path, body=body, headers=hdrs, encode_chunked=streaming)
        resp = conn.getresponse()
        data = resp.read()
        result = Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
    finally:
        conn.close()
    if result.status not in ok:
        raise HTTPError(method, url, result.status, result.body)
    return result


def with_params(url: str, **params: str) -> str:
    """Append query parameters to a URL that may already have some (kept as they are). An
    empty value gives a bare key, as in the Graph Store Protocol's ``?default``."""
    added = "&".join(urllib.parse.quote(k, safe="") + (f"={urllib.parse.quote(v, safe='')}" if v else "")
                     for k, v in params.items())
    if not added:
        return url
    return f"{url}{'&' if '?' in url else '?'}{added}"


def form(fields: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    return urllib.parse.urlencode(fields).encode(), {"Content-Type": "application/x-www-form-urlencoded"}


def sparql(
    endpoint: str,
    query: str,
    accept: str = "application/sparql-results+json",
    auth: tuple[str, str] | None = None,
    timeout: float = 120,
) -> Response:
    body, headers = form({"query": query})
    headers["Accept"] = accept
    return request("POST", endpoint, body, headers, auth=auth, timeout=timeout, ok=(200,))


def select(endpoint: str, query: str, auth: tuple[str, str] | None = None, timeout: float = 120) -> list[dict]:
    """Run a SELECT query, returning the SPARQL JSON result bindings."""
    resp = sparql(endpoint, query, auth=auth, timeout=timeout)
    return json.loads(resp.body)["results"]["bindings"]


def wait_until_up(url: str, timeout: float = 300, auth: tuple[str, str] | None = None) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            request("GET", url, auth=auth, timeout=10, ok=tuple(range(200, 500)))
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)
