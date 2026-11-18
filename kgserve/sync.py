"""The sync daemon: keep a triple store in line with the latest release of a Zenodo record."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import signal
import threading
from pathlib import Path

from . import http, rdffiles
from .backends import Backend, create
from .config import SyncConfig
from .zenodo import Zenodo, ZenodoRecord

log = logging.getLogger(__name__)

VOID = "http://rdfs.org/ns/void#"


# With SYNC_INTERVAL=0 (no periodic checks), retries after failures are still made, at most
# this far apart.
MAX_RETRY_WAIT = 6 * 3600


def next_wait(interval: int, retry_interval: int, failures: int) -> int:
    """Seconds to wait before the next check (0: no scheduled check, wait for a signal).

    After ``failures`` consecutive failed checks, retry sooner than the regular interval:
    ``retry_interval`` after the first failure, doubling at each further failure, never
    beyond the regular interval (or ``MAX_RETRY_WAIT`` when there is none).
    """
    if failures <= 0 or retry_interval <= 0:
        return interval
    cap = interval if interval > 0 else MAX_RETRY_WAIT
    return min(retry_interval * 2 ** min(failures - 1, 30), cap)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class State:
    """Sync state, persisted as JSON in the data directory and read by the linked-data server."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def update(self, **values) -> None:
        self.data.update(values)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)


def graph_template(template: str, record: ZenodoRecord):
    def graph_for(key: str) -> str | None:
        if template.lower() == "default":
            return None
        return template.format(key=key, recid=record.id, conceptrecid=record.conceptrecid,
                               doi=record.doi, conceptdoi=record.conceptdoi)
    return graph_for


def describe_store(backend: Backend) -> dict:
    """Collect information about the loaded data used by the landing page and the LD server."""
    info: dict = {}
    both = "{{ {p} }} UNION {{ GRAPH ?g {{ {p} }} }}"
    queries = {
        "uri_spaces": f"SELECT DISTINCT ?x WHERE {{ {both.format(p=f'?d <{VOID}uriSpace> ?x')} }} LIMIT 50",
        "example_resources": f"SELECT DISTINCT ?x WHERE {{ {both.format(p=f'?d <{VOID}exampleResource> ?x')} }} LIMIT 20",
    }
    for key, query in queries.items():
        try:
            info[key] = [row["x"]["value"] for row in http.select(backend.query_endpoint, query, auth=backend.auth)]
        except Exception as err:  # noqa: BLE001 - informative only
            log.warning("Query for %s failed: %s", key, err)
    try:
        rows = http.select(backend.query_endpoint,
                           "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY DESC(?n) LIMIT 500",
                           auth=backend.auth, timeout=600)
        info["graphs"] = [{"iri": r["g"]["value"], "triples": int(r["n"]["value"])} for r in rows]
    except Exception as err:  # noqa: BLE001
        log.warning("Graph statistics query failed: %s", err)
    return info


