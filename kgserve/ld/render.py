"""HTML rendering for resource pages, the landing page and the SPARQL editor."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Callable, Iterable

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD

from .describe import FORMAT_LABELS, FORMATS, Description

YASGUI_VERSION = "4.6.2"

TYPE = str(RDF.type)
FIRST_PROPERTIES = [
    TYPE,
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://purl.org/dc/terms/title",
    "http://www.w3.org/2004/02/skos/core#definition",
    "http://www.w3.org/2000/01/rdf-schema#comment",
    "http://purl.org/dc/terms/description",
]
DESCRIPTION_PROPERTIES = FIRST_PROPERTIES[4:]

WELL_KNOWN_PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "skosxl": "http://www.w3.org/2008/05/skos-xl#",
    "dcterms": "http://purl.org/dc/terms/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "schema": "http://schema.org/",
    "prov": "http://www.w3.org/ns/prov#",
    "dcat": "http://www.w3.org/ns/dcat#",
    "void": "http://rdfs.org/ns/void#",
    "sd": "http://www.w3.org/ns/sparql-service-description#",
    "sh": "http://www.w3.org/ns/shacl#",
    "geo": "http://www.opengis.net/ont/geosparql#",
    "time": "http://www.w3.org/2006/time#",
    "qb": "http://purl.org/linked-data/cube#",
    "dqv": "http://www.w3.org/ns/dqv#",
    "org": "http://www.w3.org/ns/org#",
}

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1f21;--muted:#62666d;--line:#e2e2df;--card:#fff;--accent:#2457a6;--chip:#eef2f8;--code:#f3f3f1}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e6e6e3;--muted:#9a9ea5;--line:#2c2e33;--card:#1b1c1f;--accent:#8ab4f8;--chip:#23293a;--code:#202226}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.top{border-bottom:1px solid var(--line);background:var(--card)}
header.top .in,main{max-width:1180px;margin:0 auto;padding:0 16px}
header.top .in{display:flex;gap:16px;align-items:center;justify-content:space-between;min-height:52px;flex-wrap:wrap}
header.top .kg{font-weight:600;color:var(--fg)}header.top nav{display:flex;gap:14px;font-size:14px}
main{padding-top:24px;padding-bottom:48px}
h1{font-size:26px;line-height:1.25;margin:0 0 6px;overflow-wrap:anywhere}
h2{font-size:17px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 8px;overflow-wrap:anywhere}
.iri{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.chip{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:13px}
.desc{max-width:80ch;margin:12px 0}
table.props{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
table.props td,table.props th{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top;text-align:left}
table.props tr:first-child td,table.props tr:first-child th{border-top:0}
table.props th{width:28%;font-weight:500;color:var(--muted);overflow-wrap:anywhere}
table.props ul{list-style:none;margin:0;padding:0}table.props li+li{margin-top:4px}
table.props td{overflow-wrap:anywhere}
table.nested{margin-top:4px;border-radius:6px}table.nested th{width:35%}
table.graphs th{width:auto}table.graphs td{text-align:right;white-space:nowrap;width:1%}
.tag{font-size:11px;color:var(--muted);background:var(--code);border-radius:4px;padding:0 5px;margin-left:6px;white-space:nowrap}
.g{font-size:11px;color:var(--muted);margin-left:6px;cursor:help}
.ext{font-size:12px;margin-left:4px;color:var(--muted)}
.formats{display:flex;flex-wrap:wrap;gap:8px;font-size:13px;margin:8px 0}
.formats a{border:1px solid var(--line);border-radius:6px;padding:2px 8px;background:var(--card)}
.muted{color:var(--muted)}.small{font-size:13px}
.note{background:var(--chip);border-radius:8px;padding:10px 14px;margin:14px 0}
.err{background:#fde8e8;color:#7a1d1d;border-radius:8px;padding:10px 14px}
@media (prefers-color-scheme:dark){.err{background:#3b1d1d;color:#f3c2c2}}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:0}
dl.meta dt{color:var(--muted)}dl.meta dd{margin:0;overflow-wrap:anywhere}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
footer{max-width:1180px;margin:0 auto;padding:16px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
@media (max-width:640px){table.props th,table.props td{display:block;width:auto}table.props td{border-top:0;padding-top:0}}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class _TextExtractor(HTMLParser):
    BLOCKS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_paragraphs(text: str) -> list[str]:
    """Reduce (untrusted) HTML, such as a Zenodo description, to plain-text paragraphs."""
    parser = _TextExtractor()
    parser.feed(text or "")
    raw = "".join(parser.parts)
    return [re.sub(r"\s+", " ", p).strip() for p in raw.split("\n") if p.strip()]


class Page:
    """Shared layout and link-building context for one request."""

    def __init__(self, *, title: str, kg_title: str, base_path: str, resource_path: str, base_iri: str,
                 sparql_url: str, footer: str = ""):
        self.title = title
        self.kg_title = kg_title
        self.base_path = base_path
        self.resource_path = resource_path
        self.base_iri = base_iri
        self.sparql_url = sparql_url
        self.footer = footer

    def layout(self, body: str, head: str = "") -> str:
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(self.title)}</title><style>{CSS}</style>{head}</head>
<body><header class="top"><div class="in"><a class="kg" href="{esc(self.base_path)}/">{esc(self.kg_title)}</a>
<nav><a href="{esc(self.base_path)}/">About</a><a href="{esc(self.base_path)}/sparql-ui/">SPARQL editor</a>
<a href="{esc(self.sparql_url)}">Endpoint</a></nav></div></header>
<main>{body}</main>{f'<footer>{self.footer}</footer>' if self.footer else ''}</body></html>"""

    def local_url(self, iri: str) -> str | None:
        """URL of the resource page on this server, for IRIs in the dereferenceable namespace."""
        if self.base_iri and iri.startswith(self.base_iri) and len(iri) > len(self.base_iri):
            return self.resource_path + iri[len(self.base_iri):]
        return None

    def browse_url(self, iri: str, fmt: str | None = None) -> str:
        query = {"iri": iri}
        if fmt:
            query["format"] = fmt
        return f"{self.base_path}/browse?{urllib.parse.urlencode(query)}"

    def sparql_ui_url(self, query: str) -> str:
        return f"{self.base_path}/sparql-ui/?{urllib.parse.urlencode({'query': query})}"


