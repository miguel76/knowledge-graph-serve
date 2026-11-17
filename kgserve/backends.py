"""Triple store backends.

* ``oxigraph`` (default, *managed*): the sync process owns an Oxigraph server. Each new
  release is bulk-loaded offline into a fresh store directory while the previous one keeps
  serving; then the read-only server is restarted on the new store (blue/green swap).
* ``graphdb`` (*remote*): a GraphDB (or any RDF4J) server. Data are replaced inside one
  RDF4J transaction, so queries see either the old or the new release.
* ``qlever`` (*managed*): QLever, for large graphs and analytic queries; see ``qlever.py``.
* ``gsp`` (*remote*): any store implementing the SPARQL 1.1 Graph Store Protocol and Update
  (e.g. Apache Jena Fuseki). Data are dropped and re-posted (not atomic).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import http
from .rdffiles import LoadItem

log = logging.getLogger(__name__)

# Exact count, for a store whose default graph is not the union of the named graphs.
COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { { ?s ?p ?o } UNION { GRAPH ?g { ?s ?p ?o } } }"
# Portable count of the quads in named graphs (remote stores often use a union default graph).
NAMED_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
# Subjects with a fragment, to dereference hash IRIs (``doc#a``) through their document IRI.
HASH_SUBJECTS_LIMIT = int(os.environ.get("HASH_SUBJECTS_LIMIT", "200000"))
HASH_SUBJECTS_QUERY = (
    "SELECT DISTINCT ?s WHERE { { ?s ?p ?o } UNION { GRAPH ?g { ?s ?p ?o } } "
    f'FILTER(isIRI(?s) && CONTAINS(STR(?s), "#")) }} LIMIT {HASH_SUBJECTS_LIMIT}'
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


class Backend:
    name = "base"
    #: SPARQL query endpoint that the sync process itself can reach.
    query_endpoint = ""
    auth: tuple[str, str] | None = None

    def resume(self, state: dict) -> None:
        """Called at start-up with the persisted state (e.g. to restart a managed server)."""

    def load(self, items: list[LoadItem], tag: str) -> dict:
        """Replace the store content with ``items``; return info to persist in the state."""
        raise NotImplementedError

    def check(self) -> None:
        """Called periodically: restart crashed managed processes, etc."""

    def shutdown(self) -> None:
        pass

    def count(self) -> int:
        rows = http.select(self.query_endpoint, NAMED_COUNT_QUERY, auth=self.auth, timeout=600)
        return int(rows[0]["n"]["value"])

    def hash_subjects(self) -> list[str]:
        """IRIs with a fragment used as subjects (full scan: called once per sync)."""
        rows = http.select(self.query_endpoint, HASH_SUBJECTS_QUERY, auth=self.auth, timeout=3600)
        return [row["s"]["value"] for row in rows]


# --------------------------------------------------------------------------------------
# Oxigraph (managed)
# --------------------------------------------------------------------------------------


class OxigraphBackend(Backend):
    name = "oxigraph"

    def __init__(self, data_dir: Path):
        self.bin = _env("OXIGRAPH_BIN", "oxigraph")
        self.bind = _env("OXIGRAPH_BIND", "0.0.0.0:7878")
        self.timeout = _env("OXIGRAPH_TIMEOUT", "60")
        self.union_default_graph = _env("OXIGRAPH_UNION_DEFAULT_GRAPH", "true").lower() in ("1", "true", "yes")
        self.optimize = _env("OXIGRAPH_OPTIMIZE", "true").lower() in ("1", "true", "yes")
        self.stores_dir = data_dir / "stores"
        self.stores_dir.mkdir(parents=True, exist_ok=True)
        port = self.bind.rsplit(":", 1)[-1]
        self.local_url = f"http://127.0.0.1:{port}"
        self.query_endpoint = f"{self.local_url}/query"
        self.proc: subprocess.Popen | None = None
        self.current: str | None = None
        self._hash_subjects: list[str] = []

    # -- server process -----------------------------------------------------------------

    def _start(self, tag: str) -> None:
        cmd = [self.bin, "serve-read-only", "--location", str(self.stores_dir / tag), "--bind", self.bind, "--cors"]
        if self.union_default_graph:
            cmd.append("--union-default-graph")
        if self.timeout and self.timeout != "0":
            cmd += ["--timeout-s", self.timeout]
        log.info("Starting Oxigraph on store %s", tag)
        self.proc = subprocess.Popen(cmd)
        deadline = time.monotonic() + 120
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Oxigraph exited with code {self.proc.returncode} on store {tag}")
            try:
                http.request("GET", f"{self.local_url}/query?query=ASK%7B%7D", timeout=5, ok=(200,))
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        self.current = tag
        log.info("Oxigraph is serving store %s at %s", tag, self.bind)

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def resume(self, state: dict) -> None:
        tag = state.get("store", {}).get("tag")
        if tag and (self.stores_dir / tag).is_dir():
            self._start(tag)

    def check(self) -> None:
        if self.current and self.proc and self.proc.poll() is not None:
            log.error("Oxigraph exited unexpectedly (code %s), restarting", self.proc.returncode)
            self._start(self.current)

    def shutdown(self) -> None:
        self._stop()

    # -- loading ------------------------------------------------------------------------

    def _run(self, args: list[str], stdin=None) -> None:
        log.info("$ %s", " ".join(args))
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin else None)
        if stdin is not None:
            # Feed decompressed content to oxigraph's stdin.
            assert proc.stdin is not None
            with stdin:
                shutil.copyfileobj(stdin, proc.stdin, 1 << 20)
            proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError(f"command failed with code {proc.returncode}: {' '.join(args)}")

    def _query(self, location: Path, query: str) -> list[str]:
        """Run a single-variable SELECT on a store directory; return the TSV values."""
        out = subprocess.run(
            [self.bin, "query", "--location", str(location), "--query", query, "--results-format", "tsv"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        return out[1:]

    def hash_subjects(self) -> list[str]:
        return self._hash_subjects

    def load(self, items: list[LoadItem], tag: str) -> dict:
        target = self.stores_dir / tag
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        base = [self.bin, "load", "--location", str(target)]
        started = time.monotonic()
        try:
            # Files Oxigraph can read directly are loaded in parallel, grouped by target graph.
            groups: dict[str | None, list[str]] = {}
            piped: list[LoadItem] = []
            for item in items:
                if item.native_path is not None:
                    groups.setdefault(item.graph, []).append(str(item.native_path))
                else:
                    piped.append(item)
            for graph, files in groups.items():
                self._run(base + (["--graph", graph] if graph else []) + ["--file", *files])
            for item in piped:
                args = base + ["--format", item.format.ext] + (["--graph", item.graph] if item.graph else [])
                self._run(args, stdin=item.open())
            load_seconds = time.monotonic() - started
            if self.optimize:
                self._run([self.bin, "optimize", "--location", str(target)])
            triples = int(self._query(target, COUNT_QUERY)[0].split("^^")[0].strip('"'))
            if triples == 0:
                raise RuntimeError("the new store is empty")
            # Full scans run here, on the new store, without the server's query timeout.
            self._hash_subjects = [v.strip("<>") for v in self._query(target, HASH_SUBJECTS_QUERY)]
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

        # Blue/green swap: stop the old server, serve the new store, drop the old one.
        previous = self.current
        self._stop()
        try:
            self._start(tag)
        except Exception:
            log.exception("Could not serve the new store, going back to %s", previous)
            if previous:
                self._start(previous)
            raise
        for old in self.stores_dir.iterdir():
            if old.name != tag:
                shutil.rmtree(old, ignore_errors=True)
        return {"tag": tag, "triples": triples, "load_seconds": round(load_seconds, 1),
                "total_seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------------------
# GraphDB (remote, RDF4J REST API)
# --------------------------------------------------------------------------------------


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
         graphdb:query-timeout "{timeout}"
      ]
   ] .
"""


