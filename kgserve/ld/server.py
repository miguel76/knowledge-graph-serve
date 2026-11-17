"""Linked-data HTTP server (standard library ``http.server``, meant to run behind nginx)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__
from ..config import LDConfig
from .describe import FORMATS, Store, negotiate
from .render import WELL_KNOWN_PREFIXES, Page, ResourceRenderer, error_page, esc, landing, not_found, sparql_editor

log = logging.getLogger(__name__)

EXTENSION_RE = re.compile(r"^(.+)\.(ttl|nt|jsonld|rdf|nq|trig|n3|html)$")
MAX_HASH_SUBJECTS = 300
EXAMPLE_QUERY = "SELECT ?s ?p ?o WHERE {\n  ?s ?p ?o\n}\nLIMIT 10"


class JSONFile:
    """A JSON file re-read when it changes on disk."""

    def __init__(self, path: Path, default):
        self.path = path
        self.default = default
        self.mtime = None
        self.value = default

    def get(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return self.default
        if mtime != self.mtime:
            try:
                self.value = json.loads(self.path.read_text())
                self.mtime = mtime
            except (OSError, json.JSONDecodeError):
                pass  # being rewritten: keep the previous value
        return self.value


class App:
    def __init__(self, config: LDConfig):
        self.config = config
        self.store = Store(config.sparql_endpoint)
        self.state = JSONFile(config.data_dir / "state.json", {})
        self.hash_index = JSONFile(config.data_dir / "hash-index.json", {})

    def base_iri(self) -> str:
        if self.config.base_iri:
            return self.config.base_iri
        spaces = self.state.get().get("uri_spaces") or []
        return spaces[0] if spaces else ""

    def page(self, title: str | None = None) -> Page:
        state = self.state.get()
        record = state.get("record") or {}
        kg_title = self.config.title or record.get("title") or "Knowledge graph"
        footer_bits = [esc(kg_title)]
        if record.get("version"):
            footer_bits.append(f"version {esc(record['version'])}")
        if record.get("doi"):
            footer_bits.append(f'<a href="https://doi.org/{esc(record["doi"])}">{esc(record["doi"])}</a>')
        if record.get("license"):
            footer_bits.append(esc(record["license"]))
        footer_bits.append(f'served by <a href="https://github.com/miguel76/knowledge-graph-serve">kgserve</a> {__version__}')
        return Page(title=title or kg_title, kg_title=kg_title, base_path=self.config.base_path,
                    resource_path=self.config.resource_path, base_iri=self.base_iri(),
                    sparql_url=self.config.public_sparql_url, footer=" · ".join(footer_bits))

    def store_tag(self) -> str:
        return str((self.state.get().get("store") or {}).get("tag", ""))


class Handler(BaseHTTPRequestHandler):
    server_version = f"kgserve/{__version__}"
    protocol_version = "HTTP/1.1"
    app: App  # set by serve()

    # -- plumbing -------------------------------------------------------------------------

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def send(self, status: int, body: bytes | str, content_type: str = "text/html; charset=utf-8",
             headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, location: str, status: int = HTTPStatus.MOVED_PERMANENTLY) -> None:
        self.send(status, "", "text/plain", {"Location": location})

    def languages(self) -> list[str]:
        langs = []
        for part in (self.headers.get("Accept-Language") or "").split(","):
            tag = part.split(";")[0].strip().lower()
            if tag and tag != "*":
                langs += [tag] + ([tag.split("-")[0]] if "-" in tag else [])
        return langs

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route()
        except (ConnectionError, TimeoutError) as err:
            log.warning("SPARQL endpoint unavailable: %s", err)
            self.send(503, error_page(self.app.page(), "The SPARQL endpoint is not available right now "
                                                        "(it may be loading a new release)."), headers={"Retry-After": "60"})
        except Exception as err:  # noqa: BLE001
            log.exception("Error handling %s", self.path)
            self.send(500, error_page(self.app.page(), f"Internal error: {err}"))

    # -- routes ---------------------------------------------------------------------------

    def route(self) -> None:
        app = self.app
        cfg = app.config
        path, _, query_string = self.path.partition("?")
        query = urllib.parse.parse_qs(query_string)
        base = cfg.base_path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == base or path == "":
            return self.redirect(base + "/")
        if path == base + "/":
            return self.send(200, landing(app.page(), app.state.get(), app.base_iri()))
        if path == base + "/sparql-ui":
            return self.redirect(base + "/sparql-ui/" + (f"?{query_string}" if query_string else ""))
        if path == base + "/sparql-ui/":
            return self.send(200, sparql_editor(app.page("SPARQL editor"), EXAMPLE_QUERY))
        if path == base + "/status.json":
            return self.send(200, json.dumps(app.state.get(), indent=2), "application/json")
        if path == base + "/browse":
            iri = (query.get("iri") or [""])[0]
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:[^\s<>\"{}|\\^`]*$", iri):
                return self.send(400, error_page(app.page(), "Expected an absolute IRI in the 'iri' parameter."))
            fmt = (query.get("format") or [None])[0]
            return self.resource(iri, fmt, lambda f: app.page().browse_url(iri, f))
        if path.startswith(cfg.resource_path):
            rest = path[len(cfg.resource_path):]
            base_iri = app.base_iri()
            if not base_iri:
                return self.send(404, not_found(app.page(), path, "The IRI namespace of this knowledge graph "
                                                                   "is not configured (KG_BASE_IRI)."))
            fmt = (query.get("format") or [None])[0]
            candidates = [(base_iri + rest, fmt)]
            match = EXTENSION_RE.match(rest)
            if match and not fmt:
                # ".../x.ttl": the resource ".../x" in Turtle, unless ".../x.ttl" itself exists.
                candidates.append((base_iri + match.group(1), match.group(2)))
            public = cfg.resource_path
            for i, (iri, candidate_fmt) in enumerate(candidates):
                last = i == len(candidates) - 1
                done = self.resource(iri, candidate_fmt, lambda f, iri=iri: f"{public}{iri[len(base_iri):]}?format={f}",
                                     respond_not_found=last)
                if done:
                    return
            return
        self.send(404, not_found(app.page(), path, "No such page."))

    def resource(self, iri: str, fmt: str | None, format_url, respond_not_found: bool = True) -> bool:
        """Describe ``iri``; returns False (without responding) if not found and not ``respond_not_found``."""
        app = self.app
        if fmt is None:
            fmt = negotiate(self.headers.get("Accept"))
            if fmt is None:
                self.send(406, "Not Acceptable. Available: text/html, " + ", ".join(m for m, _, _ in FORMATS.values()), "text/plain")
                return True
        elif fmt != "html" and fmt not in FORMATS:
            self.send(400, f"Unknown format {fmt!r}. Available: html, {', '.join(FORMATS)}", "text/plain")
            return True

        etag = 'W/"' + hashlib.sha1(f"{app.store_tag()}|{__version__}|{iri}|{fmt}|{self.headers.get('Accept-Language', '')}".encode()).hexdigest()[:20] + '"'
        alternates = ", ".join(f'<{format_url(f)}>; rel="alternate"; type="{FORMATS[f][0]}"' for f in FORMATS)
        headers = {"Vary": "Accept, Accept-Language", "ETag": etag, "Cache-Control": "public, max-age=300", "Link": alternates}
        if app.store_tag() and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        iris = [iri] + [h for h in app.hash_index.get().get(iri, []) if h != iri][:MAX_HASH_SUBJECTS]
        prefixes = dict(WELL_KNOWN_PREFIXES)
        desc = app.store.describe(iris, app.config.describe_limit, prefixes)
        if len(desc) == 0:
            if "#" in iri:
                # Browsers drop fragments, but a client may still ask for a hash IRI explicitly.
                doc = iri.split("#", 1)[0]
                desc = app.store.describe([doc] + app.hash_index.get().get(doc, [])[:MAX_HASH_SUBJECTS], app.config.describe_limit, prefixes)
        if len(desc) == 0:
            if not respond_not_found:
                return False
            if fmt == "html":
                self.send(404, not_found(app.page("Not found"), iri))
            else:
                self.send(404, f"No data about <{iri}>\n", "text/plain")
            return True

        if fmt == "html":
            app.store.add_inverse(desc, app.config.inverse_limit)
            app.store.add_labels(desc)
            renderer = ResourceRenderer(app.page(), desc, self.languages())
            renderer.page.title = f"{renderer.name(iri)} · {renderer.page.kg_title}"
            self.send(200, renderer.render(format_url), headers=headers)
        else:
            media_type = FORMATS[fmt][0]
            charset = "" if fmt in ("jsonld",) else "; charset=utf-8"
            self.send(200, desc.serialize(fmt), media_type + charset, headers=headers)
        return True


def serve(config: LDConfig) -> None:
    Handler.app = App(config)
    server = ThreadingHTTPServer((config.bind, config.port), Handler)
    server.daemon_threads = True
    log.info("Linked data server on %s:%s (resources at %s, SPARQL %s)", config.bind, config.port,
             config.resource_path, config.sparql_endpoint)
    server.serve_forever()
