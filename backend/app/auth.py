"""Who may talk to the local backend, and how it ends with the app that started it.

The backend listens on loopback, where any web page the user visits can send it
"simple" requests (form posts, ``text/plain`` bodies, multipart uploads) and, through
DNS rebinding, even read its answers. So:

* Requests a browser made are refused everywhere: they carry ``Origin`` or a
  ``Sec-Fetch-Site`` other than ``none``. The native app's URLSession sends neither.
* With ``FORTRESS_TOKEN`` set (the Mac app sets a fresh random one per launch), every
  request needs it, as ``X-Fortress-Token: <token>`` or ``Authorization: Bearer
  <token>``; only ``GET /api/health`` is open. A custom header also means a browser
  can't send it cross-site without a CORS preflight, which is refused.
* Without a token (tests, a backend started by hand): the ``Host`` must be a loopback
  name (``FORTRESS_ALLOWED_HOSTS`` adds more, comma separated), which stops DNS
  rebinding, and importing OneNote by server-side path is refused (uploads still work).

Every answer carries ``X-Fortress-Instance``: a hash of the token (or a random id per
process without one), so the app can tell its own backend from any other server on
the port without revealing the token.

When ``FORTRESS_PARENT_PID`` is set the backend stops itself once that process is
gone (the app crashed or was killed), and on shutdown it stops running OneNote
converters, so no backend or converter outlives the app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import signal
import threading
import time
import uuid

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_ENV = "FORTRESS_TOKEN"
PARENT_ENV = "FORTRESS_PARENT_PID"
ALLOWED_HOSTS_ENV = "FORTRESS_ALLOWED_HOSTS"
TOKEN_HEADER = "x-fortress-token"
INSTANCE_HEADER = "x-fortress-instance"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_PROCESS_INSTANCE = uuid.uuid4().hex[:16]
_log = logging.getLogger("fortress.auth")


def configured_token() -> str | None:
    """The token from the environment (read per request, so tests can switch it)."""

    return os.environ.get(TOKEN_ENV) or None


def instance_id(token: str | None) -> str:
    """What ``X-Fortress-Instance`` says: the first 16 hex digits of the token's SHA-256."""

    if not token:
        return _PROCESS_INSTANCE
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _presented_token(headers: Headers) -> str | None:
    if value := headers.get(TOKEN_HEADER):
        return value
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials:
        return credentials.strip()
    return None


def _host_name(headers: Headers) -> str:
    host = headers.get("host", "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _allowed_hosts() -> set[str]:
    extra = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return set(LOOPBACK_HOSTS) | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _is_open(method: str, path: str) -> bool:
    return path == "/api/health" and method in {"GET", "HEAD"}


def _from_browser(headers: Headers) -> bool:
    if "origin" in headers:
        return True
    site = headers.get("sec-fetch-site")
    return site is not None and site != "none"


def _is_path_import(method: str, path: str, headers: Headers) -> bool:
    return (
        method == "POST"
        and path.rstrip("/") == "/api/import/onenote"
        and not headers.get("content-type", "").startswith("multipart/form-data")
    )


class LocalAuthMiddleware:
    """Plain ASGI (no body buffering, streams pass through). Register it last so it is
    the outermost middleware and answers before CORS or any route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, _lifespan_receive(receive), send)
            return
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        token = configured_token()
        instance = instance_id(token)
        headers = Headers(scope=scope)
        method = scope.get("method", "GET")
        path = scope["path"]

        refusal = self._refusal(token, headers, method, path)
        if refusal is not None:
            status, detail = refusal
            await _deny(scope, receive, send, status, detail, instance, bearer=status == 401)
            return

        async def send_with_instance(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (INSTANCE_HEADER.encode(), instance.encode())
                )
            await send(message)

        await self.app(scope, receive, send_with_instance)

    @staticmethod
    def _refusal(token: str | None, headers: Headers, method: str, path: str) -> tuple[int, str] | None:
        if _from_browser(headers):
            return 403, "Browser requests are not accepted."
        if token is None:
            if _host_name(headers) not in _allowed_hosts():
                return 403, "Unknown host."
            if _is_path_import(method, path, headers):
                return 403, "Importing by path needs the app's token; upload the file instead."
            return None
        if _is_open(method, path):
            return None
        presented = _presented_token(headers)
        if presented is None or not hmac.compare_digest(presented.encode(), token.encode()):
            return 401, "Missing or wrong token."
        return None


async def _deny(scope: Scope, receive: Receive, send: Send, status: int, detail: str,
                instance: str, *, bearer: bool) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (INSTANCE_HEADER.encode(), instance.encode()),
    ]
    if bearer:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# --- Lifecycle -------------------------------------------------------------------------


def _lifespan_receive(receive: Receive) -> Receive:
    async def wrapped() -> Message:
        message = await receive()
        if message["type"] == "lifespan.startup":
            start_parent_watch()
        elif message["type"] == "lifespan.shutdown":
            stop_children()
        return message

    return wrapped


def stop_children() -> None:
    """Stops OneNote converters still running, so shutdown doesn't wait on them."""

    from . import onenote_import  # late: keep this module free of app imports

    onenote_import.terminate_converters()


_watch_started = False


def start_parent_watch(interval: float = 1.0) -> threading.Thread | None:
    """With ``FORTRESS_PARENT_PID``, ends this process (SIGTERM, then a hard exit) once
    that process is gone."""

    global _watch_started
    raw = os.environ.get(PARENT_ENV, "")
    if _watch_started or not raw.isdigit():
        return None
    parent = int(raw)
    _watch_started = True

    def watch() -> None:
        while _alive(parent):
            time.sleep(interval)
        _log.warning("the app (pid %s) is gone; stopping the backend", parent)
        stop_children()
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(15)
        os._exit(1)

    thread = threading.Thread(target=watch, name="fortress-parent-watch", daemon=True)
    thread.start()
    return thread


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
