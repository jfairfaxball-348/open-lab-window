"""The guarantees that make the Window an observer and nothing more.

Three independent checks that it cannot write, one that it offers nothing but
reads over HTTP, one that it is reachable only from this machine, and one that
it shares no code with the Builder or the Simulation.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import http.client
import io
import os
from pathlib import Path

import pytest

import open_lab_window
from conftest import MODEL_CHECK, PNG, SMOKE, run_url
from open_lab_window import server as server_module
from open_lab_window.archive import Library
from open_lab_window.server import JSON_ROUTES, WindowHandler, make_server

SOURCE = Path(open_lab_window.__file__).parent


def tree_digest(root: Path) -> str:
    """One digest over every path under ``root`` and every file's bytes."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def browse_everything(client, runs) -> int:
    """Request every kind of page for every run. Returns how many requests were made."""
    requests = 0

    def get(path: str, expect: int = 200) -> None:
        nonlocal requests
        assert client.request(path)[0] == expect, path
        assert client.request(path, method="HEAD")[0] == expect, path
        requests += 2

    for path in ("/", "/static/app.js", "/static/style.css", "/api/info", "/api/runs"):
        get(path)
    for run in runs:
        base = run_url(run)
        for suffix in ("", "/events", "/events?tail=1&limit=5", "/events?types=tool.call&after=2",
                       "/tools", "/artifacts", "/integrity"):
            get(base + suffix)
        for artifact in client.json(base + "/artifacts")["artifacts"]:
            get(f"{base}/artifacts/{artifact['artifact_id']}/preview")
            client.request(f"{base}/artifacts/{artifact['artifact_id']}/image")
            requests += 1
    get("/api/runs/none/none", 404)
    return requests


def populated(writer) -> list[tuple[str, str]]:
    finished = writer("synthetic", "run-20261114t030000z-aaaaaa")
    finished.start()
    finished.artifact("a.txt", b"text")
    finished.artifact("a.png", PNG)
    finished.finish()
    live = writer("synthetic", "run-20261114t040000z-bbbbbb")
    live.start()
    live.append_raw(b'{"half":')
    return [SMOKE, MODEL_CHECK, (finished.experiment_id, finished.run_id),
            (live.experiment_id, live.run_id)]


def test_archive_bytes_are_unchanged_by_browsing(client, data_root, writer):
    runs = populated(writer)
    before = tree_digest(data_root)
    listing = sorted(path.as_posix() for path in data_root.rglob("*"))
    assert browse_everything(client, runs) > 60
    assert tree_digest(data_root) == before
    # No file or directory appeared either: no cache, index or lock file.
    assert sorted(path.as_posix() for path in data_root.rglob("*")) == listing


def test_every_file_is_opened_read_only(client, data_root, writer, monkeypatch):
    runs = populated(writer)
    opened: list[tuple[str, str]] = []
    low_level: list[int] = []
    real_open, real_os_open = builtins.open, os.open

    def recording_open(file, mode="r", *args, **kwargs):
        opened.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    def recording_os_open(path, flags, *args, **kwargs):
        low_level.append(flags)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    monkeypatch.setattr(os, "open", recording_os_open)
    browse_everything(client, runs)
    monkeypatch.undo()

    in_data_root = [mode for file, mode in opened if str(data_root) in file]
    assert len(in_data_root) > 40
    assert {mode for _, mode in opened} == {"rb"}
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    assert all(flags & write_flags == 0 for flags in low_level)


