"""A loopback-only HTTP server that answers GET and HEAD and nothing else.

There is no authentication, so the access control is the address: the server
binds to 127.0.0.1 and offers no way to bind anywhere else. It also refuses
requests whose Host header is not a loopback name, so a web page elsewhere
cannot reach it by pointing a host name at 127.0.0.1.

Every route reads; none has a side effect. Methods other than GET and HEAD are
answered 405 before any routing happens; a body sent with one is discarded
unparsed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .archive import BadRequest, Library, NotFound, open_readonly

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
ALLOWED_METHODS = ("GET", "HEAD")
MAX_DISCARDED_BODY = 1 << 20

_STATIC_DIR = Path(__file__).parent / "static"
# The whole front end. Anything not listed here is not served.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Archive content is agent-produced. The page may load only this server's own
# script and style, and nothing it displays can run.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; media-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_RUN = r"/api/runs/(?P<experiment_id>[^/]+)/(?P<run_id>[^/]+)"
_Route = Callable[[Library, dict[str, str], dict[str, list[str]]], Any]


def _integer(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(query[name][-1]) if name in query else default
    except ValueError:
        raise BadRequest(f"{name} must be an integer") from None


def _info(library: Library, _: dict[str, str], __: dict[str, list[str]]) -> Any:
    return {
        "name": "open-lab-window",
        "version": __version__,
        "read_only": True,
        "allowed_methods": list(ALLOWED_METHODS),
        "runs_directory_exists": library.runs_root.is_dir(),
    }


def _runs(library: Library, _: dict[str, str], __: dict[str, list[str]]) -> Any:
    return {"runs": library.list_runs()}


def _run(library: Library, path: dict[str, str], _: dict[str, list[str]]) -> Any:
    return library.run_detail(path["experiment_id"], path["run_id"])


def _events(library: Library, path: dict[str, str], query: dict[str, list[str]]) -> Any:
    types = None
    if "types" in query:
        types = frozenset(part for part in query["types"][-1].split(",") if part)
    return library.events(
        path["experiment_id"],
        path["run_id"],
        after=_integer(query, "after", -1),
        limit=_integer(query, "limit", 100),
        types=types,
        tail=query.get("tail", ["0"])[-1] in ("1", "true"),
    )


def _tools(library: Library, path: dict[str, str], query: dict[str, list[str]]) -> Any:
    return library.tool_calls(
        path["experiment_id"],
        path["run_id"],
        offset=_integer(query, "offset", 0),
        limit=_integer(query, "limit", 100),
    )


def _artifacts(library: Library, path: dict[str, str], query: dict[str, list[str]]) -> Any:
    return library.artifacts(
        path["experiment_id"],
        path["run_id"],
        offset=_integer(query, "offset", 0),
        limit=_integer(query, "limit", 100),
    )


def _artifact_preview(library: Library, path: dict[str, str], _: dict[str, list[str]]) -> Any:
    return library.artifact_preview(path["experiment_id"], path["run_id"], path["artifact_id"])


def _integrity(library: Library, path: dict[str, str], _: dict[str, list[str]]) -> Any:
    return library.integrity(path["experiment_id"], path["run_id"])


# Every JSON endpoint. All are reached through do_GET only.
JSON_ROUTES: tuple[tuple[re.Pattern[str], _Route], ...] = (
    (re.compile(r"/api/info"), _info),
    (re.compile(r"/api/runs"), _runs),
    (re.compile(_RUN), _run),
    (re.compile(_RUN + r"/events"), _events),
    (re.compile(_RUN + r"/tools"), _tools),
    (re.compile(_RUN + r"/artifacts"), _artifacts),
    (re.compile(_RUN + r"/artifacts/(?P<artifact_id>[^/]+)/preview"), _artifact_preview),
    (re.compile(_RUN + r"/integrity"), _integrity),
)
IMAGE_ROUTE = re.compile(_RUN + r"/artifacts/(?P<artifact_id>[^/]+)/image")
MEDIA_ROUTE = re.compile(_RUN + r"/artifacts/(?P<artifact_id>[^/]+)/media")


class WindowHandler(BaseHTTPRequestHandler):
    server_version = f"open-lab-window/{__version__}"
    protocol_version = "HTTP/1.1"
    library: Library
    quiet = True

    # -- the only two methods that do anything -----------------------------

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    # -- everything else ---------------------------------------------------

    def _method_not_allowed(self) -> None:
        # Nothing a client sends is ever parsed, let alone acted on. A small
        # body is drained and thrown away only so that closing the connection
        # does not reset it before the client has read the refusal.
        self.close_connection = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= MAX_DISCARDED_BODY:
            self.rfile.read(length)
        self._send(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "application/json; charset=utf-8",
            _json({"error": "The Window is read-only. Only GET and HEAD are allowed."}),
            extra={"Allow": ", ".join(ALLOWED_METHODS), "Connection": "close"},
        )

    do_POST = do_PUT = do_PATCH = do_DELETE = _method_not_allowed
    do_OPTIONS = do_TRACE = do_CONNECT = _method_not_allowed

    # -- plumbing ----------------------------------------------------------

    def _respond(self, *, send_body: bool) -> None:
        if not self._host_is_loopback():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Host not allowed."}, send_body)
            return
        url = urlsplit(self.path)
        path = url.path
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            with open_readonly(_STATIC_DIR / name) as handle:
                self._send(HTTPStatus.OK, content_type, handle.read(), send_body=send_body)
            return
        try:
            match = MEDIA_ROUTE.fullmatch(path)
            if match:
                content_type, stored = self.library.artifact_media(**match.groupdict())
                self._stream_media(stored, content_type, send_body)
                return
            match = IMAGE_ROUTE.fullmatch(path)
            if match:
                media_type, stored = self.library.artifact_image(**match.groupdict())
                with open_readonly(stored) as handle:
                    self._send(HTTPStatus.OK, media_type, handle.read(), send_body=send_body)
                return
            for pattern, route in JSON_ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    document = route(self.library, match.groupdict(), parse_qs(url.query))
                    self._send_json(HTTPStatus.OK, document, send_body)
                    return
            raise NotFound("no such page")
        except NotFound as error:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)}, send_body)
        except BadRequest as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)}, send_body)
        except OSError:
            # A file vanished or could not be read between listing and opening.
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "The archive could not be read just now."},
                send_body,
            )

    def _stream_media(self, stored: Path, content_type: str, send_body: bool) -> None:
        size = stored.stat().st_size
        start, end = 0, size - 1
        request = self.headers.get("Range")
        if request:
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", request)
            valid = (match and any(match.groups()) and len(request) <= 80
                     and len(self.headers.get_all("Range", [])) == 1)
            if valid:
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    suffix = int(right)
                    start = max(size - suffix, 0)
                    valid = suffix > 0
            if not valid or not 0 <= start <= end < size:
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, content_type, b"",
                           send_body=send_body, extra={"Content-Range": f"bytes */{size}"})
                return
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if request else HTTPStatus.OK)
        headers = {**_SECURITY_HEADERS, "Content-Type": content_type,
                   "Content-Length": str(length), "Accept-Ranges": "bytes"}
        if request:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            with open_readonly(stored) as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def _host_is_loopback(self) -> bool:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in (LOOPBACK, "localhost")

    def _send_json(self, status: HTTPStatus, document: Any, send_body: bool) -> None:
        self._send(status, "application/json; charset=utf-8", _json(document), send_body=send_body)

    def _send(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        *,
        send_body: bool = True,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Request logging goes to stderr only when asked for; never to a file.
        if not self.quiet:
            super().log_message(format, *args)


def _json(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def make_server(library: Library, port: int = DEFAULT_PORT, *, quiet: bool = True):
    """A server for ``library`` on the loopback address. The address is not a parameter."""
    handler = type("BoundWindowHandler", (WindowHandler,), {"library": library, "quiet": quiet})
    server = ThreadingHTTPServer((LOOPBACK, port), handler)
    server.daemon_threads = True
    return server
