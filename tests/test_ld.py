import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import rdflib
from rdflib import Literal, URIRef
from rdflib.plugins.sparql import prepareQuery

from kgserve.config import Config, StoreConfig
from kgserve.ld import server
from kgserve.ld.describe import Description, describe_query, inverse_query, labels_query, negotiate
from kgserve.ld.render import html_to_paragraphs

BASE = "https://w3id.org/test/data/"
EX = "http://example.org/onto#"


@pytest.mark.parametrize("accept,expected", [
    ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "html"),
    ("text/turtle", "ttl"),
    ("application/ld+json", "jsonld"),
    ("application/json", "jsonld"),
    ("application/n-quads", "nq"),
    ("application/rdf+xml;q=0.5, text/turtle;q=0.9", "ttl"),
    ("*/*", "ttl"),
    ("", "ttl"),
    (None, "ttl"),
    ("text/turtle;q=0, application/n-triples", "nt"),
    ("image/png", None),
])
def test_negotiate(accept, expected):
    assert negotiate(accept) == expected


def test_queries_are_valid_sparql():
    for query in (describe_query([BASE + "a", BASE + "a#b"], 100), inverse_query(BASE + "a", 10),
                  labels_query([BASE + "a", EX + "p"])):
        prepareQuery(query)


def make_description() -> Description:
    dataset = rdflib.Dataset()
    s = URIRef(BASE + "a")
    for g in ("urn:g1", "urn:g2"):  # the same triple in two graphs
        dataset.graph(URIRef(g)).add((s, URIRef(EX + "p"), Literal("v")))
    return Description([str(s)], dataset)


def test_serialize_deduplicates_triple_formats_but_keeps_quads():
    desc = make_description()
    nt = desc.serialize("nt").decode().strip().splitlines()
    assert len(nt) == 1
    nq = desc.serialize("nq").decode().strip().splitlines()
    assert len(nq) == 2
    for fmt in ("ttl", "jsonld", "rdf", "trig", "n3"):
        assert desc.serialize(fmt)


def test_html_to_paragraphs_strips_markup():
    assert html_to_paragraphs("<p>Hello <script>x</script><b>world</b></p><ul><li>one</li></ul>") == \
        ["Hello xworld", "one"]


class FakeStore:
    """Stands in for the SPARQL endpoint: knows a few resources."""

    def __init__(self):
        self.calls = []

    def describe(self, iris, limit, prefixes=None):
        self.calls.append(list(iris))
        dataset = rdflib.Dataset()
        g = dataset.graph(URIRef("urn:g"))
        for iri in iris:
            if iri in (BASE + "a", BASE + "a.ttl", BASE + "doc#x", "http://other.org/z", BASE + "%C2%B0C"):
                g.add((URIRef(iri), URIRef(EX + "p"), Literal("value of " + iri)))
        return Description(list(iris), dataset)

    def add_inverse(self, desc, limit):
        desc.inverse = [(BASE + "b", EX + "q")]

    def add_labels(self, desc, max_iris=400):
        desc.labels = {BASE + "a": [Literal("Resource A", lang="en")]}


@pytest.fixture()
def ld(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "status": "ok", "store": {"tag": "t1", "triples": 3}, "uri_spaces": [BASE],
        "record": {"title": "Test KG", "doi": "10.5281/zenodo.1", "description": "<p>About</p>"},
    }))
    (tmp_path / "hash-index.json").write_text(json.dumps({BASE + "doc": [BASE + "doc#x"]}))
    config = Config(zenodo="1", store=StoreConfig(query="http://unused"), data_dir=tmp_path)
    server.Handler.app = server.App(config)
    store = FakeStore()
    server.Handler.app.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    httpd.shutdown()


def get(url, accept="text/turtle"):
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode()


def test_resource_turtle_has_canonical_iris(ld):
    base, _ = ld
    status, headers, body = get(base + "/id/a")
    assert status == 200
    assert headers["Content-Type"].startswith("text/turtle")
    assert headers["Vary"].startswith("Accept")
    assert f"<{BASE}a>" in body


