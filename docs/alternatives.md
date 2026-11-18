# Triple stores and linked-data front ends: evaluation

This evaluation chose the components of knowledge-graph-serve. The baseline is
[climate-change-kg-docker](https://github.com/hacid-project/climate-change-kg-docker):
GraphDB + LodView + YASGUI behind nginx. In that deployment LodView does not serve
RDF serialisations.

## Requirements

What the tool needs from its components, given that it serves *any* knowledge graph
published as a dump:

1. **Modular.** The triple store runs in its own container, from its vendor image, and is
   loaded through standard protocols; existing components are preferred to new code.
2. **Reload a whole release.** Data change only when a new version is published, so fast
   bulk loading matters more than incremental updates. A short window of partial data
   during a reload is acceptable.
3. **Public read-only endpoint**, with query timeouts.
4. **Named graphs.** Dumps are often N-Quads (the CCKG has 17 graphs), so a union
   default graph is expected.
5. **Linked data done right.** Content negotiation for all common RDF serialisations,
   canonical IRIs in RDF responses (IRIs are minted under w3id.org/purl.org and
   redirected to the server), hash IRIs, and fast point lookups.
6. **Mountable anywhere.** The service is published by a front web server under a path of
   its choice (e.g. `/climate-change-kg/`), which also handles HTTPS. So every URL the
   service generates must be relative.
7. **Few moving parts**, open-source licences, no license files.

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
| Load (5.7M quads) | 33 s over HTTP (bulk mode); 30 s offline | 27 s index build, offline | 113 s over HTTP | not measured (no license available) |
| Disk | 0.9 GB (1.8 GB after `oxigraph optimize`) | 82 MB | 2.2 GB (with text index) | – |
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

* Runs from its vendor image (`ghcr.io/oxigraph/oxigraph`), with `--union-default-graph`
  and `--timeout-s`. The sync loads it over HTTP: `DROP ALL` is instant, and posting the
  gzip-decompressed N-Quads to `/store?no_transaction` (bulk loading) took 33 s for the
  5.7M quads, as fast as the offline `oxigraph load`. Queries during that window see
  partial data.
* Fastest on the point lookups that linked data needs (2–5 ms).
* Reads every common RDF syntax (N-Triples, N-Quads, Turtle, TriG, RDF/XML, JSON-LD, N3).
* Limits: whole-graph aggregations take seconds at this size, and there is no full-text
  index. A full scan of the store, such as looking for every subject IRI containing `#`,
  took 48 s, close to a 60 s public timeout; the hash-IRI index is therefore collected
  from the files during the load, not queried. The union default graph does not remove a
  triple found in several graphs (`SELECT` returns it once per graph; the linked-data
  server merges them).
* The planner can be sensitive to query shape. A label lookup joining two `VALUES`
  blocks took 4.8 s, against 8 ms when rewritten with `UNION`.

### QLever: for large graphs and analytics (not integrated)

* 10–75× faster than Oxigraph on aggregations and scans on a cold run, and far more on
  repeated queries thanks to its result cache. It also has the smallest index (82 MB).
  Built for graphs the size of Wikidata (billions of triples).
* Slower on the linked-data description query (UNION over blank-node paths): 170–570 ms,
  against 2–5 ms on Oxigraph; linked-data pages take 0.15–0.6 s instead of 10–130 ms.
* Its union default graph is a set: a triple stored in two named graphs counts once
  (e.g. 853,031 `rdf:type` triples on the CCKG, against 859,742 on Oxigraph).
* **Why it is not integrated:** QLever's data come from an index built offline by
  `qlever-index`; it has no fast way to load a dump over HTTP (its SPARQL Update support
  is meant for small incremental changes). Integrating it means either running the index
  build inside the QLever container and restarting its server, or a shared volume plus
  process management in the sync, with a proxy to avoid downtime. That was implemented
  once (FIFO-fed index builds, zero-downtime swap through a switching proxy) and removed
  in favour of a modular setup with vendor containers loaded through standard protocols.
* **When to use it:** for a knowledge graph beyond a few hundred million triples, or when
  analytic queries matter more than resource lookups. Then run QLever with its own tooling
  (`qlever index`, `qlever start`, driven by a Qleverfile), and point `SPARQL_UPSTREAM` and
  `KG_STORE_QUERY` at it, without the sync service.

### Apache Jena Fuseki

* Mature and standards-compliant, with optional Lucene full-text search (the
  `secoresearch/fuseki` image configures it on labels).
* Loading through HTTP took 113 s (the same protocol as Oxigraph: `DROP ALL`, then POST),
  so its partial-data window is longer.
* Slower than Oxigraph on every query here, and uses twice the memory (JVM).
* Supported through `compose.fuseki.yaml`, with the same sync code as Oxigraph.

### GraphDB (current deployment)

* A strong product: Workbench, inference rulesets, full-text and Elasticsearch
  connectors, and SPARQL extensions. Choose it when reasoning or those connectors are
  needed.
* GraphDB 11 refuses transactions without a license, also for the free edition
  ("No license was set"). Free licenses need registration with Ontotext and renewal.
  That adds a manual step to every deployment of a "serve any KG" tool.
* Supported through `compose.graphdb.yaml`: the repository is created from a Turtle
  config and each release replaces the data in one RDF4J transaction, so there is no
  partial-data window. The protocol was verified end to end against an RDF4J server. The large single
  transaction needs enough heap: a 4 GB RDF4J native store ran out of memory committing
  5.7M quads. GraphDB itself was not measured.

### Not tested

* **Virtuoso Open Source** (GPL): scales well and has built-in linked-data pages, but
  configuring URL rewriting and content negotiation is complex. It is also known for
  departures from the SPARQL standard.
* **Blazegraph**: unmaintained; avoid for new deployments.
* **Stardog, Amazon Neptune, AllegroGraph**: commercial or cloud-only.

## Linked-data front ends

Existing components were evaluated first; the linked-data server in this repository exists
only because none of them meets requirements 5 and 6.

| | LodView (current) | Trifid 6.1 (tested) | kgserve ld (this repository) |
| --- | --- | --- | --- |
| RDF serialisations | **broken**: every RDF format returns HTTP 500 in the current deployment | Turtle, N-Triples, JSON-LD, RDF/XML, TriG (N-Quads falls back to HTML) | Turtle, N-Triples, JSON-LD, RDF/XML, N3, N-Quads, TriG |
| Canonical IRIs in RDF when the IRIs are redirected (w3id.org) | yes (`IRInamespace` / `publicUrlPrefix`) | **no**: IRIs are rewritten to the server host, in RDF too | yes |
| Mountable under any path | **no**: absolute `publicUrlPrefix` and `staticResourceURL` | **no**: root-absolute assets (`/static/core/style.css`), links built from the request host | yes: all URLs relative |
| Hash IRIs (`doc#a`) | no | no | yes |
| Same triple in several graphs | – | duplicated (with Oxigraph's union default graph) | merged; N-Quads/TriG keep the graphs |
| Extras | – | YASGUI, graph explorer, SPEX | YASGUI page, landing page with Zenodo metadata, `browse?iri=`, ETag/304 |
| Deployment | Java webapp built from a fork | Node, 690 MB image | Python + rdflib, 200 MB image shared with the sync |

* **LodView** has the right IRI model, but its RDF output is broken in its code, not in
  its configuration. The live deployment answers `500` to every RDF format with
  `error in query execution: cannot convert http://www.w3.org/1999/02/22-rdf-syntax-ns#type
  to interface org.apache.jena.rdf.model.Property -- it has no model`, a Jena API misuse in
  LodView's raw-data path. Fixing it means maintaining a Java fork (the setup already
  builds one), and its absolute URL settings tie it to one public location.
* **Trifid** (Zazuko) is maintained and its content negotiation works, but:
  * its rewriting model breaks dereferencing when IRIs are minted under a redirector such
    as w3id.org: a client asking for `https://w3id.org/…/X` in Turtle gets triples about
    `https://keng…/X`. It can be configured around this (no rewriting, with nginx forging
    the canonical host and path);
  * its HTML uses root-absolute assets and host-based links, so under a front proxy path
    such as `/climate-change-kg/` the CSS loads from the server root and links lose the
    prefix. Avoiding this requires configuring the public host and mount path into Trifid,
    which the front proxy decides;
  * it does not resolve hash IRIs, which the CCKG uses for its VoID descriptions
    (`…/indices#graph`).
* **Pubby** has been unmaintained for over a decade. **LinkedDataHub** is a full
  application platform, more than this use case needs.
* **kgserve ld** (about 800 lines of Python) therefore stays. It needs only a SPARQL
  endpoint, takes the IRI namespace from the data (`void:uriSpace`), and renders a page in
  10–130 ms on Oxigraph, including incoming links and labels. Its queries were tested on
  Oxigraph, Fuseki and QLever.

## Recommendation

* Default stack: **Oxigraph** from its vendor image, loaded over HTTP by the sync, plus
  **kgserve ld** and nginx. It needs no license and loads the CCKG in under a minute.
* **Fuseki** works the same way (`compose.fuseki.yaml`) when its Lucene text index is
  useful. Use **GraphDB** (`compose.graphdb.yaml`) where its inference or connectors are
  needed and a license is available; it also avoids the partial-data window.
* For very large graphs or heavy analytics: **QLever**, run with its own tooling.
