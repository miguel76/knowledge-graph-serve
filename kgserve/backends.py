"""Loading a release into the triple store, which runs in its own container.

Two standard protocols cover the supported stores:

* SPARQL 1.1 Update + Graph Store Protocol (Oxigraph, Apache Jena Fuseki, ...): the store is
  emptied with ``DROP ALL``, then each file is posted. Until the load ends (about 30 s for
  6 million quads on Oxigraph), queries see partial data.
* RDF4J REST API (GraphDB, RDF4J): the data are replaced inside one transaction, so
  queries see either the old or the new release.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import IO, Callable

from . import http
from .config import StoreConfig
from .rdffiles import LoadItem

log = logging.getLogger(__name__)

# Count of the quads in named graphs: portable across stores whose default graph is (or is
# not) the union of the named graphs.
NAMED_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
DEFAULT_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"
NOT_EMPTY_QUERY = "ASK { { ?s ?p ?o } UNION { GRAPH ?g { ?s ?p ?o } } }"

#: Optional wrapper of each file's stream on its way to the store (see hashindex.py).
Tap = Callable[[LoadItem, IO[bytes]], IO[bytes]]


def with_params(url: str, **params: str) -> str:
    """Append query parameters to a URL that may already have some (kept as they are). An
    empty value gives a bare key, as in the Graph Store Protocol's ``?default``."""
    added = "&".join(urllib.parse.quote(k, safe="") + (f"={urllib.parse.quote(v, safe='')}" if v else "")
                     for k, v in params.items())
    if not added:
        return url
    return f"{url}{'&' if '?' in url else '?'}{added}"


class Store:
    def __init__(self, config: StoreConfig):
        self.config = config
        self.query_endpoint = config.query
        self.auth = config.auth

    def select(self, query: str, timeout: float = 600) -> list[dict]:
        return http.select(self.query_endpoint, query, auth=self.auth, timeout=timeout)

    def wait_until_up(self) -> None:
        http.wait_until_up(with_params(self.query_endpoint, query="ASK {}"), auth=self.auth)

    def is_empty(self) -> bool:
        resp = http.sparql(self.query_endpoint, NOT_EMPTY_QUERY, auth=self.auth)
        return not json.loads(resp.body)["boolean"]

    def count(self) -> int:
        named = int(self.select(NAMED_COUNT_QUERY)[0]["n"]["value"])
        # Data in the default graph only (e.g. triplesGraph "default"): count those instead.
        return named or int(self.select(DEFAULT_COUNT_QUERY)[0]["n"]["value"])

    def load(self, items: list[LoadItem], tap: Tap | None = None) -> dict:
        """Replace the content of the store with ``items``."""
        self.wait_until_up()
        started = time.monotonic()
        opener = (lambda item: tap(item, item.open())) if tap else (lambda item: item.open())
        if self.config.type == "graphdb":
            self._load_rdf4j(items, opener)
        else:
            self._load_gsp(items, opener)
        return {"triples": self.count(), "load_seconds": round(time.monotonic() - started, 1)}

    # -- SPARQL Update + Graph Store Protocol ------------------------------------------------

    def _load_gsp(self, items: list[LoadItem], opener: Callable[[LoadItem], IO[bytes]]) -> None:
        body, headers = http.form({"update": "DROP ALL"})
        http.request("POST", self.config.update, body, headers, auth=self.auth)
        for item in items:
            log.info("Loading %s", item.key)
            if item.format.dataset:
                url = self.config.data  # quads: graphs come from the data
            elif item.graph:
                url = with_params(self.config.data, graph=item.graph)
            else:
                url = with_params(self.config.data, default="")
            with opener(item) as stream:
                http.request("POST", url, stream, {"Content-Type": item.format.media_type}, auth=self.auth)

    # -- RDF4J REST API (GraphDB) ------------------------------------------------------------

    def _ensure_graphdb_repository(self) -> None:
        server = self.config.data.rstrip("/")
        listing = json.loads(http.request("GET", f"{server}/repositories", auth=self.auth,
                                          headers={"Accept": "application/sparql-results+json"}).body)
        if any(row.get("id", {}).get("value") == self.config.repository for row in listing["results"]["bindings"]):
            return
        log.info("Creating GraphDB repository %s", self.config.repository)
        http.request("PUT", f"{server}/repositories/{self.config.repository}",
                     GRAPHDB_REPOSITORY_TEMPLATE.format(id=self.config.repository).encode(),
                     {"Content-Type": "text/turtle"}, auth=self.auth)

    def _load_rdf4j(self, items: list[LoadItem], opener: Callable[[LoadItem], IO[bytes]]) -> None:
        self._ensure_graphdb_repository()
        repository = f"{self.config.data.rstrip('/')}/repositories/{self.config.repository}"
        txn = http.request("POST", f"{repository}/transactions", auth=self.auth).headers["location"]
        try:
            http.request("PUT", with_params(txn, action="UPDATE"), b"CLEAR ALL",
                         {"Content-Type": "application/sparql-update"}, auth=self.auth)
            for item in items:
                log.info("Loading %s", item.key)
                params = {"action": "ADD"}
                if item.graph and not item.format.dataset:
                    params["context"] = f"<{item.graph}>"
                with opener(item) as stream:
                    http.request("PUT", with_params(txn, **params), stream,
                                 {"Content-Type": item.format.media_type}, auth=self.auth)
            http.request("PUT", with_params(txn, action="COMMIT"), auth=self.auth)
        except BaseException:
            try:
                http.request("DELETE", txn, auth=self.auth)
            except Exception:  # noqa: BLE001 - best-effort rollback
                log.exception("Could not roll back transaction %s", txn)
            raise


GRAPHDB_REPOSITORY_TEMPLATE = """\
@prefix config: <tag:rdf4j.org,2023:config/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .
[] a config:Repository ;
   config:rep.id "{id}" ;
   rdfs:label "{id}" ;
   config:rep.impl [
      config:rep.type "graphdb:SailRepository" ;
      config:sail.impl [
         config:sail.type "graphdb:Sail" ;
         graphdb:ruleset "empty" ;
         graphdb:disable-sameAs "true" ;
         graphdb:query-timeout "60"
      ]
   ] .
"""
