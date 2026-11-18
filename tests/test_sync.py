import gzip
import json
import zipfile
from pathlib import Path

import pytest

from kgserve import rdffiles
from kgserve.config import SyncConfig
from kgserve.sync import Syncer, graph_template
from kgserve.zenodo import ZenodoRecord, parse_record_ref

HERE = Path(__file__).parent


@pytest.mark.parametrize("ref", [
    "24305088",
    "10.5281/zenodo.24305088",
    "https://doi.org/10.5281/zenodo.24305088",
    "doi:10.5281/ZENODO.24305088",
    "https://zenodo.org/records/24305088",
    "https://zenodo.org/record/24305088",
    "https://zenodo.org/api/records/24305088/versions/latest",
])
def test_parse_record_ref(ref):
    assert parse_record_ref(ref) == "24305088"


def test_parse_record_ref_rejects_garbage():
    with pytest.raises(ValueError):
        parse_record_ref("https://example.org/dataset")


def test_record_from_api():
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    assert record.id == "24305089"
    assert record.conceptrecid == "24305088"
    assert record.conceptdoi == "10.5281/zenodo.24305088"
    assert record.license == "cc-by-4.0"
    assert [f.key for f in record.files] == ["kg.nq.gz", "void.ttl"]
    assert record.files[0].checksum.startswith("md5:")


@pytest.mark.parametrize("name,expected", [
    ("kg.nq.gz", ("nq", True, "gz")),
    ("void.ttl", ("ttl", False, None)),
    ("onto.owl", ("rdf", False, None)),
    ("data.trig.xz", ("trig", True, "xz")),
    ("Data.NT.BZ2", ("nt", False, "bz2")),
    ("x.jsonld", ("jsonld", True, None)),
])
def test_detect(name, expected):
    fmt = rdffiles.detect(name)
    assert (fmt.ext, fmt.dataset, fmt.compression) == expected


@pytest.mark.parametrize("name", ["README.md", "kg.csv.gz", "gz", "data.tar.gz"])
def test_detect_non_rdf(name):
    assert rdffiles.detect(name) is None


def test_select():
    names = ["kg.nq.gz", "void.ttl", "README.md", "dump.zip", "sample.ttl"]
    assert rdffiles.select(names, [], []) == ["kg.nq.gz", "void.ttl", "dump.zip", "sample.ttl"]
    assert rdffiles.select(names, [], ["sample.*"]) == ["kg.nq.gz", "void.ttl", "dump.zip"]
    assert rdffiles.select(names, ["*.nq.gz"], []) == ["kg.nq.gz"]


def test_load_items_and_open(tmp_path):
    nq = b'<http://ex.org/s> <http://ex.org/p> "o" <http://ex.org/g> .\n'
    ttl = b"<http://ex.org/a> <http://ex.org/p> <http://ex.org/b> .\n"
    (tmp_path / "kg.nq.gz").write_bytes(gzip.compress(nq))
    with zipfile.ZipFile(tmp_path / "dump.zip", "w") as archive:
        archive.writestr("data/a.ttl", ttl)
        archive.writestr("data/readme.txt", "not rdf")
    graph_for = lambda key: f"urn:graph:{key}"

    [gz] = rdffiles.load_items("kg.nq.gz", tmp_path / "kg.nq.gz", graph_for)
    assert gz.graph is None  # quads keep their own graphs
    assert gz.native_path == tmp_path / "kg.nq.gz"
    with gz.open() as stream:
        assert stream.read() == nq

    [member] = rdffiles.load_items("dump.zip", tmp_path / "dump.zip", graph_for)
    assert member.key == "dump.zip/data/a.ttl"
    assert member.graph == "urn:graph:dump.zip/data/a.ttl"
    assert member.native_path is None  # must be piped
    with member.open() as stream:
        assert stream.read() == ttl


def test_graph_template():
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    assert graph_template("https://doi.org/{conceptdoi}#{key}", record)("void.ttl") == \
        "https://doi.org/10.5281/zenodo.24305088#void.ttl"
    assert graph_template("default", record)("void.ttl") is None


class FakeZenodo:
    def __init__(self, record):
        self.record_data = record
        self.downloads = []

    def record(self, ref, follow_latest=True):
        return self.record_data

    def download(self, file, dest):
        self.downloads.append(file.key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"<http://ex.org/a#x> <http://ex.org/p> <http://ex.org/b> .\n")
        return dest


