import gzip
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kgserve import rdffiles
from kgserve.proxy import SwitchProxy
from kgserve.qlever import QLeverBackend, convert_command, index_command, plan_inputs

NQ = b'<http://ex.org/s> <http://ex.org/p> "o" <http://ex.org/g> .\n'
TTL = b"<http://ex.org/a> <http://ex.org/p> <http://ex.org/b> .\n"
graph_for = lambda key: f"urn:graph:{key}"  # noqa: E731


def make_items(tmp_path: Path) -> list[rdffiles.LoadItem]:
    (tmp_path / "kg.nq.gz").write_bytes(gzip.compress(NQ))
    (tmp_path / "void.ttl").write_bytes(TTL)
    (tmp_path / "onto.rdf").write_text("<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>")
    (tmp_path / "extra.trig").write_text("<urn:g> { <urn:a> <urn:b> <urn:c> . }")
    items = []
    for key in ("kg.nq.gz", "void.ttl", "onto.rdf", "extra.trig"):
        items += rdffiles.load_items(key, tmp_path / key, graph_for)
    return items


def test_plan_inputs(tmp_path):
    work = tmp_path / "work"
    gz, ttl, rdf, trig = plan_inputs(make_items(tmp_path), work)

    assert (gz.format, gz.graph, gz.piped, gz.convert) == ("nq", "-", True, False)  # decompressed
    assert gz.path == work / "input-000.nq"
    assert (ttl.format, ttl.graph, ttl.piped, ttl.convert) == ("ttl", "urn:graph:void.ttl", False, False)
    assert ttl.path == tmp_path / "void.ttl"  # read directly
    assert not ttl.parallel and gz.parallel
    assert (rdf.format, rdf.graph, rdf.piped, rdf.convert) == ("nq", "-", True, True)
    assert convert_command("oxigraph", rdf.item) == [
        "oxigraph", "convert", "--from-format", "rdf", "--to-format", "nq", "--to-graph", "urn:graph:onto.rdf"]
    assert convert_command("oxigraph", trig.item) == [
        "oxigraph", "convert", "--from-format", "trig", "--to-format", "nq"]  # keeps its graphs


def test_index_command(tmp_path):
    inputs = plan_inputs(make_items(tmp_path)[:2], tmp_path / "work")
    cmd = index_command("qlever-index", tmp_path / "store" / "kg", inputs, "t1", memory="10G")
    assert cmd == [
        "qlever-index", "-i", str(tmp_path / "store" / "kg"), "-K", "t1",
        "-f", str(tmp_path / "work" / "input-000.nq"), "-F", "nq", "-g", "-", "-p", "true",
        "-f", str(tmp_path / "void.ttl"), "-F", "ttl", "-g", "urn:graph:void.ttl", "-p", "false",
        "-m", "10G",
    ]


FAKE_INDEX = """\
import sys
args = sys.argv[1:]
basename = args[args.index("-i") + 1]
files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
if {fail}:
    sys.exit(3)
with open(basename + ".out", "wb") as out:
    for f in files:
        with open(f, "rb") as src:
            out.write(src.read())
"""


def fake_backend(tmp_path: Path, monkeypatch, fail: bool = False) -> QLeverBackend:
    script = tmp_path / "fake_index.py"
    script.write_text(FAKE_INDEX.format(fail=fail))
    launcher = tmp_path / "qlever-index"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("QLEVER_INDEX_BIN", str(launcher))
    monkeypatch.setenv("QLEVER_BIND", "127.0.0.1:0")
    backend = QLeverBackend(tmp_path / "data")
    return backend


def test_build_index_streams_through_pipes(tmp_path, monkeypatch):
    backend = fake_backend(tmp_path, monkeypatch)
    try:
        items = make_items(tmp_path)[:2]  # gz (piped) + ttl (direct): no oxigraph needed
        target = tmp_path / "data" / "stores" / "t1"
        target.mkdir(parents=True)
        backend.build_index(items, target, "t1")
        assert (target / "kg.out").read_bytes() == NQ + TTL
        assert not list(target.glob("input-*"))  # pipes removed
    finally:
        backend.shutdown()


@pytest.mark.skipif(shutil.which("oxigraph") is None, reason="needs the oxigraph binary")
def test_build_index_converts_formats(tmp_path, monkeypatch):
    backend = fake_backend(tmp_path, monkeypatch)
    try:
        items = make_items(tmp_path)[3:]  # TriG -> N-Quads
        target = tmp_path / "data" / "stores" / "t1"
        target.mkdir(parents=True)
        backend.build_index(items, target, "t1")
        assert (target / "kg.out").read_bytes().strip() == b"<urn:a> <urn:b> <urn:c> <urn:g> ."
    finally:
        backend.shutdown()


def test_build_index_failure_does_not_hang(tmp_path, monkeypatch):
    backend = fake_backend(tmp_path, monkeypatch, fail=True)
    try:
        target = tmp_path / "data" / "stores" / "t1"
        target.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="qlever-index failed"):
            backend.build_index(make_items(tmp_path)[:2], target, "t1")
        assert not list(target.glob("input-*"))
    finally:
        backend.shutdown()


def serve_text(text: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def fetch(port: int) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def test_switch_proxy():
    a, b = serve_text("A"), serve_text("B")
    proxy = SwitchProxy("127.0.0.1", 0)
    proxy.start()
    try:
        assert fetch(proxy.port)[0] == 503  # nothing to forward to yet
        proxy.switch(("127.0.0.1", a.server_address[1]))
        assert fetch(proxy.port) == (200, "A")
        proxy.switch(("127.0.0.1", b.server_address[1]))
        assert fetch(proxy.port) == (200, "B")
        assert proxy.drain(("127.0.0.1", a.server_address[1]), 5)
        proxy.switch(("127.0.0.1", 1))  # nothing listens there
        assert fetch(proxy.port)[0] == 503
    finally:
        proxy.stop()
        a.shutdown()
        b.shutdown()
