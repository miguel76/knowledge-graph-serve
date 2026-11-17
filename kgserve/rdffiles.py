"""Recognise RDF dump files (possibly compressed or zipped) and turn them into load items."""

from __future__ import annotations

import bz2
import fnmatch
import gzip
import lzma
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

# extension -> (canonical extension understood by Oxigraph, media type, is a dataset (quads) format)
FORMATS: dict[str, tuple[str, str, bool]] = {
    "nt": ("nt", "application/n-triples", False),
    "ntriples": ("nt", "application/n-triples", False),
    "nq": ("nq", "application/n-quads", True),
    "nquads": ("nq", "application/n-quads", True),
    "ttl": ("ttl", "text/turtle", False),
    "turtle": ("ttl", "text/turtle", False),
    "trig": ("trig", "application/trig", True),
    "rdf": ("rdf", "application/rdf+xml", False),
    "owl": ("rdf", "application/rdf+xml", False),
    "xml": ("rdf", "application/rdf+xml", False),
    "jsonld": ("jsonld", "application/ld+json", True),
    "n3": ("n3", "text/n3", False),
}

COMPRESSIONS = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}


@dataclass(frozen=True)
class RdfFormat:
    ext: str
    media_type: str
    dataset: bool
    compression: str | None  # None, "gz", "bz2", "xz"


def detect(name: str) -> RdfFormat | None:
    """Detect the RDF format of a file name like ``kg.nq.gz``; ``None`` if not RDF."""
    parts = name.lower().rsplit("/", 1)[-1].split(".")
    compression = None
    if len(parts) > 1 and parts[-1] in COMPRESSIONS:
        compression = parts.pop()
    if len(parts) < 2 or parts[-1] not in FORMATS:
        return None
    ext, media_type, dataset = FORMATS[parts[-1]]
    return RdfFormat(ext, media_type, dataset, compression)


def is_candidate(name: str) -> bool:
    """True for files the sync should consider: RDF files and zip archives."""
    return detect(name) is not None or name.lower().endswith(".zip")


def select(names: list[str], include: list[str], exclude: list[str]) -> list[str]:
    """Pick the files to load: those matching ``include`` globs (default: every RDF file
    or zip archive), minus those matching ``exclude`` globs."""
    selected = []
    for name in names:
        if include:
            if not any(fnmatch.fnmatch(name, pattern) for pattern in include):
                continue
        elif not is_candidate(name):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in exclude):
            continue
        selected.append(name)
    return selected


@dataclass
class LoadItem:
    """One RDF document to load: a downloaded file, or a member of a downloaded zip."""

    key: str  # Zenodo file key (or "archive.zip/member")
    path: Path
    format: RdfFormat
    graph: str | None = None  # target named graph for triple formats (None: default graph)
    member: str | None = None  # zip member name

    def open(self) -> IO[bytes]:
        """Open a decompressed binary stream of the RDF content."""
        if self.member is not None:
            archive = zipfile.ZipFile(self.path)
            raw: IO[bytes] = archive.open(self.member)
        else:
            raw = open(self.path, "rb")
        if self.format.compression:
            return COMPRESSIONS[self.format.compression](raw)  # type: ignore[return-value]
        return raw

    @property
    def native_path(self) -> Path | None:
        """Path that Oxigraph can read directly (plain or gzip file), if any."""
        if self.member is None and self.format.compression in (None, "gz"):
            return self.path
        return None


def load_items(key: str, path: Path, graph_for: "callable[[str], str | None]") -> list[LoadItem]:
    """Build load items for a downloaded file, expanding zip archives."""
    if key.lower().endswith(".zip"):
        items = []
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                fmt = detect(member)
                if fmt is None or member.endswith("/"):
                    continue
                member_key = f"{key}/{member}"
                items.append(LoadItem(member_key, path, fmt, None if fmt.dataset else graph_for(member_key), member))
        return items
    fmt = detect(key)
    if fmt is None:
        raise ValueError(f"Unrecognised RDF format for {key}")
    return [LoadItem(key, path, fmt, None if fmt.dataset else graph_for(key))]
