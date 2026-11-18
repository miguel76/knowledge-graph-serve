"""QLever backend (managed), for large knowledge graphs and analytic queries.

Each release is indexed offline with ``qlever-index`` into a fresh directory. Inputs are
streamed through named pipes: compressed files are decompressed on the fly, and formats
QLever does not read (RDF/XML, JSON-LD, TriG, N3) are converted to N-Quads by
``oxigraph convert``, so no uncompressed copy is written to disk.

The new index is then served by ``qlever-server`` on an internal port and verified
(statement count, hash IRIs) with an access token known only to this process, which lifts
the query timeout. Finally the public port, held by a :class:`~kgserve.proxy.SwitchProxy`,
is switched to the new server and the old one is stopped once its last connections end:
releases are swapped without downtime, however long the new server takes to start.

Without the access token the server is read-only (QLever refuses updates).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import http
from .backends import HASH_SUBJECTS_QUERY, NAMED_COUNT_QUERY, Backend, _env
from .proxy import SwitchProxy
from .rdffiles import LoadItem

log = logging.getLogger(__name__)

NATIVE_FORMATS = ("nt", "ttl", "nq")  # what qlever-index parses
DEFAULT_GRAPH_COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"


@dataclass
class IndexInput:
    """One ``-f`` argument of ``qlever-index``."""

    path: Path  # the file itself, or a named pipe fed while indexing
    format: str  # nt | ttl | nq
    graph: str  # graph IRI for triple formats, "-" for the default graph / quads
    item: LoadItem
    piped: bool  # fed through a named pipe (decompressed and/or converted)
    convert: bool  # converted to N-Quads by oxigraph

    @property
    def parallel(self) -> bool:
        # The parallel parser needs line-based input; Turtle may have multi-line literals.
        return self.format in ("nt", "nq")


def plan_inputs(items: list[LoadItem], workdir: Path) -> list[IndexInput]:
    inputs = []
    for i, item in enumerate(items):
        ext = item.format.ext
        native = ext in NATIVE_FORMATS
        fmt = ext if native else "nq"
        # Converted triple formats get their graph from `oxigraph convert --to-graph`.
        graph = item.graph if native and item.graph else "-"
        direct = native and item.member is None and item.format.compression is None
        path = item.path if direct else workdir / f"input-{i:03d}.{fmt}"
        inputs.append(IndexInput(path, fmt, graph, item, piped=not direct, convert=not native))
    return inputs


def index_command(index_bin: str, basename: Path, inputs: list[IndexInput], name: str,
                  memory: str = "", settings_file: str = "") -> list[str]:
    cmd = [index_bin, "-i", str(basename), "-K", name]
    for inp in inputs:
        cmd += ["-f", str(inp.path), "-F", inp.format, "-g", inp.graph, "-p", "true" if inp.parallel else "false"]
    if memory:
        cmd += ["-m", memory]
    if settings_file:
        cmd += ["-s", settings_file]
    return cmd


def convert_command(oxigraph_bin: str, item: LoadItem) -> list[str]:
    cmd = [oxigraph_bin, "convert", "--from-format", item.format.ext, "--to-format", "nq"]
    if item.graph and not item.format.dataset:
        cmd += ["--to-graph", item.graph]
    return cmd


class _Feeder(threading.Thread):
    """Writes the (decompressed, possibly converted) content of an input into its pipe."""

    def __init__(self, inp: IndexInput, oxigraph_bin: str):
        super().__init__(name=f"feed-{inp.path.name}", daemon=True)
        self.inp = inp
        self.oxigraph_bin = oxigraph_bin
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            with self.inp.item.open() as source, open(self.inp.path, "wb") as pipe:  # blocks until read
                if not self.inp.convert:
                    shutil.copyfileobj(source, pipe, 1 << 20)
                    return
                proc = subprocess.Popen(convert_command(self.oxigraph_bin, self.inp.item),
                                        stdin=subprocess.PIPE, stdout=pipe)
                assert proc.stdin is not None
                try:
                    shutil.copyfileobj(source, proc.stdin, 1 << 20)
                finally:
                    proc.stdin.close()
                if proc.wait() != 0:
                    raise RuntimeError(f"oxigraph convert failed on {self.inp.item.key} (code {proc.returncode})")
        except BaseException as err:  # noqa: BLE001 - reported by build_index
            self.error = err


def _timeout(value: str) -> str:
    return f"{value}s" if value.isdigit() else value


class QLeverBackend(Backend):
    name = "qlever"

    def __init__(self, data_dir: Path):
        self.index_bin = _env("QLEVER_INDEX_BIN", "qlever-index")
        self.server_bin = _env("QLEVER_SERVER_BIN", "qlever-server")
        self.oxigraph_bin = _env("OXIGRAPH_BIN", "oxigraph")
        host, _, port = _env("QLEVER_BIND", "0.0.0.0:7001").rpartition(":")
        self.ports = [int(p) for p in _env("QLEVER_INTERNAL_PORTS", "7011,7012").replace(",", " ").split()]
        self.memory = _env("QLEVER_MEMORY", "4G")
        self.timeout = _timeout(_env("QLEVER_TIMEOUT", "60"))
        self.query_threads = _env("QLEVER_QUERY_THREADS", "4")
        self.index_memory = _env("QLEVER_INDEX_MEMORY")
        self.index_settings = _env("QLEVER_INDEX_SETTINGS")
        self.server_args = shlex.split(_env("QLEVER_SERVER_ARGS"))
        self.drain_seconds = float(_env("QLEVER_DRAIN_SECONDS", "60"))
        self.stores_dir = data_dir / "stores"
        self.stores_dir.mkdir(parents=True, exist_ok=True)
        # Lets this process (only) lift the query timeout; never leaves the container.
        self.token = secrets.token_hex(24)
        self.servers: dict[int, tuple[subprocess.Popen, str]] = {}
        self.active_port: int | None = None
        self._hash_subjects: list[str] = []
        self.proxy = SwitchProxy(host or "0.0.0.0", int(port))
        self.proxy.start()

    @property
    def query_endpoint(self) -> str:  # type: ignore[override]
        port = self.active_port if self.active_port is not None else self.proxy.port
        return f"http://127.0.0.1:{port}/"

    # -- servers --------------------------------------------------------------------------

    def _start_server(self, tag: str, port: int) -> None:
        self._stop_server(port)
        cmd = [self.server_bin, "-i", str(self.stores_dir / tag / "kg"), "-p", str(port), "-m", self.memory,
               "-s", self.timeout, "-j", self.query_threads, "-a", self.token, *self.server_args]
        log.info("Starting QLever on index %s (port %s)", tag, port)
        proc = subprocess.Popen(cmd, cwd=self.stores_dir / tag)
        self.servers[port] = (proc, tag)
        while True:
            if proc.poll() is not None:
                del self.servers[port]
                raise RuntimeError(f"qlever-server exited with code {proc.returncode} on index {tag}")
            try:
                http.request("GET", f"http://127.0.0.1:{port}/?query=ASK%7B%7D", timeout=5, ok=(200,))
                return
            except OSError:
                time.sleep(0.5)  # large indexes take a while to load; no deadline

    def _stop_server(self, port: int) -> None:
        proc, _ = self.servers.pop(port, (None, None))
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _select(self, port: int, query: str, timeout_s: int = 3600) -> list[dict]:
        body, headers = http.form({"query": query, "timeout": f"{timeout_s}s", "access-token": self.token})
        headers["Accept"] = "application/sparql-results+json"
        resp = http.request("POST", f"http://127.0.0.1:{port}/", body, headers, timeout=timeout_s + 60, ok=(200,))
        return json.loads(resp.body)["results"]["bindings"]

    def resume(self, state: dict) -> None:
        tag = state.get("store", {}).get("tag")
        if tag and (self.stores_dir / tag).is_dir():
            self._start_server(tag, self.ports[0])
            self.active_port = self.ports[0]
            self.proxy.switch(("127.0.0.1", self.ports[0]))

    def check(self) -> None:
        if self.active_port is None or self.active_port not in self.servers:
            return
        proc, tag = self.servers[self.active_port]
        if proc.poll() is not None:
            log.error("qlever-server exited unexpectedly (code %s), restarting", proc.returncode)
            self.proxy.switch(None)
            self._start_server(tag, self.active_port)
            self.proxy.switch(("127.0.0.1", self.active_port))

    def shutdown(self) -> None:
        for port in list(self.servers):
            self._stop_server(port)
        self.proxy.stop()

    def hash_subjects(self) -> list[str]:
        return self._hash_subjects

    # -- loading --------------------------------------------------------------------------

    def build_index(self, items: list[LoadItem], target: Path, name: str) -> None:
        inputs = plan_inputs(items, target)
        feeders = []
        for inp in inputs:
            if inp.piped:
                os.mkfifo(inp.path)
                feeders.append(_Feeder(inp, self.oxigraph_bin))
        cmd = index_command(self.index_bin, target / "kg", inputs, name, self.index_memory, self.index_settings)
        log.info("$ %s", " ".join(cmd))
        for feeder in feeders:
            feeder.start()
        returncode = subprocess.run(cmd, cwd=target).returncode
        # Unblock feeders still waiting for the indexer to open their pipe (if it failed).
        for feeder in feeders:
            if feeder.is_alive():
                try:
                    os.close(os.open(feeder.inp.path, os.O_RDONLY | os.O_NONBLOCK))
                except OSError:
                    pass
            feeder.join(30)
        for inp in inputs:
            if inp.piped:
                inp.path.unlink(missing_ok=True)
        errors = [f"{f.inp.item.key}: {f.error}" for f in feeders if f.error is not None]
        if returncode != 0:
            raise RuntimeError(f"qlever-index failed with code {returncode}" + (f" ({'; '.join(errors)})" if errors else ""))
        if errors:
            raise RuntimeError("could not read " + "; ".join(errors))

    def load(self, items: list[LoadItem], tag: str) -> dict:
        target = self.stores_dir / tag
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        started = time.monotonic()
        port = next(p for p in self.ports if p != self.active_port)
        try:
            self.build_index(items, target, tag)
            index_seconds = time.monotonic() - started
            self._start_server(tag, port)
            named = int(self._select(port, NAMED_COUNT_QUERY)[0]["n"]["value"])
            default = int(self._select(port, DEFAULT_GRAPH_COUNT_QUERY)[0]["n"]["value"])
            triples = max(named, default)  # quads in named graphs, or triples if all in the default graph
            if triples == 0:
                raise RuntimeError("the new index is empty")
            hash_subjects = [row["s"]["value"] for row in self._select(port, HASH_SUBJECTS_QUERY)]
        except BaseException:
            self._stop_server(port)
            shutil.rmtree(target, ignore_errors=True)
            raise

        # Zero-downtime swap: new connections go to the new server, the old one drains.
        old_port = self.active_port
        self.proxy.switch(("127.0.0.1", port))
        self.active_port = port
        self._hash_subjects = hash_subjects
        if old_port is not None:
            if not self.proxy.drain(("127.0.0.1", old_port), self.drain_seconds):
                log.warning("Connections to the previous index still open after %ss, closing", self.drain_seconds)
            self._stop_server(old_port)
        for old in self.stores_dir.iterdir():
            if old.name != tag:
                shutil.rmtree(old, ignore_errors=True)
        return {"tag": tag, "triples": triples, "load_seconds": round(index_seconds, 1),
                "total_seconds": round(time.monotonic() - started, 1)}
