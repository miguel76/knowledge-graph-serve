"""Configuration.

Everything about the knowledge graph comes from one JSON file (``config.json``), where only
``zenodo`` is required. How to reach the triple store is container wiring, set by the
compose files through ``KG_STORE_*`` environment variables.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/config/config.json"

# config.json key -> (attribute, type, default)
OPTIONS: dict[str, tuple[str, type, object]] = {
    "zenodo": ("zenodo", str, None),
    "followLatest": ("follow_latest", bool, True),
    "files": ("files", list, []),
    "excludeFiles": ("exclude_files", list, []),
    "triplesGraph": ("triples_graph", str, "https://doi.org/{conceptdoi}#{key}"),
    "syncInterval": ("sync_interval", int, 3600),
    "retryInterval": ("retry_interval", int, 300),
    "zenodoApi": ("zenodo_api", str, "https://zenodo.org/api"),
    "zenodoToken": ("zenodo_token", str, ""),
    "baseIri": ("base_iri", str, ""),
    "resourcePath": ("resource_path", str, "id"),
    "title": ("title", str, ""),
    "describeLimit": ("describe_limit", int, 5000),
    "inverseLimit": ("inverse_limit", int, 50),
}

# Store type -> default endpoints (query, update, data) of the store container.
STORE_DEFAULTS = {
    "oxigraph": ("http://store:7878/query", "http://store:7878/update", "http://store:7878/store?no_transaction"),
    "fuseki": ("http://store:3030/ds/sparql", "http://store:3030/ds/update", "http://store:3030/ds/data"),
    "graphdb": ("http://store:7200/repositories/{repository}", "", "http://store:7200"),
}


class ConfigError(SystemExit):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class StoreConfig:
    """Connection to the triple store (from KG_STORE_* environment variables)."""

    type: str = "oxigraph"
    query: str = ""
    update: str = ""
    data: str = ""
    repository: str = "kg"
    user: str = ""
    password: str = ""

    @property
    def auth(self) -> tuple[str, str] | None:
        return (self.user, self.password) if self.user else None

    @classmethod
    def from_env(cls) -> "StoreConfig":
        kind = _env("KG_STORE_TYPE", "oxigraph").lower()
        if kind not in STORE_DEFAULTS:
            raise ConfigError(f"KG_STORE_TYPE={kind!r}: expected one of {', '.join(STORE_DEFAULTS)}")
        repository = _env("KG_STORE_REPOSITORY", "kg")
        query, update, data = (v.format(repository=repository) for v in STORE_DEFAULTS[kind])
        return cls(type=kind, query=_env("KG_STORE_QUERY", query), update=_env("KG_STORE_UPDATE", update),
                   data=_env("KG_STORE_DATA", data), repository=repository,
                   user=_env("KG_STORE_USER"), password=_env("KG_STORE_PASSWORD"))


@dataclass
class Config:
    zenodo: str
    follow_latest: bool = True
    files: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    triples_graph: str = "https://doi.org/{conceptdoi}#{key}"
    sync_interval: int = 3600
    retry_interval: int = 300
    zenodo_api: str = "https://zenodo.org/api"
    zenodo_token: str = ""
    base_iri: str = ""
    resource_path: str = "id"
    title: str = ""
    describe_limit: int = 5000
    inverse_limit: int = 50
    store: StoreConfig = field(default_factory=StoreConfig)
    data_dir: Path = Path("/data")

    @classmethod
    def from_dict(cls, values: dict, store: StoreConfig | None = None, data_dir: Path | None = None) -> "Config":
        if not isinstance(values, dict):
            raise ConfigError("config.json must contain a JSON object")
        unknown = sorted(set(values) - set(OPTIONS))
        if unknown:
            raise ConfigError(f"Unknown option(s) in config.json: {', '.join(unknown)} "
                              f"(known: {', '.join(OPTIONS)})")
        kwargs: dict = {}
        for key, (attr, kind, _) in OPTIONS.items():
            if key not in values:
                continue
            value = values[key]
            if kind is int and isinstance(value, bool) or not isinstance(value, kind):
                raise ConfigError(f"config.json: {key!r} must be a {kind.__name__}, not {value!r}")
            if kind is list and not all(isinstance(v, str) for v in value):
                raise ConfigError(f"config.json: {key!r} must be a list of strings")
            kwargs[attr] = value
        if not kwargs.get("zenodo"):
            raise ConfigError('config.json: "zenodo" (the record DOI, id or URL) is required')
        kwargs["resource_path"] = kwargs.get("resource_path", "id").strip("/") or "id"
        return cls(**kwargs, store=store or StoreConfig(), data_dir=data_dir or Path("/data"))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or _env("KG_CONFIG", DEFAULT_CONFIG_PATH))
        if path.is_dir():
            # Docker creates a directory when the bind-mounted file does not exist.
            raise ConfigError(f"{path} is a directory: create config.json next to compose.yaml, "
                              'e.g. {"zenodo": "10.5281/zenodo.24305088"}')
        try:
            values = json.loads(path.read_text())
        except FileNotFoundError:
            raise ConfigError(f"Configuration file not found: {path}") from None
        except json.JSONDecodeError as err:
            raise ConfigError(f"{path} is not valid JSON: {err}") from None
        return cls.from_dict(values, StoreConfig.from_env(), Path(_env("DATA_DIR", "/data")))
