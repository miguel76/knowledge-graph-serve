"""Loading a release into the triple store, which runs in its own container.

Two standard protocols cover the supported stores:

* SPARQL 1.1 Update + Graph Store Protocol (Oxigraph, Apache Jena Fuseki, ...): the store is
  emptied with ``DROP ALL``, then each file is posted. Until the load ends (about 30 s for
  6 million quads on Oxigraph), queries see partial data.
* RDF4J REST API (GraphDB, RDF4J): the data are replaced inside one transaction, so
  queries see either the old or the new release.

Inference (``inference`` in config.json), where the store offers it:

* GraphDB: the regime is the ruleset of the repository, and inferences are materialised
  when loading. A repository with another ruleset is recreated, then loaded.
* Fuseki: the regime is set when the store starts (fuseki/entrypoint.sh reads config.json)
  on the service "kg", a Jena reasoner over the union of the graphs. Jena reasoners cache
  their deductions, so after each load the sync makes it start again from the new data.
"""

from __future__ import annotations

import json
import logging
import time
from typing import IO, Callable

from . import http
from .config import StoreConfig
from .http import with_params
from .rdffiles import LoadItem

log = logging.getLogger(__name__)

# Count of the quads in named graphs: portable across stores whose default graph is (or is
# not) the union of the named graphs.
NAMED_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
DEFAULT_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"
NOT_EMPTY_QUERY = "ASK { { ?s ?p ?o } UNION { GRAPH ?g { ?s ?p ?o } } }"

#: Optional wrapper of each file's stream on its way to the store (see hashindex.py).
Tap = Callable[[LoadItem, IO[bytes]], IO[bytes]]


def graphdb_ruleset(inference: str) -> str:
    return "empty" if inference == "none" else inference


class Store:
    def __init__(self, config: StoreConfig, inference: str = "none"):
        self.config = config
        self.inference = inference
        # Counts and statistics are about the explicit statements.
        self.query_endpoint = config.explicit_query
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
            if self.config.type == "fuseki" and self.inference != "none":
                self._refresh_fuseki_inference()
        return {"triples": self.count(), "load_seconds": round(time.monotonic() - started, 1)}

    # -- Inference ---------------------------------------------------------------------------

    def check_inference(self) -> bool:
        """Whether the store serves the configured inference regime.

        GraphDB: ``False`` if the repository is missing or has another ruleset; the next load
        recreates it. Fuseki takes the regime from config.json when it starts, so a store
        started with another one is an error, solved by restarting the store.
        """
        if self.config.type == "graphdb":
            exists, ruleset = self._graphdb_repository()
            return exists and ruleset in (None, graphdb_ruleset(self.inference))
        if self.config.type == "fuseki" and self.config.inference_service:
            # fuseki/entrypoint.sh names a query endpoint of the service after the regime.
            url = with_params(f"{self.config.inference_service.rstrip('/')}/inference-{self.inference}",
                              query="ASK {}")
            try:
                http.request("GET", url, auth=self.auth, timeout=60, ok=(200,))
            except http.HTTPError as err:
                if err.status != 404:
                    raise
                raise RuntimeError(
                    f"The store does not serve inference {self.inference!r}: it reads \"inference\" from "
                    "config.json when it starts, restart it (docker compose restart store)") from None
        return True

    def _refresh_fuseki_inference(self) -> None:
        """Make Fuseki's reasoner start again from the data just loaded.

        A Jena inference graph keeps its deductions and does not see what is written through
        another service. Aborting a write transaction on it discards them (``rebind``). This
        update is refused (the union graph is read-only), which aborts it. Then a first query
        computes the deductions, here rather than on a user's query.
        """
        service = self.config.inference_service.rstrip("/")
        if not service:
            log.warning("KG_STORE_INFERENCE is not set: the reasoner may keep deductions of the previous release")
            return
        body, headers = http.form({"update": "DELETE DATA { <urn:x-kgserve:reset> <urn:x-kgserve:reset> <urn:x-kgserve:reset> }"})
        http.request("POST", f"{service}/update", body, headers, auth=self.auth, timeout=600, ok=(200, 204, 403))
        started = time.monotonic()
        try:
            http.sparql(f"{service}/sparql", "ASK { ?s ?p ?o }", auth=self.auth, timeout=3600)
            log.info("Inference %s ready in %.1f s", self.inference, time.monotonic() - started)
        except Exception as err:  # noqa: BLE001 - the first user query will compute them instead
            log.warning("Inference %s: first query failed (%s)", self.inference, err)

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

    def _graphdb_repository(self) -> tuple[bool, str | None]:
        """Whether the repository exists, and its ruleset (``None`` if it cannot be read)."""
        server = self.config.data.rstrip("/")
        repository = self.config.repository
        listing = json.loads(http.request("GET", f"{server}/repositories", auth=self.auth,
                                          headers={"Accept": "application/sparql-results+json"}).body)
        if not any(row.get("id", {}).get("value") == repository for row in listing["results"]["bindings"]):
            return False, None
        try:
            config = json.loads(http.request("GET", f"{server}/rest/repositories/{repository}", auth=self.auth,
                                             headers={"Accept": "application/json"}, timeout=60).body)
            ruleset = (config.get("params") or {}).get("ruleset")
            ruleset = ruleset.get("value") if isinstance(ruleset, dict) else ruleset
            if isinstance(ruleset, str) and ruleset:
                return True, ruleset
        except (http.HTTPError, ValueError, AttributeError) as err:
            log.warning("Could not read the configuration of GraphDB repository %s: %s", repository, err)
            return True, None
        log.warning("No ruleset in the configuration of GraphDB repository %s", repository)
        return True, None

    def _ensure_graphdb_repository(self) -> None:
        server = self.config.data.rstrip("/")
        repository = self.config.repository
        ruleset = graphdb_ruleset(self.inference)
        exists, current = self._graphdb_repository()
        if exists and current in (None, ruleset):
            return
        if exists:
            # The ruleset of a repository is set when it is created: start again with the new one.
            log.info("Recreating GraphDB repository %s with ruleset %s (was %s)", repository, ruleset, current)
            http.request("DELETE", f"{server}/repositories/{repository}", auth=self.auth)
        else:
            log.info("Creating GraphDB repository %s with ruleset %s", repository, ruleset)
        http.request("PUT", f"{server}/repositories/{repository}",
                     GRAPHDB_REPOSITORY_TEMPLATE.format(id=repository, ruleset=ruleset).encode(),
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
         graphdb:ruleset "{ruleset}" ;
         graphdb:disable-sameAs "true" ;
         graphdb:query-timeout "60"
      ]
   ] .
"""