class GraphDBBackend(Backend):
    name = "graphdb"

    def __init__(self):
        self.url = _env("GRAPHDB_URL", "http://graphdb:7200").rstrip("/")
        self.repo = _env("GRAPHDB_REPOSITORY", "kg")
        self.ruleset = _env("GRAPHDB_RULESET", "empty")
        self.timeout = _env("GRAPHDB_QUERY_TIMEOUT", "60")
        user = _env("GRAPHDB_USER")
        self.auth = (user, _env("GRAPHDB_PASSWORD")) if user else None
        self.query_endpoint = f"{self.url}/repositories/{self.repo}"

    def _ensure_repository(self) -> None:
        # RDF4J REST API (implemented by GraphDB): list the repositories.
        http.wait_until_up(f"{self.url}/repositories", auth=self.auth)
        listing = json.loads(http.request("GET", f"{self.url}/repositories", auth=self.auth,
                                          headers={"Accept": "application/sparql-results+json"}).body)
        if any(row.get("id", {}).get("value") == self.repo for row in listing["results"]["bindings"]):
            return
        log.info("Creating GraphDB repository %s", self.repo)
        config_file = _env("GRAPHDB_REPOSITORY_CONFIG")
        config = Path(config_file).read_bytes() if config_file else GRAPHDB_REPOSITORY_TEMPLATE.format(
            id=self.repo, ruleset=self.ruleset, timeout=self.timeout).encode()
        # RDF4J REST API: create a repository from its Turtle configuration.
        http.request("PUT", self.query_endpoint, config, {"Content-Type": "text/turtle"}, auth=self.auth)

    def load(self, items: list[LoadItem], tag: str) -> dict:
        self._ensure_repository()
        started = time.monotonic()
        resp = http.request("POST", f"{self.query_endpoint}/transactions", auth=self.auth)
        txn = resp.headers["location"]
        sep = "&" if "?" in txn else "?"
        try:
            http.request("PUT", f"{txn}{sep}action=UPDATE", b"CLEAR ALL",
                         {"Content-Type": "application/sparql-update"}, auth=self.auth)
            for item in items:
                log.info("Adding %s", item.key)
                url = f"{txn}{sep}action=ADD"
                if item.graph:
                    url += "&context=" + urllib.parse.quote(f"<{item.graph}>")
                with item.open() as stream:
                    http.request("PUT", url, stream, {"Content-Type": item.format.media_type}, auth=self.auth)
            http.request("PUT", f"{txn}{sep}action=COMMIT", auth=self.auth)
        except BaseException:
            try:
                http.request("DELETE", txn, auth=self.auth)
            except Exception:  # noqa: BLE001 - best effort rollback
                log.exception("Could not roll back transaction %s", txn)
            raise
        return {"tag": tag, "triples": self.count(), "load_seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------------------
# Generic SPARQL 1.1 Graph Store Protocol (e.g. Apache Jena Fuseki)
# --------------------------------------------------------------------------------------


class GSPBackend(Backend):
    name = "gsp"

    def __init__(self):
        self.dataset = _env("GSP_DATASET_URL", "http://fuseki:3030/kg").rstrip("/")
        self.update_endpoint = _env("GSP_UPDATE_URL", self.dataset + "/update")
        self.query_endpoint = _env("GSP_QUERY_URL", self.dataset + "/query")
        user = _env("GSP_USER")
        self.auth = (user, _env("GSP_PASSWORD")) if user else None

    def load(self, items: list[LoadItem], tag: str) -> dict:
        http.wait_until_up(self.query_endpoint, auth=self.auth)
        started = time.monotonic()
        body, headers = http.form({"update": "DROP ALL"})
        http.request("POST", self.update_endpoint, body, headers, auth=self.auth)
        for item in items:
            log.info("Posting %s", item.key)
            url = self.dataset
            if not item.format.dataset:
                url += ("?graph=" + urllib.parse.quote(item.graph, safe="")) if item.graph else "?default"
            with item.open() as stream:
                http.request("POST", url, stream, {"Content-Type": item.format.media_type}, auth=self.auth)
        return {"tag": tag, "triples": self.count(), "load_seconds": round(time.monotonic() - started, 1)}


def create(name: str, data_dir: Path) -> Backend:
    if name == "oxigraph":
        return OxigraphBackend(data_dir)
    if name in ("graphdb", "rdf4j"):
        return GraphDBBackend()
    if name in ("gsp", "fuseki"):
        return GSPBackend()
    if name == "qlever":
        from .qlever import QLeverBackend

        return QLeverBackend(data_dir)
    raise SystemExit(f"Unknown BACKEND {name!r} (expected oxigraph, qlever, graphdb or gsp)")
