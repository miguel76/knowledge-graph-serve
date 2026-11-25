import gzip
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kgserve import rdffiles
from kgserve.backends import Store, with_params
from kgserve.config import StoreConfig


def test_with_params():
    assert with_params("http://s/store?no_transaction", graph="urn:g#x") == "http://s/store?no_transaction&graph=urn%3Ag%23x"
    assert with_params("http://s/data", default="") == "http://s/data?default"
    assert with_params("http://s/t/1", action="ADD", context="<urn:g>") == "http://s/t/1?action=ADD&context=%3Curn%3Ag%3E"
    assert with_params("http://s/data") == "http://s/data"


@pytest.fixture()
def recorder():
    """An HTTP server recording the requests it gets, answering like a SPARQL store."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _body(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                data = b""
                while (size := int(self.rfile.readline().strip(), 16)):
                    data += self.rfile.read(size)
                    self.rfile.readline()
                self.rfile.readline()
                return data
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def _reply(self, status=204, body=b"", content_type="text/plain"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(("GET", self.path, self.headers.get("Content-Type"), b""))
            self._reply(200, b'{"boolean": true}', "application/sparql-results+json")

        def do_POST(self):
            body = self._body()
            requests.append(("POST", self.path, self.headers.get("Content-Type"), body))
            if self.path.startswith("/query"):
                result = {"results": {"bindings": [{"n": {"type": "literal", "value": "7"}}]}}
                return self._reply(200, json.dumps(result).encode(), "application/sparql-results+json")
            self._reply()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", requests
    server.shutdown()


def test_gsp_load(tmp_path, recorder):
    url, requests = recorder
    nq = b'<urn:s> <urn:p> "o" <urn:g> .\n'
    ttl = b"<urn:a> <urn:b> <urn:c> .\n"
    (tmp_path / "kg.nq.gz").write_bytes(gzip.compress(nq))
    (tmp_path / "void.ttl").write_bytes(ttl)
    items = (rdffiles.load_items("kg.nq.gz", tmp_path / "kg.nq.gz", lambda key: f"urn:graph:{key}")
             + rdffiles.load_items("void.ttl", tmp_path / "void.ttl", lambda key: f"urn:graph:{key}"))
    store = Store(StoreConfig(type="oxigraph", query=f"{url}/query", update=f"{url}/update",
                              data=f"{url}/store?no_transaction"))

    info = store.load(items)

    posts = [r for r in requests if r[0] == "POST" and not r[1].startswith("/query")]
    assert posts[0][1] == "/update" and b"update=DROP+ALL" in posts[0][3]
    assert posts[1] == ("POST", "/store?no_transaction", "application/n-quads", nq)  # decompressed
    assert posts[2] == ("POST", "/store?no_transaction&graph=urn%3Agraph%3Avoid.ttl", "text/turtle", ttl)
    assert info["triples"] == 7


@pytest.fixture()
def fake_store():
    """An HTTP server answering with ``routes[(method, path)]`` -> (status, body), recording
    the requests (method, path with query string, body)."""
    requests, routes = [], {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self, method):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((method, self.path, body))
            path = self.path.split("?")[0]
            status, reply = routes.get((method, path), (404, b"not found"))
            reply = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", routes, requests
    server.shutdown()


def graphdb_listing(*ids):
    return 200, {"results": {"bindings": [{"id": {"type": "literal", "value": i}} for i in ids]}}


def test_graphdb_repository_with_ruleset(fake_store):
    url, routes, requests = fake_store
    store = Store(StoreConfig(type="graphdb", query=f"{url}/repositories/kg", data=url), inference="owl2-rl")
    assert store.query_endpoint == f"{url}/repositories/kg?infer=false"

    # Missing: created with the ruleset.
    routes[("GET", "/repositories")] = graphdb_listing()
    routes[("PUT", "/repositories/kg")] = (204, b"")
    assert store.check_inference() is False
    store._ensure_graphdb_repository()
    put = [r for r in requests if r[0] == "PUT"]
    assert len(put) == 1 and b'graphdb:ruleset "owl2-rl"' in put[0][2]

    # Another ruleset: recreated.
    requests.clear()
    routes[("GET", "/repositories")] = graphdb_listing("kg")
    routes[("GET", "/rest/repositories/kg")] = (200, {"id": "kg", "params": {"ruleset": {"name": "ruleset", "value": "empty"}}})
    routes[("DELETE", "/repositories/kg")] = (204, b"")
    assert store.check_inference() is False
    store._ensure_graphdb_repository()
    assert [r[0] for r in requests if r[0] in ("PUT", "DELETE")] == ["DELETE", "PUT"]

    # Same ruleset: kept.
    requests.clear()
    routes[("GET", "/rest/repositories/kg")] = (200, {"id": "kg", "params": {"ruleset": {"value": "owl2-rl"}}})
    assert store.check_inference() is True
    store._ensure_graphdb_repository()
    assert not [r for r in requests if r[0] in ("PUT", "DELETE")]

    # Unreadable configuration: kept (no recreation at every check).
    routes[("GET", "/rest/repositories/kg")] = (500, b"oops")
    assert store.check_inference() is True
    store._ensure_graphdb_repository()
    assert not [r for r in requests if r[0] in ("PUT", "DELETE")]

    # No inference: the "empty" ruleset.
    store = Store(StoreConfig(type="graphdb", query=f"{url}/repositories/kg", data=url))
    routes[("GET", "/rest/repositories/kg")] = (200, {"params": {"ruleset": {"value": "empty"}}})
    assert store.check_inference() is True


def test_fuseki_inference_check_and_refresh(tmp_path, fake_store):
    url, routes, requests = fake_store
    store = Store(StoreConfig(type="fuseki", query=f"{url}/ds/sparql", update=f"{url}/ds/update",
                              data=f"{url}/ds/data", inference_service=f"{url}/kg"), inference="rdfs")
    # The store started with another regime.
    with pytest.raises(RuntimeError, match="restart it"):
        store.check_inference()
    routes[("GET", "/kg/inference-rdfs")] = (200, {"boolean": True})
    assert store.check_inference() is True

    # After loading, the reasoner's deductions are discarded (refused update), then recomputed.
    count = {"results": {"bindings": [{"n": {"type": "literal", "value": "1"}}]}}
    routes.update({("GET", "/ds/sparql"): (200, {"boolean": True}), ("POST", "/ds/sparql"): (200, count),
                   ("POST", "/ds/update"): (204, b""), ("POST", "/ds/data"): (200, b"{}"),
                   ("POST", "/kg/update"): (403, b"Can't update the union graph of a dataset"),
                   ("POST", "/kg/sparql"): (200, {"boolean": True})})
    (tmp_path / "a.ttl").write_bytes(b"<urn:a> <urn:b> <urn:c> .\n")
    store.load(rdffiles.load_items("a.ttl", tmp_path / "a.ttl", lambda key: "urn:g"))
    posts = [path for method, path, _ in requests if method == "POST"]
    assert posts.index("/kg/update") > posts.index("/ds/data?graph=urn%3Ag")
    assert posts.index("/kg/sparql") > posts.index("/kg/update")

    # No inference: nothing to refresh.
    requests.clear()
    routes[("GET", "/kg/inference-none")] = (200, {"boolean": True})
    store.inference = "none"
    assert store.check_inference() is True
    store.load(rdffiles.load_items("a.ttl", tmp_path / "a.ttl", lambda key: "urn:g"))
    assert not [r for r in requests if r[1].startswith(("/kg/update", "/kg/sparql"))]


class SlowStream(io.RawIOBase):
    """``chunks`` pieces of N-Quads, one every ``delay`` seconds."""

    def __init__(self, chunks: int, delay: float):
        self.chunks, self.delay, self.sent = chunks, delay, 0

    def readable(self):
        return True

    def readinto(self, b):
        if self.sent == self.chunks:
            return 0
        if self.sent:
            time.sleep(self.delay)
        data = b"<urn:s%d> <urn:p> <urn:o> <urn:g> .\n" % self.sent
        self.sent += 1
        b[:len(data)] = data
        return len(data)


@pytest.fixture()
def rdf4j_transactions():
    """An RDF4J transaction endpoint that forgets a transaction left untouched for TIMEOUT
    seconds, as the RDF4J server does: requests touch it when they start and when they end."""
    timeout = 0.3
    state = {"touched": time.monotonic(), "expired": False, "pings": 0, "added": b""}
    lock = threading.Lock()

    def touch():
        with lock:
            now = time.monotonic()
            if now - state["touched"] > timeout:
                state["expired"] = True
            state["touched"] = now
            return not state["expired"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            if self.headers.get("Transfer-Encoding") != "chunked":
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = b""
            while (size := int(self.rfile.readline().strip(), 16)):
                data += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
            return data

        def do_POST(self):
            self._body()
            self.send_response(201)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/repositories/kg/transactions/1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            touch()

        def do_PUT(self):
            action = self.path.split("action=")[1].split("&")[0]
            if not touch():
                self._body()
                return self._reply(500, b"transaction with id 1 not registered.")
            body = self._body()
            if action == "PING":
                state["pings"] += 1
                body = str(int(timeout * 1000)).encode()
            elif action == "ADD":
                state["added"] += body
                body = b""
            else:
                body = b""
            ok = touch()
            self._reply(200 if ok else 500, body if ok else b"transaction with id 1 not registered.")

        def do_DELETE(self):
            self._reply(204)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()


def test_rdf4j_transaction_kept_alive_during_a_long_upload(rdf4j_transactions):
    url, state = rdf4j_transactions
    store = Store(StoreConfig(type="graphdb", query=f"{url}/repositories/kg", data=url))
    store._ensure_graphdb_repository = lambda: None
    item = rdffiles.LoadItem(key="slow.nq", path=Path("slow.nq"), format=rdffiles.detect("slow.nq"))
    # A 1.2 s upload, 4 times the timeout of the transaction.
    store._load_rdf4j([item], lambda item: io.BufferedReader(SlowStream(13, 0.1)))
    assert not state["expired"]
    assert state["pings"] >= 4
    assert state["added"].count(b"\n") == 13
