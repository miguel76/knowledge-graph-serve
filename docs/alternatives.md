# Triple stores and linked-data front ends: evaluation

This evaluation chose the components of knowledge-graph-serve. The baseline is
[climate-change-kg-docker](https://github.com/hacid-project/climate-change-kg-docker):
GraphDB + LodView + YASGUI behind nginx. In that deployment LodView does not serve
RDF serialisations.

## Requirements

What the tool needs from its components, given that it serves *any* knowledge graph
published as a dump:

1. **Reload a whole release.** Data change only when a new version is published. A
   store that bulk-loads fast offline, then switches atomically, fits better than one
   tuned for incremental updates.
2. **Public read-only endpoint**, with query timeouts.
3. **Named graphs.** Dumps are often N-Quads (the CCKG has 17 graphs), so a union
   default graph is expected.
4. **Linked data done right.** Content negotiation for all common RDF serialisations,
   canonical IRIs in RDF responses (IRIs are minted under w3id.org/purl.org and
   redirected to the server), hash IRIs, and fast point lookups.
5. **Few moving parts**, open-source licences, no license files.

## Test setup

* Data: Climate Change Knowledge Graph 1.0.0
  ([10.5281/zenodo.24305089](https://doi.org/10.5281/zenodo.24305089)):
  `kg.nq.gz` (35.6 MB, 5,684,642 quads in 16 named graphs) plus `void.ttl` (2,269 triples).
* Machine: 4 vCPUs, 15 GB RAM, containers with default settings.
* Queries ([bench/bench.py](../bench/bench.py)): `count` (all quads), `types` (top 20
  classes by instances), `describe` (the resource description query used by the
  linked-data server), `inverse` (50 incoming links), `label-search` (substring search on
  `rdfs:label`), `per-graph` (distinct predicates per graph). Each was run 4 times. The
  tables report the first (cold) run and the median of the other 3. QLever caches
  results, so its medians are cache hits; compare its cold runs.

## Triple stores

### Measurements

| | Oxigraph 0.5.13 | QLever (image of 2026-11-17) | Fuseki 6.2.0 (TDB1 + Lucene) | GraphDB 11.5.2 |
| --- | --- | --- | --- | --- |
| Licence | Apache-2.0 / MIT | Apache-2.0 | Apache-2.0 | proprietary, license file required even for the free edition |
| Load (5.7M quads) | 30 s bulk load + 22 s optimize, offline | 27 s index build, offline | 113 s, HTTP POST into the live store | not measured (no license available) |
| Disk | 1.8 GB (0.9 GB before optimize) | 82 MB | 2.2 GB (with text index) | – |
| Memory while serving | 0.7 GB | – | 1.6 GB (JVM) | – |

Query times in ms, first run / median of the next 3 (Fuseki: median only):

| Query | Oxigraph | QLever | Fuseki |
| --- | --- | --- | --- |
| count | 2327 / 2071 | 32 / 2 | 1511 |
| types | 302 / 286 | 12 / 2 | 2678 |
| describe (linked data) | 5.0 / 1.9 | 569 / 171 | 11.6 |
| inverse (linked data) | 3.5 / 1.6 | 6.1 / 3.0 | 9.6 |
| label-search | 3709 / 1354 | 350 / 2 | 3997 |
| per-graph | 2663 / 2211 | 193 / 2 | 3518 |

### Oxigraph: the default

* Fits the release-reload model: `oxigraph load` bulk-loads (gzip-compressed) dumps
  offline into a new directory. `oxigraph serve-read-only` then serves it with
  `--union-default-graph` and `--timeout-s`. The sync swaps releases with about 0.2 s of
  unavailability: in a test, 448 queries during a reload all succeeded.
* Fastest on the point lookups that linked data needs (2–5 ms).
* A single static binary, so it ships inside the sync image (about 230 MB in total): no
  extra container, JVM or license.
* Reads every common RDF syntax (N-Triples, N-Quads, Turtle, TriG, RDF/XML, JSON-LD, N3).
* Limits: whole-graph aggregations take seconds at this size, and there is no full-text
  index. The union default graph does not remove a triple found in several graphs
  (`SELECT` returns it once per graph; the linked-data server merges them).
* The planner can be sensitive to query shape. A label lookup joining two `VALUES`
  blocks took 4.8 s, against 8 ms when rewritten with `UNION`.

### QLever: for large graphs and analytics

* 10–75× faster than Oxigraph on aggregations and scans on a cold run, and far more on
  repeated queries thanks to its result cache. It also has the smallest index (82 MB). Built for graphs the size of Wikidata (billions of triples).
* Its offline index build also fits the release-reload model. A QLever backend would
  follow the same blue/green pattern as Oxigraph.
* Slower on the linked-data description query (UNION over blank-node paths): 170–570 ms,
  against 2–5 ms on Oxigraph. Still acceptable.
* Reads only N-Triples, Turtle and N-Quads (other formats would need converting first:
  `oxigraph convert` is in the image). The image is Ubuntu with Python tooling, so it is
  heavier to embed.
* **Recommendation:** add it as a second managed backend when a knowledge graph grows
  beyond a few hundred million triples, or when analytic queries matter more than
  resource lookups.

### Apache Jena Fuseki

* Mature and standards-compliant, with optional Lucene full-text search (the
  `secoresearch/fuseki` image configures it on labels).
* Loading through HTTP took 113 s and is not atomic: the sync drops everything, then
  posts the new data. An offline `tdb2.tdbloader` plus a restart would avoid this, at
  the cost of managing the Fuseki process the way Oxigraph is managed.
* Slower than Oxigraph on every query here, and uses twice the memory (JVM).
* Supported through `compose.fuseki.yaml` (generic Graph Store Protocol backend).

### GraphDB (current deployment)

* A strong product: Workbench, inference rulesets, full-text and Elasticsearch
  connectors, and SPARQL extensions. Choose it when reasoning or those connectors are
  needed.
* GraphDB 11 refuses transactions without a license, also for the free edition
  ("No license was set"). Free licenses need registration with Ontotext and renewal.
  That adds a manual step to every deployment of a "serve any KG" tool.
* Supported through `compose.graphdb.yaml`: the repository is created from a Turtle
  config and each release replaces the data in one RDF4J transaction (atomic). The
  protocol was verified end to end against an RDF4J server. The large single
  transaction needs enough heap: a 4 GB RDF4J native store ran out of memory committing
  5.7M quads. GraphDB itself was not measured.

### Not tested

* **Virtuoso Open Source** (GPL): scales well and has built-in linked-data pages, but
  configuring URL rewriting and content negotiation is complex. It is also known for
  departures from the SPARQL standard.
* **Blazegraph**: unmaintained; avoid for new deployments.
* **Stardog, Amazon Neptune, AllegroGraph**: commercial or cloud-only.

## Linked-data front ends

| | LodView (current) | Trifid 6.1 (tested) | kgserve ld (this repository) |
| --- | --- | --- | --- |
| RDF serialisations | do not work in the current deployment | Turtle, N-Triples, JSON-LD, RDF/XML, TriG (N-Quads falls back to HTML) | Turtle, N-Triples, JSON-LD, RDF/XML, N3, N-Quads, TriG |
| Canonical IRIs in RDF when the IRIs are redirected (w3id.org) | yes (`IRInamespace` / `publicUrlPrefix`) | **no**: IRIs are rewritten to the server host, in RDF too | yes; HTML links to local pages |
| Hash IRIs (`doc#a`) | no | no | yes (index of hash IRIs built at each load) |
| Same triple in several graphs | – | duplicated (with Oxigraph's union default graph) | merged; N-Quads/TriG keep the graphs |
| Extras | – | YASGUI, graph explorer, SPEX | YASGUI page, landing page with Zenodo metadata, `browse?iri=`, ETag/304 |
| Deployment | Java webapp built from a fork | Node, 690 MB image | in the sync image (Python + rdflib), about 230 MB in total |

* **LodView** has the right IRI model but is a Java webapp that the current setup builds
  from a fork, and its RDF output does not work there.
* **Trifid** (Zazuko) is maintained and its content negotiation works. But its rewriting
  model breaks dereferencing when IRIs are minted under a redirector such as w3id.org: a
  client asking for `https://w3id.org/…/X` in Turtle gets triples about
  `https://keng…/X`. It can be configured around this (no rewriting, with nginx
  forwarding the canonical host and path). Hash IRIs stay unsupported. It is a good choice
  where the server's own host *is* the IRI namespace and the graph explorer is wanted.
* **Pubby** has been unmaintained for over a decade. **LinkedDataHub** is a full
  application platform, more than this use case needs.
* **kgserve ld** (about 800 lines of Python) is therefore part of this repository. It needs
  only a SPARQL endpoint; its queries were tested on Oxigraph, Fuseki and QLever. It renders
  a page in 10–130 ms on Oxigraph, including incoming links and labels.

## Recommendation

* Default stack: **Oxigraph** (managed by the sync, blue/green reloads) + **kgserve ld** +
  nginx. It needs no license, ships as one image plus nginx, and loads the CCKG in under a
  minute.
* For very large graphs or heavy analytics: add a **QLever** managed backend (follow-up).
* Keep **GraphDB** (supported) where its inference or connectors are needed and a
  license is available.
