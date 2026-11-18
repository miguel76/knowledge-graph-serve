# knowledge-graph-serve

A tool to serve any knowledge graph whose dump is published on [Zenodo](https://zenodo.org):
it follows the Zenodo record, loads every new release into a triple store, and offers

* a **SPARQL endpoint** (read-only, CORS enabled) and a SPARQL editor (YASGUI);
* **linked data**: every IRI of the knowledge graph dereferences to an HTML page or, by
  content negotiation, to Turtle, N-Triples, JSON-LD, RDF/XML, N3, N-Quads or TriG;
* a landing page with the dataset metadata (from Zenodo) and the synchronisation status.

It generalises [climate-change-kg-docker](https://github.com/hacid-project/climate-change-kg-docker)
(GraphDB + LodView + YASGUI + nginx): nothing is specific to one knowledge graph, data are
loaded automatically, and LodView is replaced by a linked-data server that serves all RDF
serialisations. The evaluation of triple stores and linked-data front ends that led to these
choices is in [docs/alternatives.md](docs/alternatives.md).

## How it works

```
                     ┌──────────────────────── store ──────────────────────────┐
 Zenodo record ────▶ │ kgserve sync: resolve latest version → download → verify │
 (concept DOI)       │ md5 → bulk load into a new Oxigraph store → swap server  │
                     │                   oxigraph serve-read-only :7878          │
                     └──────────────────────────────▲───────────────────────────┘
                                                    │ SPARQL
           ┌─────────── web (nginx) ───────────┐    │    ┌──── linked-data ─────┐
 client ──▶│ {BASE_PATH}/sparql  ──────────────┼────┘    │ kgserve ld           │
           │ {RESOURCE_PATH}/…   ──────────────┼────────▶│ resource pages, RDF, │
           │ {BASE_PATH}/ (landing, editor)  ──┼────────▶│ landing, SPARQL UI   │
           └───────────────────────────────────┘         └──────────────────────┘
```

* **Sync** (`kgserve sync`): a long-running process that checks the record every
  `SYNC_INTERVAL` seconds (no cron needed), and retries sooner after a failure. A concept DOI
  always resolves to the latest version. When there is a new version it downloads the RDF
  files (`.nt .nq .ttl .trig .rdf .owl .jsonld .n3`, optionally `.gz/.bz2/.xz`, or `.zip`
  archives of them), verifies their checksums and loads them. Quad files keep their named
  graphs; each triple file goes to its own named graph (`TRIPLES_GRAPH`).
* **Blue/green loading** (default Oxigraph backend): each release is bulk-loaded offline
  into a fresh store while the previous one keeps answering queries; then the read-only
  server is restarted on the new store (about 0.2 s; no failed requests in testing). If
  anything fails, the previous release stays online.
* **Linked data** (`kgserve ld`): `{PUBLIC_URL}{RESOURCE_PATH}/x` describes the IRI
  `{KG_BASE_IRI}x`. RDF responses use the canonical IRIs (e.g. `https://w3id.org/…`), while
  HTML pages link to the local pages. Hash IRIs (`doc#a`) are served with their document
  (`doc`). Any other IRI of the graph can be browsed at `{BASE_PATH}/browse?iri=…`.

The store, the linked-data server and nginx are three services of one Docker Compose
project. The `kgserve` image (Python + the Oxigraph binary) is about 230 MB.

## Quick start

Requirements: Docker with Compose v2.24.4 or later.

```bash
git clone https://github.com/miguel76/knowledge-graph-serve
```

```bash
cd knowledge-graph-serve
```

```bash
docker compose build
```

Generate a `.env` for your record. `init` reads the record, finds the IRI namespace in its
VoID description (`void:uriSpace`), if any, and derives the URL paths from it:

```bash
docker compose run --rm --no-deps store init 10.5281/zenodo.24305088 -o - --public-url http://localhost:8080 > .env
```

```bash
docker compose up -d
```

Then open <http://localhost:8080/climate-change-kg/>. The first load takes a few minutes
(downloading and indexing); `docker compose logs -f store` shows the progress.

For the Climate Change Knowledge Graph the generated `.env` is:

```ini
ZENODO_RECORD=10.5281/zenodo.24305088
KG_TITLE=Climate Change Knowledge Graph (CCKG)
PUBLIC_URL=http://localhost:8080
BASE_PATH=/climate-change-kg
RESOURCE_PATH=/climate-change-kg/id
KG_BASE_IRI=https://w3id.org/climate-change-kg/data/
HTTP_PORT=8080
SYNC_INTERVAL=3600
```

and the service offers:

| URL | What |
| --- | --- |
| `/climate-change-kg/` | landing page: metadata, statistics, named graphs |
| `/climate-change-kg/sparql` | SPARQL endpoint (GET/POST); browsers are redirected to the editor |
| `/climate-change-kg/sparql-ui/` | SPARQL editor (`?query=` pre-fills it) |
| `/climate-change-kg/id/climdex/indices/CDD` | `https://w3id.org/climate-change-kg/data/climdex/indices/CDD`, by content negotiation |
| `/climate-change-kg/id/climdex/indices/CDD.ttl` or `…?format=ttl` | a given format (`ttl nt jsonld rdf n3 nq trig html`) |
| `/climate-change-kg/browse?iri=…` | any other IRI in the graph (e.g. ontology terms) |
| `/climate-change-kg/status.json` | synchronisation state |

## Configuration

All settings are environment variables, normally in `.env`; see [.env.example](.env.example)
for the full list. The main ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ZENODO_RECORD` | (required) | record id, DOI or URL; a concept DOI follows new versions |
| `ZENODO_FOLLOW_LATEST` | `true` | `false` pins the given version |
| `KG_FILES`, `KG_EXCLUDE_FILES` | all RDF files | glob patterns of the record files to load |
| `TRIPLES_GRAPH` | `https://doi.org/{conceptdoi}#{key}` | named graph of triple files (`default` for the default graph) |
| `SYNC_INTERVAL` | `3600` | seconds between checks (`0`: only on start and on demand) |
| `SYNC_RETRY_INTERVAL` | `300` | after a failed check, retry this soon, doubling on each further failure up to `SYNC_INTERVAL` (`0`: no early retries) |
| `PUBLIC_URL` | `http://localhost:8080` | public origin, used in the pages |
| `BASE_PATH` | `/kg` | path of the service |
| `RESOURCE_PATH` | `{BASE_PATH}/id` | path where the IRIs are dereferenced |
| `KG_BASE_IRI` | first `void:uriSpace` in the data | IRI namespace served at `RESOURCE_PATH` |
| `HTTP_PORT` | `8080` | published HTTP port |
| `DATA_PATH` | `./data` | where downloads, stores and state are kept |

To make the IRIs resolvable, redirect the namespace to the service. For example a w3id.org
rule sending `https://w3id.org/climate-change-kg/data/(.*)` to
`https://keng.istc.cnr.it/climate-change-kg/id/$1` (this is how the CCKG is set up today).

## Operations

```bash
docker compose logs -f store            # sync and load progress
```

```bash
docker compose kill -s HUP store        # check Zenodo for a new version now
```

```bash
docker compose kill -s USR1 store       # reload the current version (e.g. after changing KG_FILES)
```

```bash
docker compose run --rm --no-deps store resolve 10.5281/zenodo.24305088   # show versions and files
```

The state (loaded version, statement count, errors, next check) is in
`data/state.json` and at `{BASE_PATH}/status.json`.

### Choosing another triple store

Oxigraph is the default (see [docs/alternatives.md](docs/alternatives.md) for why).

* **QLever**, for large graphs and fast analytic queries:
  `docker compose -f compose.yaml -f compose.qlever.yaml up -d --build`. Like Oxigraph it
  is managed by the sync: each release is indexed offline (formats QLever does not read
  are converted on the fly). The public port then switches to the new server once it
  answers, so releases are swapped without downtime. Options: `QLEVER_MEMORY` (query
  memory, default `4G`), `QLEVER_TIMEOUT` (default `60` s), `QLEVER_QUERY_THREADS`
  (default `4`), `QLEVER_INDEX_MEMORY`, `QLEVER_INDEX_SETTINGS` (path, inside the container, of a
  JSON settings file for `qlever-index`), `QLEVER_SERVER_ARGS` (extra `qlever-server` arguments).

The sync also works with stores it does not manage:

* **GraphDB**: `docker compose -f compose.yaml -f compose.graphdb.yaml up -d`. The
  repository is created if missing and each release replaces its content in one RDF4J
  transaction. GraphDB 11 needs a license file, also for the free edition: put it in
  `./graphdb.license`. Any other RDF4J server works too (`BACKEND=rdf4j`,
  `GRAPHDB_URL`, `GRAPHDB_REPOSITORY_CONFIG`).
* **Apache Jena Fuseki**: `docker compose -f compose.yaml -f compose.fuseki.yaml up -d`
  (with a Lucene index on labels). Loads are not atomic: during a load the endpoint
  serves partial data.
* Any store with SPARQL Update and the Graph Store Protocol: `BACKEND=gsp` with
  `GSP_DATASET_URL`, `GSP_UPDATE_URL` and `GSP_QUERY_URL`.

The linked-data server only needs a SPARQL endpoint (`SPARQL_ENDPOINT`). Its queries
have been tested on Oxigraph, Fuseki and QLever.

### HTTPS

Set `SERVER_NAME` (the public host name) in `.env`, then get the first certificate. The
HTTP server must be running for the ACME challenge, so the first time start it without
TLS:

```bash
docker compose up -d web
```

```bash
docker compose -f compose.yaml -f compose.tls.yaml run --rm certbot certonly --webroot -w /var/www/certbot -d "$SERVER_NAME"
```

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

To renew the certificate later:

```bash
docker compose -f compose.yaml -f compose.tls.yaml run --rm certbot renew
```

```bash
docker compose -f compose.yaml -f compose.tls.yaml restart web
```

Site-specific nginx additions, such as legacy redirects, go in `nginx/extra/*.inc` (see
the example there).

### Migrating climate-change-kg-docker

1. Generate `.env` with `init` (above) and set `PUBLIC_URL=https://keng.istc.cnr.it` and
   `SERVER_NAME=keng.istc.cnr.it`.
2. Copy the existing `certbot/` directory and use `compose.tls.yaml`.
3. Copy `nginx/extra/legacy-redirects.inc.example` to `nginx/extra/legacy-redirects.inc`
   for the old `/hacid-cs-kg` paths.
4. The paths stay the same (`/climate-change-kg/sparql`, `/climate-change-kg/sparql-ui/`,
   `/climate-change-kg/id/…`), so the w3id.org redirect and existing clients keep working.

Not covered: the writable `climate-change-kg-cases` repository of the old setup. It is
not a Zenodo release; keep it in GraphDB, or in a separate store, next to this service.

## Development

```bash
pip install -e '.[test]'
```

```bash
python -m pytest
```

```bash
python bench/bench.py http://localhost:8080/climate-change-kg/sparql
```

Code layout: `kgserve/zenodo.py` (Zenodo API), `kgserve/rdffiles.py` (formats and
archives), `kgserve/backends.py` (stores), `kgserve/qlever.py` and `kgserve/proxy.py` (QLever
backend), `kgserve/sync.py` (daemon),
`kgserve/ld/` (linked-data server). The only dependency is `rdflib`, used by the
linked-data server and `init`; the sync itself needs only the standard library.
