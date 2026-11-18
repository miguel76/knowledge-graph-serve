"""Configuration, read from environment variables (see ``.env.example``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [item for item in _env(name).replace(",", " ").split() if item]


def _path_prefix(value: str) -> str:
    """Normalise a URL path prefix to ``/a/b`` form (no trailing slash, ``""`` for root)."""
    value = value.strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


@dataclass
class SyncConfig:
    record: str
    zenodo_api: str = "https://zenodo.org/api"
    zenodo_token: str = ""
    follow_latest: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    triples_graph: str = "https://doi.org/{conceptdoi}#{key}"
    data_dir: Path = Path("/data")
    backend: str = "oxigraph"
    interval: int = 3600
    retry_interval: int = 300
    keep_downloads: bool = False

    @classmethod
    def from_env(cls) -> "SyncConfig":
        record = _env("ZENODO_RECORD")
        if not record:
            raise SystemExit("ZENODO_RECORD is not set (a Zenodo record id, DOI or URL)")
        return cls(
            record=record,
            zenodo_api=_env("ZENODO_API", "https://zenodo.org/api").rstrip("/"),
            zenodo_token=_env("ZENODO_TOKEN"),
            follow_latest=_bool("ZENODO_FOLLOW_LATEST", True),
            include=_list("KG_FILES"),
            exclude=_list("KG_EXCLUDE_FILES"),
            triples_graph=_env("TRIPLES_GRAPH", "https://doi.org/{conceptdoi}#{key}"),
            data_dir=Path(_env("DATA_DIR", "/data")),
            backend=_env("BACKEND", "oxigraph").lower(),
            interval=int(_env("SYNC_INTERVAL", "3600")),
            retry_interval=int(_env("SYNC_RETRY_INTERVAL", "300")),
            keep_downloads=_bool("KEEP_DOWNLOADS", False),
        )


@dataclass
class LDConfig:
    sparql_endpoint: str
    public_url: str
    base_path: str
    resource_path: str
    base_iri: str = ""
    data_dir: Path = Path("/data")
    bind: str = "0.0.0.0"
    port: int = 8000
    public_sparql_url: str = ""
    describe_limit: int = 5000
    inverse_limit: int = 50
    hash_iri_lookup: bool = True
    title: str = ""

    @classmethod
    def from_env(cls) -> "LDConfig":
        base_path = _path_prefix(_env("BASE_PATH", "/kg"))
        public_url = _env("PUBLIC_URL", "http://localhost:8080").rstrip("/")
        return cls(
            sparql_endpoint=_env("SPARQL_ENDPOINT", "http://store:7878/query"),
            public_url=public_url,
            base_path=base_path,
            resource_path=_path_prefix(_env("RESOURCE_PATH", base_path + "/id")) + "/",
            base_iri=_env("KG_BASE_IRI"),
            data_dir=Path(_env("DATA_DIR", "/data")),
            bind=_env("LD_BIND", "0.0.0.0"),
            port=int(_env("LD_PORT", "8000")),
            public_sparql_url=_env("PUBLIC_SPARQL_URL", public_url + base_path + "/sparql"),
            describe_limit=int(_env("LD_DESCRIBE_LIMIT", "5000")),
            inverse_limit=int(_env("LD_INVERSE_LIMIT", "50")),
            hash_iri_lookup=_bool("LD_HASH_IRI_LOOKUP", True),
            title=_env("KG_TITLE"),
        )