class ResourceRenderer:
    def __init__(self, page: Page, desc: Description, languages: list[str]):
        self.page = page
        self.desc = desc
        self.languages = [*languages, "en", ""]
        self.prefixes = dict(WELL_KNOWN_PREFIXES)
        self.graph_ids: dict[str, int] = {}
        # subject -> predicate -> object -> set of graphs
        self.index: dict[object, dict[str, dict[object, set[str]]]] = {}
        for s, p, o, g in desc.dataset.quads((None, None, None, None)):
            graph = str(g.identifier if hasattr(g, "identifier") else g)
            self.graph_ids.setdefault(graph, len(self.graph_ids) + 1)
            self.index.setdefault(s, {}).setdefault(str(p), {}).setdefault(o, set()).add(graph)

    # -- labels ---------------------------------------------------------------------------

    def best_label(self, iri: str) -> str | None:
        labels = self.desc.labels.get(iri)
        if not labels:
            return None
        for lang in self.languages:
            for label in labels:
                if (label.language or "") == lang:
                    return str(label)
        return str(labels[0])

    def short(self, iri: str) -> str:
        for prefix, ns in self.prefixes.items():
            if iri.startswith(ns) and len(iri) > len(ns):
                return f"{prefix}:{iri[len(ns):]}"
        local = re.split(r"[/#]", iri.rstrip("/#"))[-1]
        return urllib.parse.unquote(local) or iri

    def name(self, iri: str) -> str:
        return self.best_label(iri) or self.short(iri)

    # -- terms ----------------------------------------------------------------------------

    def iri_link(self, iri: str) -> str:
        local = self.page.local_url(iri)
        label = esc(self.name(iri))
        title = esc(iri)
        if local:
            return f'<a href="{esc(local)}" title="{title}">{label}</a>'
        if iri in self.desc.labels:  # described in this knowledge graph
            return (f'<a href="{esc(self.page.browse_url(iri))}" title="{title}">{label}</a>'
                    f'<a class="ext" href="{title}" title="Open {title}">↗</a>')
        return f'<a href="{title}" title="{title}">{label}</a>'

    def literal(self, lit: Literal) -> str:
        text = esc(lit).replace("\n", "<br>")
        if lit.language:
            return f'{text}<span class="tag">{esc(lit.language)}</span>'
        if lit.datatype and str(lit.datatype) != str(XSD.string):
            return f'{text}<span class="tag" title="{esc(lit.datatype)}">{esc(self.short(str(lit.datatype)))}</span>'
        return text

    def graphs_badge(self, graphs: set[str]) -> str:
        if len(self.graph_ids) < 2:
            return ""
        ids = sorted(self.graph_ids[g] for g in graphs)
        title = esc("In graph" + ("s " if len(ids) > 1 else " ") + ", ".join(sorted(graphs)))
        return f'<span class="g" title="{title}">[{",".join(f"g{i}" for i in ids)}]</span>'

    def value(self, obj, graphs: set[str], depth: int, seen: set) -> str:
        if isinstance(obj, URIRef):
            out = self.iri_link(str(obj))
        elif isinstance(obj, BNode):
            out = self.table(obj, depth + 1, seen, nested=True) if depth < 5 and obj not in seen else '<span class="muted">[blank node]</span>'
        else:
            out = self.literal(obj)
        return out + self.graphs_badge(graphs)

    def ordered(self, predicates: Iterable[str]) -> list[str]:
        def key(p: str):
            return (FIRST_PROPERTIES.index(p) if p in FIRST_PROPERTIES else len(FIRST_PROPERTIES), self.name(p).lower())
        return sorted(predicates, key=key)

    def table(self, subject, depth: int = 0, seen: set | None = None, nested: bool = False) -> str:
        seen = (seen or set()) | {subject}
        rows = []
        for p in self.ordered(self.index.get(subject, {})):
            values = self.index[subject][p]
            items = "".join(f"<li>{self.value(o, g, depth, seen)}</li>" for o, g in sorted(values.items(), key=lambda kv: str(kv[0])))
            rows.append(f'<tr><th>{self.iri_link(p)}</th><td><ul>{items}</ul></td></tr>')
        if not rows:
            return '<span class="muted">[blank node]</span>'
        return f'<table class="props{" nested" if nested else ""}">{"".join(rows)}</table>'

    # -- page -----------------------------------------------------------------------------

    def subject_header(self, iri: str, level: str) -> str:
        props = self.index.get(URIRef(iri), {})
        types = "".join(f'<span class="chip">{self.iri_link(str(t))}</span>' for t in sorted(props.get(TYPE, {}), key=str))
        description = ""
        for p in DESCRIPTION_PROPERTIES:
            values = [o for o in props.get(p, {}) if isinstance(o, Literal)]
            if values:
                best = next((v for lang in self.languages for v in values if (v.language or "") == lang), values[0])
                description = f'<p class="desc">{esc(best)}</p>'
                break
        anchor = f' id="{esc(iri.split("#", 1)[1])}"' if "#" in iri else ""
        return (f'<{level}{anchor}>{esc(self.name(iri))}</{level}><div class="iri">{esc(iri)}</div>'
                f'{f"<div class=chips>{types}</div>" if types else ""}{description}')

    def render(self, format_url: Callable[[str], str]) -> str:
        main_iri = self.desc.iris[0]
        parts = [self.subject_header(main_iri, "h1")]
        formats = "".join(f'<a href="{esc(format_url(f))}" rel="alternate" type="{FORMATS[f][0]}">{FORMAT_LABELS[f]}</a>' for f in FORMATS)
        parts.append(f'<div class="formats"><span class="muted">Download:</span>{formats}</div>')
        if self.desc.truncated:
            parts.append('<div class="note">This description is truncated. Use the SPARQL endpoint to get all the data.</div>')
        if URIRef(main_iri) in self.index:
            parts.append(self.table(URIRef(main_iri)))
        hash_subjects = [iri for iri in self.desc.iris[1:] if URIRef(iri) in self.index]
        if hash_subjects:
            parts.append(f"<h2>Resources in this document ({len(hash_subjects)})</h2>")
            for iri in hash_subjects:
                parts.append(self.subject_header(iri, "h3") + self.table(URIRef(iri)))
        if self.desc.inverse:
            by_property: dict[str, list[str]] = {}
            for s, p in self.desc.inverse:
                by_property.setdefault(p, []).append(s)
            rows = "".join(
                f'<tr><th>{self.iri_link(p)} <span class="muted small">of</span></th><td><ul>'
                + "".join(f"<li>{self.iri_link(s)}</li>" for s in sorted(subjects, key=lambda s: self.name(s).lower()))
                + "</ul></td></tr>"
                for p, subjects in sorted(by_property.items(), key=lambda kv: self.name(kv[0]).lower()))
            more = ""
            if self.desc.inverse_truncated:
                query = f"SELECT ?s ?p WHERE {{\n  ?s ?p <{main_iri}>\n}}\nLIMIT 1000"
                more = f'<p class="small"><a href="{esc(self.page.sparql_ui_url(query))}">See all references in the SPARQL editor</a></p>'
            parts.append(f'<h2>Referenced by</h2><table class="props">{rows}</table>{more}')
        if self.graph_ids:
            items = "".join(
                f'<li><span class="g">g{i}</span> {self.iri_link(g)} <span class="iri">{esc(g)}</span></li>'
                for g, i in sorted(self.graph_ids.items(), key=lambda kv: kv[1]))
            parts.append(f'<h2>Named graphs</h2><ul class="small">{items}</ul>')
        return self.page.layout("".join(parts))