class Syncer:
    #: How often the waiting loop wakes up to supervise the store and look at signals.
    poll_seconds: float = 5

    def __init__(self, config: SyncConfig):
        self.config = config
        self.zenodo = Zenodo(config.zenodo_api, config.zenodo_token)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = State(config.data_dir / "state.json")
        self.backend = create(config.backend, config.data_dir)
        self.check_now = threading.Event()  # SIGHUP: check Zenodo now
        self.reload = threading.Event()  # SIGUSR1: reload the current release even if up to date
        self.stop = threading.Event()
        self.failures = 0  # consecutive failed checks

    def sync_once(self, force: bool = False) -> bool:
        """Check Zenodo and load a new release if there is one. Returns True if data changed."""
        cfg = self.config
        record = self.zenodo.record(cfg.record, cfg.follow_latest)
        self.state.update(last_check=now())
        keys = rdffiles.select([f.key for f in record.files], cfg.include, cfg.exclude)
        loaded = self.state.data.get("record", {})
        up_to_date = (loaded.get("id") == record.id and self.state.data.get("loaded_files") == keys
                      and self.state.data.get("status") == "ok" and self.state.data.get("backend") == self.backend.name)
        if up_to_date and not force:
            log.info("Up to date with %s (version %s)", record.doi, record.version or "-")
            return False
        if not keys:
            raise RuntimeError(f"No RDF files to load in record {record.id} "
                               f"(files: {[f.key for f in record.files]}); set KG_FILES")
        log.info("Syncing %s version %s (%s): %s", record.doi, record.version or "-", record.title, keys)
        self.state.update(status="syncing", syncing_record=record.id)

        download_dir = cfg.data_dir / "downloads" / record.id
        items: list[rdffiles.LoadItem] = []
        graph_for = graph_template(cfg.triples_graph, record)
        for f in record.files:
            if f.key in keys:
                path = self.zenodo.download(f, download_dir / f.key)
                items.extend(rdffiles.load_items(f.key, path, graph_for))

        tag = f"{record.id}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}"
        store = self.backend.load(items, tag)
        log.info("Loaded %s triples from record %s", store.get("triples"), record.id)

        if not cfg.keep_downloads:
            shutil.rmtree(cfg.data_dir / "downloads", ignore_errors=True)
        else:
            for old in (cfg.data_dir / "downloads").iterdir():
                if old.name != record.id:
                    shutil.rmtree(old, ignore_errors=True)

        self.write_hash_index()
        self.state.update(
            status="ok", backend=self.backend.name, record=record.to_dict(), loaded_files=keys,
            store=store, last_sync=now(), last_error=None, syncing_record=None,
            **describe_store(self.backend),
        )
        return True

    def write_hash_index(self) -> None:
        """Map document IRIs to the hash IRIs they contain (``doc`` -> ``doc#a``, ``doc#b``)."""
        index: dict[str, list[str]] = {}
        try:
            for iri in self.backend.hash_subjects():
                index.setdefault(iri.split("#", 1)[0], []).append(iri)
        except Exception as err:  # noqa: BLE001 - hash IRIs are then not dereferenceable
            log.warning("Could not build the hash IRI index: %s", err)
        path = self.config.data_dir / "hash-index.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index))
        os.replace(tmp, path)
        log.info("Hash IRI index: %d documents", len(index))

    def _safe_sync(self, force: bool) -> bool:
        """Run one check; on failure keep the current data online. Returns True on success."""
        try:
            self.sync_once(force)
        except Exception as err:  # noqa: BLE001 - keep the daemon (and the current data) alive
            self.failures += 1
            log.exception("Sync failed (%d consecutive failure%s)", self.failures, "s" if self.failures > 1 else "")
            self.state.update(status="error" if not self.state.data.get("store") else "ok",
                              last_error=f"{now()}: {err}", syncing_record=None, failures=self.failures)
            return False
        if self.failures:
            log.info("Sync succeeded after %d failed attempt%s", self.failures, "s" if self.failures > 1 else "")
        self.failures = 0
        self.state.update(failures=0)
        return True

    def run(self, once: bool = False, force: bool = False) -> None:
        if threading.current_thread() is threading.main_thread():  # not in tests' worker threads
            signal.signal(signal.SIGHUP, lambda *_: self.check_now.set())
            signal.signal(signal.SIGUSR1, lambda *_: (self.reload.set(), self.check_now.set()))
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        try:
            if not once:
                self.backend.resume(self.state.data)
            self._safe_sync(force)
            if once:
                return
            while not self.stop.is_set():
                wait = next_wait(self.config.interval, self.config.retry_interval, self.failures)
                if self.failures and wait > 0:
                    log.info("Retrying in %ss", wait)
                next_check = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=wait)
                self.state.update(next_check=next_check.replace(microsecond=0).isoformat() if wait > 0 else None)
                waited = 0
                while not self.stop.is_set() and not self.check_now.is_set() and (wait <= 0 or waited < wait):
                    self.stop.wait(self.poll_seconds)
                    waited += self.poll_seconds
                    self.backend.check()
                if self.stop.is_set():
                    break
                forced = self.reload.is_set()
                if self.check_now.is_set():
                    log.info("Reload requested (SIGUSR1)" if forced else "Check requested (SIGHUP)")
                self.check_now.clear()
                self.reload.clear()
                self._safe_sync(forced)
        finally:
            self.backend.shutdown()