class FakeBackend:
    name = "fake"
    query_endpoint = "http://unused"
    auth = None

    def __init__(self):
        self.loads = []

    def load(self, items, tag):
        self.loads.append([item.key for item in items])
        return {"tag": tag, "triples": 1}

    def hash_subjects(self):
        return ["http://ex.org/a#x", "http://ex.org/a#y", "http://ex.org/b#z"]


def test_sync_once(tmp_path, monkeypatch):
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    monkeypatch.setattr("kgserve.sync.describe_store", lambda backend: {"uri_spaces": ["http://ex.org/"]})
    syncer = Syncer(SyncConfig(record="24305088", data_dir=tmp_path, backend="oxigraph"))
    syncer.zenodo = FakeZenodo(record)
    syncer.backend = FakeBackend()

    # kg.nq.gz is not really gzipped here: only names matter to the fake backend.
    assert syncer.sync_once() is True
    assert syncer.backend.loads == [["kg.nq.gz", "void.ttl"]]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "ok"
    assert state["record"]["id"] == "24305089"
    assert state["uri_spaces"] == ["http://ex.org/"]
    assert json.loads((tmp_path / "hash-index.json").read_text()) == {
        "http://ex.org/a": ["http://ex.org/a#x", "http://ex.org/a#y"], "http://ex.org/b": ["http://ex.org/b#z"]}
    assert not (tmp_path / "downloads").exists()  # removed after loading

    # Same version: nothing to do. Forced: reload.
    assert syncer.sync_once() is False
    assert syncer.sync_once(force=True) is True
    assert len(syncer.backend.loads) == 2

    # Different file selection: reload.
    syncer.config.include = ["kg.nq.gz"]
    assert syncer.sync_once() is True
    assert syncer.backend.loads[-1] == ["kg.nq.gz"]


@pytest.mark.parametrize("interval,retry,failures,expected", [
    (3600, 300, 0, 3600),  # no failure: regular interval
    (3600, 300, 1, 300),  # first retry
    (3600, 300, 2, 600),  # doubling
    (3600, 300, 4, 2400),
    (3600, 300, 5, 3600),  # capped at the regular interval
    (3600, 300, 500, 3600),  # no overflow
    (600, 900, 1, 600),  # retry longer than the interval: interval wins
    (3600, 0, 3, 3600),  # retries disabled
    (0, 300, 0, 0),  # manual mode, no failure: wait for a signal
    (0, 300, 2, 600),  # manual mode still retries failures
    (0, 300, 99, 6 * 3600),  # ... at most every 6 hours
])
def test_next_wait(interval, retry, failures, expected):
    from kgserve.sync import next_wait

    assert next_wait(interval, retry, failures) == expected


class FlakyZenodo(FakeZenodo):
    """Fails the first ``failures`` record lookups, as if Zenodo were down."""

    def __init__(self, record, failures):
        super().__init__(record)
        self.failures = failures
        self.calls = 0

    def record(self, ref, follow_latest=True):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("Zenodo is unreachable")
        return self.record_data


def test_failed_sync_is_retried_sooner(tmp_path, monkeypatch):
    import threading
    import time

    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    monkeypatch.setattr("kgserve.sync.describe_store", lambda backend: {})
    config = SyncConfig(record="24305088", data_dir=tmp_path, interval=3600, retry_interval=1)
    syncer = Syncer(config)
    syncer.poll_seconds = 0.05
    syncer.zenodo = FlakyZenodo(record, failures=2)
    syncer.backend = FakeBackend()
    syncer.backend.shutdown = lambda: None
    syncer.backend.check = lambda: None
    syncer.backend.resume = lambda state: None

    thread = threading.Thread(target=syncer.run)
    thread.start()
    try:
        # Fails at start-up, retries after 1 s, fails again, retries after 2 s, then loads.
        deadline = time.monotonic() + 15
        while not syncer.backend.loads and time.monotonic() < deadline:
            time.sleep(0.05)
        assert syncer.backend.loads, "the sync was not retried"
        assert syncer.zenodo.calls == 3
        time.sleep(0.2)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["failures"] == 0 and state["status"] == "ok"
        assert syncer.failures == 0
    finally:
        syncer.stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_failure_state(tmp_path, monkeypatch):
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    syncer = Syncer(SyncConfig(record="24305088", data_dir=tmp_path))
    syncer.zenodo = FlakyZenodo(record, failures=5)
    syncer.backend = FakeBackend()
    assert syncer._safe_sync(False) is False
    assert syncer._safe_sync(False) is False
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["failures"] == 2
    assert "Zenodo is unreachable" in state["last_error"]
    assert state["status"] == "error"  # nothing loaded yet
