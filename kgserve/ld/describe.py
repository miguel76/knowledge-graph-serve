"""Fetch the description of resources from the SPARQL endpoint and serialise it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import rdflib
from rdflib import BNode, Literal, URIRef

from .. import http

# Output formats: short name -> (media type, rdflib format, is a dataset format)
FORMATS: dict[str, tuple[str, str, bool]] = {
    "ttl": ("text/turtle", "turtle", False),
    "nt": ("application/n-triples", "nt", False),
    "jsonld": ("application/ld+json", "json-ld", False),
    "rdf": ("application/rdf+xml", "xml", False),
    "n3": ("text/n3", "n3", False),
    "nq": ("application/n-quads", "nquads", True),
    "trig": ("application/trig", "trig", True),
}
FORMAT_LABELS = {"ttl": "Turtle", "nt": "N-Triples", "jsonld": "JSON-LD", "rdf": "RDF/XML",
                 "nq": "N-Quads", "trig": "TriG", "n3": "N3"}

MEDIA_TYPES: dict[str, str] = {media: name for name, (media, _, _) in FORMATS.items()}
MEDIA_TYPES.update({
    "application/x-turtle": "ttl",
    "application/json": "jsonld",
    "application/xml": "rdf",
    "text/xml": "rdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
})

LABEL_PROPERTIES = [
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://purl.org/dc/terms/title",
    "http://purl.org/dc/elements/1.1/title",
    "http://xmlns.com/foaf/0.1/name",
    "http://schema.org/name",
    "https://schema.org/name",
    "http://www.w3.org/2008/05/skos-xl#literalForm",
]


def negotiate(accept: str | None) -> str | None:
    """Pick an output format ("html" or a key of FORMATS) from an Accept header.
    A bare ``*/*`` (non-browser clients) gets Turtle; ``None`` if nothing is acceptable."""
    if not accept or not accept.strip():
        return "ttl"
    options: list[tuple[float, int, str]] = []
    for position, part in enumerate(accept.split(",")):
        media, *params = [p.strip() for p in part.split(";")]
        q = 1.0
        for param in params:
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if q <= 0:
            continue
        media = media.lower()
        if media in MEDIA_TYPES:
            options.append((q, -position, MEDIA_TYPES[media]))
        elif media in ("*/*", "text/*", "application/*"):
            # Wildcards rank below explicit types with the same q.
            options.append((q - 0.0001, -1000 - position, "ttl" if media != "text/*" else "html"))
    if not options:
        return None
    return max(options)[2]


def _term(binding: dict):
    kind = binding["type"]
    if kind == "uri":
        return URIRef(binding["value"])
    if kind == "bnode":
        return BNode(binding["value"])
    if kind in ("literal", "typed-literal"):
        lang = binding.get("xml:lang")
        datatype = None if lang else binding.get("datatype")
        return Literal(binding["value"], lang=lang, datatype=URIRef(datatype) if datatype else None)
    raise ValueError(f"unsupported term type {kind}")


def _blank_closure(depth: int) -> str:
    """Patterns for the subject ``?r`` and blank nodes reachable from it, up to ``depth``."""
    branches = ["{ GRAPH ?g { ?r ?p ?o } BIND(?r AS ?s) }"]
    for level in range(1, depth + 1):
        chain = ["?r ?p0 ?b1 . FILTER(isBlank(?b1))"]
        for i in range(1, level):
            chain.append(f"?b{i} ?p{i} ?b{i + 1} . FILTER(isBlank(?b{i + 1}))")
        chain.append(f"?b{level} ?p ?o . BIND(?b{level} AS ?s)")
        branches.append("{ GRAPH ?g { " + " ".join(chain) + " } }")
    return "\n  UNION ".join(branches)


def describe_query(iris: list[str], limit: int, depth: int = 3) -> str:
    values = " ".join(f"<{iri}>" for iri in iris)
    return f"SELECT DISTINCT ?g ?s ?p ?o WHERE {{\n  VALUES ?r {{ {values} }}\n  {_blank_closure(depth)}\n}} LIMIT {limit}"


def inverse_query(iri: str, limit: int) -> str:
    return (f"SELECT DISTINCT ?s ?p WHERE {{ GRAPH ?g {{ ?s ?p <{iri}> }} FILTER(isIRI(?s)) }} "
            f"LIMIT {limit + 1}")


def labels_query(iris: list[str]) -> str:
    # One UNION branch per property: joining two VALUES blocks makes some stores (Oxigraph)
    # pick a plan that is orders of magnitude slower.
    values = " ".join(f"<{iri}>" for iri in iris)
    branches = " UNION ".join(f"{{ GRAPH ?g {{ ?x <{p}> ?l }} }}" for p in LABEL_PROPERTIES)
    return (f"SELECT DISTINCT ?x ?l WHERE {{ VALUES ?x {{ {values} }} {branches} FILTER(isLiteral(?l)) }} "
            f"LIMIT {len(iris) * 10}")


@dataclass
class Description:
    """The quads describing one or more resources (a resource and its hash IRIs)."""

    iris: list[str]
    dataset: rdflib.Dataset
    truncated: bool = False
    inverse: list[tuple[str, str]] = field(default_factory=list)
    inverse_truncated: bool = False
    labels: dict[str, list[Literal]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.dataset)

    def serialize(self, fmt: str) -> bytes:
        _, rdflib_format, is_dataset = FORMATS[fmt]
        if is_dataset:
            return self.dataset.serialize(format=rdflib_format, encoding="utf-8")
        # Triple formats: merge the graphs (this also removes duplicates across graphs).
        graph = rdflib.Graph()
        for prefix, namespace in self.dataset.namespaces():
            graph.bind(prefix, namespace, override=True)
        for s, p, o, _ in self.dataset.quads((None, None, None, None)):
            graph.add((s, p, o))
        return graph.serialize(format=rdflib_format, encoding="utf-8")


class Store:
    def __init__(self, endpoint: str, auth: tuple[str, str] | None = None, timeout: float = 60):
        self.endpoint = endpoint
        self.auth = auth
        self.timeout = timeout

    def select(self, query: str) -> list[dict]:
        resp = http.sparql(self.endpoint, query, auth=self.auth, timeout=self.timeout)
        return json.loads(resp.body)["results"]["bindings"]

    def describe(self, iris: list[str], limit: int, prefixes: dict[str, str] | None = None) -> Description:
        dataset = rdflib.Dataset()
        for prefix, namespace in (prefixes or {}).items():
            dataset.bind(prefix, namespace, override=True)
        rows = self.select(describe_query(iris, limit))
        for row in rows:
            graph = dataset.graph(URIRef(row["g"]["value"])) if "g" in row else dataset.default_graph
            graph.add((_term(row["s"]), _term(row["p"]), _term(row["o"])))
        return Description(iris, dataset, truncated=len(rows) >= limit)

    def add_inverse(self, desc: Description, limit: int) -> None:
        rows = self.select(inverse_query(desc.iris[0], limit))
        desc.inverse_truncated = len(rows) > limit
        desc.inverse = [(r["s"]["value"], r["p"]["value"]) for r in rows[:limit]]

    def add_labels(self, desc: Description, max_iris: int = 400) -> None:
        iris: list[str] = []
        seen: set[str] = set()

        def add(term) -> None:
            if isinstance(term, URIRef) and str(term) not in seen and len(iris) < max_iris:
                seen.add(str(term))
                iris.append(str(term))

        for s, p, o, _ in desc.dataset.quads((None, None, None, None)):
            add(s), add(p), add(o)
        for s, p in desc.inverse:
            add(URIRef(s)), add(URIRef(p))
        if not iris:
            return
        for row in self.select(labels_query(iris)):
            desc.labels.setdefault(row["x"]["value"], []).append(_term(row["l"]))
