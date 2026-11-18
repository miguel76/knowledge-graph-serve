import gzip
import json
import zipfile
from pathlib import Path

import pytest

from kgserve import rdffiles
from kgserve.config import Config, ConfigError, StoreConfig
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
        if file.key.endswith(".nq.gz"):
            dest.write_bytes(gzip.compress(b"<http://ex.org/a#x> <http://ex.org/p> <http://ex.org/b#z> <http://ex.org/g> .\n"))
        else:
            dest.write_bytes(b"<http://ex.org/a#y> <http://ex.org/p> <http://ex.org/b> .\n")
        return dest


class FakeStore:
    def __init__(self):
        self.loads = []
        self.empty = False

    def load(self, items, tap=None):
        self.loads.append([item.key for item in items])
        for item in items:  # stream each file through the tap, as the real loader does
            stream = tap(item, item.open()) if tap else item.open()
            with stream:
                while stream.read(7):
                    pass
        self.empty = False
        return {"triples": 1, "load_seconds": 0.1}

    def is_empty(self):
        return self.empty


def test_sync_once(tmp_path, monkeypatch):
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    monkeypatch.setattr("kgserve.sync.describe_store", lambda store: {"uri_spaces": ["http://ex.org/"]})
    syncer = Syncer(Config(zenodo="24305088", data_dir=tmp_path))
    syncer.zenodo = FakeZenodo(record)
    syncer.store = FakeStore()

    assert syncer.sync_once() is True
    assert syncer.store.loads == [["kg.nq.gz", "void.ttl"]]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "ok"
    assert state["record"]["id"] == "24305089"
    assert state["uri_spaces"] == ["http://ex.org/"]
    # From the N-Quads stream (a#x) and from parsing the Turtle file (a#y); b#z is an object.
    assert json.loads((tmp_path / "hash-index.json").read_text()) == {
        "http://ex.org/a": ["http://ex.org/a#x", "http://ex.org/a#y"]}
    assert not (tmp_path / "downloads").exists()  # removed after loading

    # Same version: nothing to do. Forced: reload.
    assert syncer.sync_once() is False
    assert syncer.sync_once(force=True) is True
    assert len(syncer.store.loads) == 2

    # Different file selection: reload.
    syncer.config.files = ["kg.nq.gz"]
    assert syncer.sync_once() is True
    assert syncer.store.loads[-1] == ["kg.nq.gz"]

    # The store lost its data (e.g. its volume was recreated): reload.
    assert syncer.sync_once() is False
    syncer.store.empty = True
    assert syncer.sync_once() is True


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
    monkeypatch.setattr("kgserve.sync.describe_store", lambda store: {})
    syncer = Syncer(Config(zenodo="24305088", data_dir=tmp_path, sync_interval=3600, retry_interval=1))
    syncer.zenodo = FlakyZenodo(record, failures=2)
    syncer.store = FakeStore()

    thread = threading.Thread(target=syncer.run)
    thread.start()
    try:
        # Fails at start-up, retries after 1 s, fails again, retries after 2 s, then loads.
        deadline = time.monotonic() + 15
        while not syncer.store.loads and time.monotonic() < deadline:
            time.sleep(0.05)
        assert syncer.store.loads, "the sync was not retried"
        assert syncer.zenodo.calls == 3
        time.sleep(0.2)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["failures"] == 0 and state["status"] == "ok"
        assert syncer.failures == 0
    finally:
        syncer._signal(stop=True)
        thread.join(5)
    assert not thread.is_alive()


def test_failure_state(tmp_path, monkeypatch):
    record = ZenodoRecord.from_api(json.loads((HERE / "zenodo_record.json").read_text()))
    syncer = Syncer(Config(zenodo="24305088", data_dir=tmp_path))
    syncer.zenodo = FlakyZenodo(record, failures=5)
    syncer.store = FakeStore()
    assert syncer._safe_sync(False) is False
    assert syncer._safe_sync(False) is False
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["failures"] == 2
    assert "Zenodo is unreachable" in state["last_error"]
    assert state["status"] == "error"  # nothing loaded yet


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_STORE_TYPE", "fuseki")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "config.json"
    path.write_text('{"zenodo": "10.5281/zenodo.24305088", "files": ["*.nq.gz"], "resourcePath": "/res/"}')
    config = Config.load(path)
    assert config.zenodo == "10.5281/zenodo.24305088"
    assert config.files == ["*.nq.gz"]
    assert config.resource_path == "res"
    assert config.sync_interval == 3600 and config.base_iri == ""  # defaults
    assert config.store.type == "fuseki"
    assert config.store.query == "http://store:3030/ds/sparql"
    assert config.data_dir == tmp_path / "data"


@pytest.mark.parametrize("content,message", [
    ("{}", '"zenodo"'),
    ('{"zenodo": "1", "syncIntervall": 5}', "Unknown option"),
    ('{"zenodo": "1", "syncInterval": "1h"}', "must be a int"),
    ('{"zenodo": "1", "followLatest": 1}', "must be a bool"),
    ('{"zenodo": "1", "files": "*.nq"}', "must be a list"),
    ('{"zenodo": "1",}', "not valid JSON"),
    ("[]", "JSON object"),
])
def test_config_errors(tmp_path, content, message):
    path = tmp_path / "config.json"
    path.write_text(content)
    with pytest.raises(ConfigError, match=message):
        Config.load(path)


def test_config_missing_file_mounted_as_directory(tmp_path):
    # Docker creates a directory when a bind-mounted file does not exist.
    (tmp_path / "config.json").mkdir()
    with pytest.raises(ConfigError, match="is a directory"):
        Config.load(tmp_path / "config.json")


def test_store_defaults(monkeypatch):
    monkeypatch.setenv("KG_STORE_TYPE", "graphdb")
    monkeypatch.setenv("KG_STORE_REPOSITORY", "cckg")
    store = StoreConfig.from_env()
    assert store.query == "http://store:7200/repositories/cckg"
    assert store.data == "http://store:7200"
    monkeypatch.setenv("KG_STORE_TYPE", "nope")
    with pytest.raises(ConfigError):
        StoreConfig.from_env()