def not_found(page: Page, iri: str, message: str = "") -> str:
    query = f"SELECT ?p ?o WHERE {{\n  <{iri}> ?p ?o\n}}"
    return page.layout(
        f"<h1>Not found</h1><div class=iri>{esc(iri)}</div>"
        f"<p>{esc(message or 'There is no data about this resource in the knowledge graph.')}</p>"
        f'<p><a href="{esc(page.sparql_ui_url(query))}">Query it in the SPARQL editor</a></p>')


def error_page(page: Page, message: str) -> str:
    return page.layout(f'<h1>Service unavailable</h1><p class="err">{esc(message)}</p>')


def sparql_editor(page: Page, example_query: str) -> str:
    cdn = f"https://cdn.jsdelivr.net/npm/@zazuko/yasgui@{YASGUI_VERSION}/build"
    head = f'<link rel="stylesheet" href="{cdn}/yasgui.min.css"><script src="{cdn}/yasgui.min.js"></script>'
    config = json.dumps({"endpoint": page.sparql_url, "query": example_query})
    body = f"""<div id="yasgui"></div><script>
const cfg = {config};
const yasgui = new Yasgui(document.getElementById("yasgui"), {{
  requestConfig: {{ endpoint: cfg.endpoint, method: "POST" }},
  copyEndpointOnNewTab: true, persistenceId: "kgserve:" + cfg.endpoint,
}});
const params = new URLSearchParams(window.location.search);
const tab = yasgui.getTab();
if (params.get("query")) {{ tab.setQuery(params.get("query")); }}
else if (!tab.getQuery() || tab.getQuery().trim() === "") {{ tab.setQuery(cfg.query); }}
</script>"""
    return page.layout(body, head).replace("<main>", '<main style="max-width:none">', 1)