def test_resource_html(ld):
    base, _ = ld
    status, headers, body = get(base + "/id/a", "text/html")
    assert status == 200 and "Resource A" in body and "Referenced by" in body
    assert 'href="../id/b"' in body  # relative link for IRIs in the namespace
    assert 'href="./a?format=ttl"' in body  # formats of this resource
    assert 'href="../sparql-ui/"' in body and 'href="../"' in body  # relative to the service root


def test_extension_and_format_parameter(ld):
    base, store = ld
    # "a.ttl" exists as a resource itself: served as is (content negotiated).
    status, _, body = get(base + "/id/a.ttl", "application/n-triples")
    assert status == 200 and f"<{BASE}a.ttl>" in body
    status, headers, _ = get(base + "/id/a?format=jsonld", "text/html")
    assert status == 200 and headers["Content-Type"].startswith("application/ld+json")


def test_extension_fallback(ld):
    base, store = ld
    status, headers, body = get(base + "/id/%C2%B0C.nt", "text/html")
    assert status == 200 and headers["Content-Type"].startswith("application/n-triples")
    assert store.calls[-2:] == [[BASE + "%C2%B0C.nt"], [BASE + "%C2%B0C"]]


def test_hash_iris_through_document(ld):
    base, store = ld
    status, _, body = get(base + "/id/doc")
    assert status == 200 and f"<{BASE}doc#x>" in body
    assert store.calls[-1] == [BASE + "doc", BASE + "doc#x"]


def test_not_found_and_not_acceptable(ld):
    base, _ = ld
    assert get(base + "/id/missing")[0] == 404
    assert get(base + "/id/missing", "text/html")[0] == 404
    assert get(base + "/id/a", "image/png")[0] == 406
    assert get(base + "/id/a?format=docx")[0] == 400


def test_browse_any_iri(ld):
    base, _ = ld
    status, _, body = get(base + "/browse?iri=http%3A%2F%2Fother.org%2Fz")
    assert status == 200 and "<http://other.org/z>" in body
    assert get(base + "/browse?iri=not%20an%20iri")[0] == 400


def test_etag(ld):
    base, _ = ld
    _, headers, _ = get(base + "/id/a")
    request = urllib.request.Request(base + "/id/a", headers={"Accept": "text/turtle", "If-None-Match": headers["ETag"]})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request)
    assert err.value.code == 304


def test_pages(ld):
    base, _ = ld
    status, _, body = get(base + "/", "text/html")
    assert status == 200 and "Test KG" in body and "About" in body
    status, _, body = get(base + "/sparql-ui/", "text/html")
    assert status == 200 and "@matdata/yasgui@" in body
    assert "<main>" not in body  # no site-wide CSS around the editor
    status, _, body = get(base + "/status.json")
    assert json.loads(body)["status"] == "ok"


def test_landing_shows_inference(ld, tmp_path):
    base, _ = ld
    assert "Inference" not in get(base + "/", "text/html")[2]
    state = json.loads((tmp_path / "state.json").read_text())
    (tmp_path / "state.json").write_text(json.dumps({**state, "inference": "owl2-rl"}))
    body = get(base + "/", "text/html")[2]
    assert "<code>owl2-rl</code>" in body and "infer=false" in body


@pytest.mark.parametrize("path,expected", [
    ("/", "./"), ("/browse?iri=x", "./"), ("/sparql-ui/", "../"), ("/id/a", "../"),
    ("/id/a/b/c", "../../../"), ("/id/a/?format=ttl", "../../"),
])
def test_root_prefix(path, expected):
    from kgserve.ld.render import root_prefix

    assert root_prefix(path) == expected


def test_links_are_relative_at_any_depth(ld):
    base, _ = ld
    status, _, body = get(base + "/id/doc", "text/html")
    assert status == 200 and 'id="x"' in body  # hash resource rendered with its anchor
    assert 'href="../sparql-ui/"' in body
    assert get(base + "/kg/id/a", "text/html")[0] == 404  # no base path: resources are at /id/
    status, _, body = get(base + "/", "text/html")
    assert 'href="./sparql-ui/"' in body and 'data-url="./sparql"' in body
    assert 'href="./"' in body  # home link
    assert ' href="/' not in body  # no root-absolute link anywhere