def test_source_contains_nothing_that_could_write_or_reach_out():
    """A scan of the Window's own code: one read-only open, no writers, no clients."""
    writers = {"write_text", "write_bytes", "mkdir", "makedirs", "unlink", "rmdir", "rename",
               "renames", "remove", "rmtree", "touch", "chmod", "truncate", "symlink_to",
               "hardlink_to", "copy", "copy2", "copyfile", "copytree", "move", "connect",
               "urlopen", "Popen", "system", "startfile"}
    banned_modules = {"sqlite3", "shutil", "subprocess", "socket", "tempfile", "urllib.request",
                      "http.client", "requests", "multiprocessing", "ctypes", "pickle", "shelve",
                      "dbm", "logging", "open_lab_builder", "open_lab_simulation"}
    opens = []
    files = sorted(SOURCE.glob("*.py"))
    assert len(files) >= 4
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                assert not any(module == banned or module.startswith(banned + ".")
                               for banned in banned_modules), f"{file.name} imports {module}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in writers, f"{file.name} uses .{node.attr}"
                # Path.open and os.open could be given a writing mode.
                assert node.attr != "open", f"{file.name} uses .open"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    opens.append((file.name, [ast.literal_eval(arg) for arg in node.args[1:]],
                                  node.keywords))
    assert opens == [("archive.py", ["rb"], [])]


def test_only_get_and_head_are_answered(client):
    paths = ["/", "/api/runs", run_url(SMOKE), run_url(SMOKE) + "/events",
             run_url(SMOKE) + "/tools", run_url(SMOKE) + "/artifacts",
             run_url(SMOKE) + "/integrity", "/api/anything/else"]
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
        for path in paths:
            body = b'{"action":"stop"}' if method in ("POST", "PUT", "PATCH") else None
            status, headers, _ = client.request(path, method=method, body=body)
            assert status == 405, (method, path)
            assert headers["Allow"] == "GET, HEAD"
    status, _, body = client.request("/api/runs", method="HEAD")
    assert status == 200 and body == b""


def test_unknown_methods_are_refused(client):
    host, port = client.base.removeprefix("http://").split(":")
    for method in ("CONNECT", "PROPFIND", "MKCOL", "COPY", "MOVE", "LOCK", "BREW"):
        connection = http.client.HTTPConnection(host, int(port), timeout=10)
        connection.request(method, "/api/runs")
        response = connection.getresponse()
        assert response.status in (405, 501), method
        response.read()
        connection.close()


def test_handler_defines_no_other_method_and_routes_only_through_get():
    def is_refusal(name: str) -> bool:
        return getattr(WindowHandler, name) is WindowHandler._method_not_allowed

    handlers = {name for name in dir(WindowHandler) if name.startswith("do_")}
    assert {name for name in handlers if not is_refusal(name)} == {"do_GET", "do_HEAD"}
    assert {"do_POST", "do_PUT", "do_PATCH", "do_DELETE"} <= handlers
    # The route table is consulted in exactly one place, reached from GET and HEAD.
    source = (SOURCE / "server.py").read_text(encoding="utf-8")
    assert source.count("in JSON_ROUTES") == 1 and source.count("self._respond(") == 2
    assert len(JSON_ROUTES) == 8
    # A request body is touched in one place only: where it is thrown away.
    assert source.count("rfile") == 1 and "self.rfile.read(length)" in source
    assert "json.loads" not in source


def test_server_binds_to_loopback_only(data_root):
    server = make_server(Library(data_root), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    # There is no parameter or option that could change the address.
    assert "host" not in make_server.__code__.co_varnames
    cli = (SOURCE / "cli.py").read_text(encoding="utf-8")
    assert "--host" not in cli and "--bind" not in cli and "0.0.0.0" not in cli
    assert server_module.LOOPBACK == "127.0.0.1"


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1:{port}", 200), ("localhost:{port}", 200), ("127.0.0.1", 200),
    ("evil.example:{port}", 403), ("evil.example", 403), ("192.168.1.5:{port}", 403), ("", 403),
])
def test_requests_for_another_host_name_are_refused(client, host, expected):
    port = client.base.rsplit(":", 1)[1]
    status, _, body = client.request("/api/runs", headers={"Host": host.format(port=port)})
    assert status == expected
    if expected == 403:
        assert b"runs" not in body


def test_no_builder_or_simulation_code_and_no_schemas_in_source():
    for file in SOURCE.rglob("*"):
        assert not file.name.endswith(".schema.json"), file
        if file.suffix in (".py", ".js", ".html"):
            text = file.read_text(encoding="utf-8")
            assert "open_lab_builder" not in text and "open_lab_simulation" not in text, file
