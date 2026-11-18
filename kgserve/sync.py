"""The sync service: keep the triple store in line with the latest release of a Zenodo record.

It runs as a long-lived process (its own container) that checks the record every
``syncInterval`` seconds, retries sooner after failures, and can be woken up with signals:
SIGHUP checks now, SIGUSR1 reloads the current release.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import signal
import threading
from pathlib import Path

from . import rdffiles
from .backends import Store
from .hashindex import HashIndex
from .config import Config
from .zenodo import Zenodo, ZenodoRecord

log = logging.getLogger(__name__)

VOID = "http://rdfs.org/ns/void#"

# With syncInterval 0 (no periodic checks), failed checks are still retried, at most this far apart.
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


def describe_store(store: Store) -> dict:
    """Collect information about the loaded data used by the landing page and the LD server."""
    info: dict = {}
    both = "{{ {p} }} UNION {{ GRAPH ?g {{ {p} }} }}"
    queries = {
        "uri_spaces": f"SELECT DISTINCT ?x WHERE {{ {both.format(p=f'?d <{VOID}uriSpace> ?x')} }} LIMIT 50",
        "example_resources": f"SELECT DISTINCT ?x WHERE {{ {both.format(p=f'?d <{VOID}exampleResource> ?x')} }} LIMIT 20",
    }
    for key, query in queries.items():
        try:
            info[key] = [row["x"]["value"] for row in store.select(query)]
        except Exception as err:  # noqa: BLE001 - informative only
            log.warning("Query for %s failed: %s", key, err)
    try:
        rows = store.select("SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } "
                            "GROUP BY ?g ORDER BY DESC(?n) LIMIT 500")
        info["graphs"] = [{"iri": r["g"]["value"], "triples": int(r["n"]["value"])} for r in rows]
    except Exception as err:  # noqa: BLE001
        log.warning("Graph statistics query failed: %s", err)
    return info


class Syncer:
    def __init__(self, config: Config):
        self.config = config
        self.zenodo = Zenodo(config.zenodo_api, config.zenodo_token)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = State(config.data_dir / "state.json")
        self.store = Store(config.store)
        self.wake = threading.Event()  # set by signals
        self.reload = False  # SIGUSR1: reload even if up to date
        self.stop = False
        self.failures = 0  # consecutive failed checks

    def sync_once(self, force: bool = False) -> bool:
        """Check Zenodo and load a new release if there is one. Returns True if data changed."""
        cfg = self.config
        record = self.zenodo.record(cfg.zenodo, cfg.follow_latest)
        self.state.update(last_check=now())
        keys = rdffiles.select([f.key for f in record.files], cfg.files, cfg.exclude_files)
        loaded = self.state.data
        up_to_date = (loaded.get("record", {}).get("id") == record.id and loaded.get("loaded_files") == keys
                      and loaded.get("status") == "ok" and loaded.get("store_type") == cfg.store.type)
        # The store keeps its own volume: reload if it was emptied (e.g. recreated).
        if up_to_date and not force and not self.store.is_empty():
            log.info("Up to date with %s (version %s)", record.doi, record.version or "-")
            return False
        if not keys:
            raise RuntimeError(f"No RDF files to load in record {record.id} "
                               f"(files: {[f.key for f in record.files]}); set \"files\" in config.json")
        log.info("Loading %s version %s (%s): %s", record.doi, record.version or "-", record.title, keys)

        download_dir = cfg.data_dir / "downloads" / record.id
        items: list[rdffiles.LoadItem] = []
        graph_for = graph_template(cfg.triples_graph, record)
        for f in record.files:
            if f.key in keys:
                path = self.zenodo.download(f, download_dir / f.key)
                items.extend(rdffiles.load_items(f.key, path, graph_for))
        # From here the store is being replaced: until the load ends its data are partial.
        self.state.update(status="loading", loading_record=record.id)
        hash_index = HashIndex()
        try:
            store = self.store.load(items, tap=hash_index.tap)
            self.write_hash_index(hash_index.finish())
        finally:
            shutil.rmtree(cfg.data_dir / "downloads", ignore_errors=True)
        store["tag"] = f"{record.id}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}"
        log.info("Loaded %s statements from record %s", store["triples"], record.id)

        self.state.update(
            status="ok", store_type=cfg.store.type, record=record.to_dict(), loaded_files=keys,
            store=store, last_sync=now(), last_error=None, loading_record=None,
            **describe_store(self.store),
        )
        return True

    def write_hash_index(self, index: dict[str, list[str]]) -> None:
        """Save the map of document IRIs to their hash IRIs, read by the linked-data server."""
        path = self.config.data_dir / "hash-index.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index))
        os.replace(tmp, path)
        log.info("Hash IRI index: %d documents", len(index))

    def _safe_sync(self, force: bool) -> bool:
        """Run one check, recording failures. Returns True on success."""
        try:
            self.sync_once(force)
        except Exception as err:  # noqa: BLE001 - keep the service alive
            self.failures += 1
            log.exception("Sync failed (%d consecutive failure%s)", self.failures, "s" if self.failures > 1 else "")
            # Data already loaded stay "ok" (e.g. Zenodo unreachable); a failed load does not.
            status = "ok" if self.state.data.get("status") == "ok" else "error"
            self.state.update(status=status, last_error=f"{now()}: {err}", loading_record=None,
                              failures=self.failures)
            return False
        if self.failures:
            log.info("Sync succeeded after %d failed attempt%s", self.failures, "s" if self.failures > 1 else "")
        self.failures = 0
        self.state.update(failures=0)
        return True

    def _signal(self, reload: bool = False, stop: bool = False) -> None:
        self.reload = self.reload or reload
        self.stop = self.stop or stop
        self.wake.set()

    def run(self, once: bool = False, force: bool = False) -> None:
        if threading.current_thread() is threading.main_thread():  # not in tests' worker threads
            signal.signal(signal.SIGHUP, lambda *_: self._signal())
            signal.signal(signal.SIGUSR1, lambda *_: self._signal(reload=True))
            signal.signal(signal.SIGTERM, lambda *_: self._signal(stop=True))
            signal.signal(signal.SIGINT, lambda *_: self._signal(stop=True))
        self._safe_sync(force)
        while not once and not self.stop:
            wait = next_wait(self.config.sync_interval, self.config.retry_interval, self.failures)
            if self.failures and wait > 0:
                log.info("Retrying in %ss", wait)
            next_check = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=wait)
            self.state.update(next_check=next_check.replace(microsecond=0).isoformat() if wait > 0 else None)
            woken = self.wake.wait(wait if wait > 0 else None)
            self.wake.clear()
            if self.stop:
                break
            force, self.reload = self.reload, False
            if woken:
                log.info("Reload requested (SIGUSR1)" if force else "Check requested (SIGHUP)")
            self._safe_sync(force)
