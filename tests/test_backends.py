import gzip
import json
import threading
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
