"""Collect the hash IRIs (``doc#a``) used as subjects while a release is loaded.

The linked-data server needs them to answer a request for ``doc`` with the resources
``doc#a``, ``doc#b``... (browsers never send fragments). Finding them with a SPARQL query
is a full scan of the store; instead they are picked up from the files on their way to it:

* N-Triples and N-Quads (the usual format of large dumps) are scanned line by line while
  they stream to the store: the subject is the first term of a line, always a full IRI.
* Files in other syntaxes are parsed with rdflib after the upload, up to ``MAX_PARSE_SIZE``.
"""

from __future__ import annotations

import logging
import re
from typing import IO

from .rdffiles import LoadItem

log = logging.getLogger(__name__)

LINE_FORMATS = ("nt", "nq")
MAX_PARSE_SIZE = 100_000_000  # bytes of uncompressed input
SUBJECT_RE = re.compile(rb"^[ \t]*<([^>\s]*#[^>\s]*)>", re.MULTILINE)
RDFLIB_FORMATS = {"ttl": "turtle", "trig": "trig", "rdf": "xml", "jsonld": "json-ld", "n3": "n3"}


class _LineTap:
    """Wraps a binary stream; collects hash IRI subjects from the lines read through it."""

    def __init__(self, stream: IO[bytes], found: set[str]):
        self.stream = stream
        self.found = found
        self.pending = b""

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        buffer = self.pending + data
        end = buffer.rfind(b"\n") + 1 if data else len(buffer)  # scan complete lines only
        for match in SUBJECT_RE.finditer(buffer, 0, end):
            self.found.add(match.group(1).decode("utf-8", "replace"))
        self.pending = buffer[end:]
        return data

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class HashIndex:
    def __init__(self):
        self.subjects: set[str] = set()
        self.unscanned: list[LoadItem] = []

    def tap(self, item: LoadItem, stream: IO[bytes]) -> IO[bytes]:
        """Called by the loader for each file: scan line formats as they stream."""
        if item.format.ext in LINE_FORMATS:
            return _LineTap(stream, self.subjects)  # type: ignore[return-value]
        self.unscanned.append(item)
        return stream

    def finish(self) -> dict[str, list[str]]:
        """Parse the other files and return ``{document IRI: [hash IRIs]}``."""
        import rdflib

        for item in self.unscanned:
            with item.open() as stream:
                data = stream.read(MAX_PARSE_SIZE + 1)
            if len(data) > MAX_PARSE_SIZE:
                log.warning("%s is too large to look for hash IRIs (use N-Triples or N-Quads)", item.key)
                continue
            graph = rdflib.Dataset() if item.format.dataset else rdflib.Graph()
            try:
                graph.parse(data=data, format=RDFLIB_FORMATS[item.format.ext])
            except Exception as err:  # noqa: BLE001 - the store accepted it; only the index suffers
                log.warning("Could not parse %s to look for hash IRIs: %s", item.key, err)
                continue
            triples = graph.quads((None, None, None, None)) if item.format.dataset else graph
            for s, *_ in triples:
                if isinstance(s, rdflib.URIRef) and "#" in s:
                    self.subjects.add(str(s))
        index: dict[str, list[str]] = {}
        for iri in sorted(self.subjects):
            index.setdefault(iri.split("#", 1)[0], []).append(iri)
        return index
