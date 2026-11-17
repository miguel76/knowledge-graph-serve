"""Minimal Zenodo REST API client: resolve a record to its latest version and download files."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import USER_AGENT

log = logging.getLogger(__name__)

_DOI_RE = re.compile(r"10\.5281/zenodo\.(\d+)", re.IGNORECASE)
_URL_RE = re.compile(r"zenodo\.org/(?:api/)?records?/(\d+)", re.IGNORECASE)


def parse_record_ref(ref: str) -> str:
    """Extract a Zenodo record id from an id, a DOI, a DOI URL or a record URL."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    for pattern in (_DOI_RE, _URL_RE):
        match = pattern.search(ref)
        if match:
            return match.group(1)
    raise ValueError(f"Not a Zenodo record id, DOI or URL: {ref!r}")


@dataclass
class ZenodoFile:
    key: str
    size: int
    checksum: str  # "md5:<hex>"
    url: str


@dataclass
class ZenodoRecord:
    id: str
    conceptrecid: str
    doi: str
    conceptdoi: str
    title: str
    version: str
    publication_date: str
    updated: str
    description: str
    license: str
    creators: list[str]
    html_url: str
    files: list[ZenodoFile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_api(cls, data: dict) -> "ZenodoRecord":
        meta = data.get("metadata", {})
        license_ = meta.get("license") or {}
        files = [
            ZenodoFile(
                key=f["key"],
                size=int(f.get("size", 0)),
                checksum=f.get("checksum", ""),
                url=f["links"].get("self") or f["links"].get("content"),
            )
            for f in data.get("files", [])
        ]
        return cls(
            id=str(data["id"]),
            conceptrecid=str(data.get("conceptrecid", data["id"])),
            doi=data.get("doi", ""),
            conceptdoi=data.get("conceptdoi", data.get("doi", "")),
            title=meta.get("title", ""),
            version=meta.get("version", ""),
            publication_date=meta.get("publication_date", ""),
            updated=data.get("updated", ""),
            description=meta.get("description", ""),
            license=license_.get("id", "") if isinstance(license_, dict) else str(license_),
            creators=[c.get("name", "") for c in meta.get("creators", [])],
            html_url=data.get("links", {}).get("self_html", ""),
            files=files,
        )


class Zenodo:
    def __init__(self, api: str = "https://zenodo.org/api", token: str = ""):
        self.api = api.rstrip("/")
        self.token = token

    def _request(self, url: str, accept: str = "application/json") -> urllib.request.Request:
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def _get_json(self, url: str, retries: int = 4) -> dict:
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(self._request(url), timeout=60) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as err:
                # 429: rate limited; 5xx: transient. Back off and retry.
                if (err.code == 429 or err.code >= 500) and attempt < retries:
                    wait = int(err.headers.get("Retry-After", "0") or 0) or 2 ** (attempt + 2)
                    log.warning("Zenodo returned %s for %s, retrying in %ss", err.code, url, wait)
                    time.sleep(wait)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 2))
                    continue
                raise
        raise RuntimeError("unreachable")

    def record(self, ref: str, follow_latest: bool = True) -> ZenodoRecord:
        """Fetch a record. A concept id/DOI resolves to its latest version; a version id
        is followed to the latest version unless ``follow_latest`` is false."""
        recid = parse_record_ref(ref)
        # Concept ids redirect to the latest version, urllib follows the redirect.
        data = self._get_json(f"{self.api}/records/{recid}")
        if follow_latest and str(data["id"]) == recid and str(data.get("conceptrecid")) != recid:
            data = self._get_json(f"{self.api}/records/{recid}/versions/latest")
        if not data.get("files") and data.get("links", {}).get("files"):
            listing = self._get_json(data["links"]["files"])
            data["files"] = listing.get("entries", [])
            for f in data["files"]:
                f.setdefault("links", {}).setdefault("self", f["links"].get("content"))
        return ZenodoRecord.from_api(data)

    def download(self, file: ZenodoFile, dest: Path, retries: int = 3) -> Path:
        """Download ``file`` to ``dest``, verifying its checksum. Skips the download if a
        file with the right checksum is already there."""
        algo, _, expected = file.checksum.partition(":")
        if dest.exists() and expected and _hash_file(dest, algo) == expected:
            log.info("Already downloaded: %s", dest.name)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        for attempt in range(retries + 1):
            try:
                hasher = hashlib.new(algo or "md5")
                started = time.monotonic()
                with urllib.request.urlopen(self._request(file.url, "*/*"), timeout=120) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 20):
                        hasher.update(chunk)
                        out.write(chunk)
                if expected and hasher.hexdigest() != expected:
                    raise IOError(f"checksum mismatch for {file.key}: {hasher.hexdigest()} != {expected}")
                tmp.rename(dest)
                log.info("Downloaded %s (%.1f MB in %.0fs)", file.key, file.size / 1e6, time.monotonic() - started)
                return dest
            except (urllib.error.URLError, IOError) as err:
                if attempt >= retries:
                    raise
                log.warning("Download of %s failed (%s), retrying", file.key, err)
                time.sleep(2 ** (attempt + 2))
        raise RuntimeError("unreachable")


def _hash_file(path: Path, algo: str) -> str:
    hasher = hashlib.new(algo or "md5")
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()