def landing(page: Page, state: dict, base_iri: str) -> str:
    record = state.get("record") or {}
    store = state.get("store") or {}
    parts = [f"<h1>{esc(record.get('title') or page.kg_title)}</h1>"]
    status = state.get("status")
    if not store:
        parts.append('<div class="note">The knowledge graph is being loaded. Please come back in a few minutes.</div>'
                     if status in (None, "syncing", "starting") else "")
    if state.get("last_error"):
        parts.append(f'<p class="err small">Last synchronisation error: {esc(state["last_error"])}</p>')
    for paragraph in html_to_paragraphs(record.get("description", ""))[:8]:
        parts.append(f'<p class="desc">{esc(paragraph)}</p>')

    access = [("SPARQL endpoint", f'<code>{esc(page.sparql_url)}</code>'),
              ("SPARQL editor", f'<a href="{esc(page.base_path)}/sparql-ui/">{esc(page.base_path)}/sparql-ui/</a>')]
    if base_iri:
        access.append(("Linked data", f'IRIs starting with <code>{esc(base_iri)}</code> are served at '
                                      f'<a href="{esc(page.resource_path)}">{esc(page.resource_path)}</a> '
                                      "(HTML, Turtle, N-Triples, JSON-LD, RDF/XML, N-Quads, TriG)"))
    examples = state.get("example_resources") or []
    if examples:
        links = ", ".join(f'<a href="{esc(page.local_url(e) or page.browse_url(e))}">{esc(e)}</a>' for e in examples[:5])
        access.append(("Example resources", links))
    parts.append("<h2>Access</h2><dl class=meta>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in access) + "</dl>")

    if record:
        doi = record.get("doi")
        concept = record.get("conceptdoi")
        meta = [
            ("Version", esc(record.get("version") or "-") + (f' <span class="muted">({esc(record.get("publication_date"))})</span>' if record.get("publication_date") else "")),
            ("DOI", f'<a href="https://doi.org/{esc(doi)}">{esc(doi)}</a>' if doi else "-"),
            ("All versions", f'<a href="https://doi.org/{esc(concept)}">{esc(concept)}</a>' if concept else "-"),
            ("License", esc(record.get("license") or "-")),
            ("Creators", esc("; ".join(record.get("creators") or [])) or "-"),
            ("Loaded files", ", ".join(f"<code>{esc(f)}</code>" for f in state.get("loaded_files") or [])),
        ]
        parts.append("<h2>Dataset</h2><dl class=meta>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta) + "</dl>")

    sync = [("Statements", f"{store['triples']:,}" if store.get("triples") is not None else "-"),
            ("Store", esc(state.get("backend") or "-")),
            ("Last loaded", esc(state.get("last_sync") or "-")),
            ("Last checked", esc(state.get("last_check") or "-")),
            ("Next check", esc(state.get("next_check") or "-"))]
    parts.append("<h2>Synchronisation</h2><dl class=meta>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in sync) + "</dl>")

    graphs = state.get("graphs") or []
    if graphs:
        rows = "".join(f'<tr><th><a href="{esc(page.local_url(g["iri"]) or page.browse_url(g["iri"]))}">{esc(g["iri"])}</a></th>'
                       f'<td>{g["triples"]:,}</td></tr>' for g in graphs)
        parts.append(f'<h2>Named graphs ({len(graphs)})</h2><table class="props graphs">{rows}</table>')
    return page.layout("".join(parts))
