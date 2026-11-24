# knowledge-graph-serve

Serves a knowledge graph published on [Zenodo](https://zenodo.org) as a **SPARQL endpoint**
and as **linked data**, and keeps it in sync with the latest release of the record.

Point it at a Zenodo DOI and start it:

```bash
echo '{"zenodo": "10.5281/zenodo.24305088"}' > config.json
```

```bash
docker compose up -d --build
```

<http://127.0.0.1:8080/> now serves the Climate Change Knowledge Graph:

| Path | What |
| --- | --- |
| `/` | landing page: dataset metadata (from Zenodo), statistics, named graphs, sync status |
| `/sparql` | SPARQL endpoint (query only, GET/POST, CORS), with [inference](#inference) if chosen; browsers are sent to the editor |
| `/sparql-ui/` | SPARQL editor ([Matdata-eu's YASGUI](https://github.com/Matdata-eu/yasgui), as in the `mathiasvda/yasgui` image; `?query=` pre-fills it) |
| `/id/<path>` | the resource `<baseIri><path>`: HTML page, or by content negotiation Turtle, N-Triples, JSON-LD, RDF/XML, N3, N-Quads, TriG (`?format=ttl` or `.ttl` also work) |
| `/browse?iri=<IRI>` | any other IRI in the graph (e.g. ontology terms) |
| `/status.json` | synchronisation state |

All URLs that the service generates are relative, so a front web server can publish it
under any path, with HTTPS (see [Publishing](#publishing)). One instance serves one
knowledge graph; to serve several, run one instance per graph.

## How it works

```
                      ┌──────────────┐   SPARQL Update +
  Zenodo record ────▶ │     sync     │── Graph Store Protocol ──┐
  (latest version)    └──────────────┘                          ▼
                                                         ┌─────────────┐
                      ┌──────────────┐   /sparql         │    store    │
  front proxy ──────▶ │  web (nginx) │──────────────────▶│  (Oxigraph) │
  (optional)          │              │   everything else └─────────────┘
                      │              │──────────▶┌─────────────┐  ▲
                      └──────────────┘           │ linked-data │──┘ SPARQL
                                                 └─────────────┘
```

Four containers, each with one job:

* **store**: the triple store, from its vendor image (Oxigraph by default; Fuseki and
  GraphDB are alternatives). Only its query endpoint is reachable from outside.
* **sync**: checks the Zenodo record every `syncInterval` seconds. When a new version is
  published (a concept DOI always resolves to the latest one), it downloads the RDF files,
  verifies their checksums and loads them into the store over standard protocols. It
  empties the store with `DROP ALL`, then posts each file. While a release loads (about
  30 s for 6 million quads), queries see partial data; the landing page says so.
* **linked-data**: resource pages and RDF serialisations, the landing page and the SPARQL
  editor. It only needs the SPARQL endpoint.
* **web**: nginx, routing `/sparql` to the store and everything else to linked-data.

Formats: `.nt .nq .ttl .trig .rdf .owl .jsonld .n3`, optionally compressed (`.gz`, `.bz2`,
`.xz`), and `.zip` archives of them. Quad formats keep their named graphs; each file in a
triple format goes into its own named graph (`triplesGraph`).

## Configuration

Everything about the knowledge graph is in `config.json`, next to `compose.yaml`. Only
`zenodo` is required:

| Option | Default | Meaning |
| --- | --- | --- |
| `zenodo` | (required) | Zenodo record: DOI, record id or URL. A concept DOI follows new versions |
| `followLatest` | `true` | `false` pins the version given in `zenodo` |
| `files` | all RDF files | glob patterns of the record files to load, e.g. `["kg.nq.gz"]` |
| `excludeFiles` | `[]` | glob patterns of files not to load |
| `triplesGraph` | `"https://doi.org/{conceptdoi}#{key}"` | named graph for files in triple formats; placeholders `{key}` `{recid}` `{conceptrecid}` `{doi}` `{conceptdoi}`; `"default"` for the default graph |
| `baseIri` | the first `void:uriSpace` in the data | IRI namespace served at `/id/` |
| `resourcePath` | `"id"` | path where the IRIs under `baseIri` are served |
| `title` | the Zenodo record title | title shown in the pages |
| `syncInterval` | `3600` | seconds between checks for a new version (`0`: only at start and on demand) |
| `retryInterval` | `300` | after a failed check, retry this soon, doubling on each further failure up to `syncInterval` (`0`: no early retries) |
| `describeLimit` | `5000` | maximum statements in a resource description |
| `inverseLimit` | `50` | maximum incoming links shown on a resource page |
| `inference` | `"none"` | inference regime of the SPARQL endpoint, among those of the store (see [Inference](#inference)) |
| `zenodoApi` | `"https://zenodo.org/api"` | e.g. the Zenodo sandbox |
| `zenodoToken` | none | access token, only for restricted records |

A complete example:

```json
{
  "zenodo": "10.5281/zenodo.24305088",
  "files": ["kg.nq.gz", "void.ttl"],
  "baseIri": "https://w3id.org/climate-change-kg/data/",
  "title": "Climate Change Knowledge Graph",
  "syncInterval": 86400
}
```

`config.json` describes the knowledge graph only. Two things are container wiring, in the
compose files:

* the address the service listens on: `127.0.0.1:8080` by default, changed with the
  `KG_LISTEN` variable (e.g. `KG_LISTEN=127.0.0.1:8081 docker compose up -d`);
* the triple store (see [Other triple stores](#other-triple-stores)).

Data are kept in `./data` (`data/store` for the store, `data/sync` for the sync state).

## Publishing

The service answers at the root of `KG_LISTEN`. To publish it, add a location to the
front web server that also handles the host name and HTTPS. For example, with nginx and
the knowledge graph at `https://keng.istc.cnr.it/climate-change-kg/`:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name keng.istc.cnr.it;
    ssl_certificate     /etc/letsencrypt/live/keng.istc.cnr.it/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/keng.istc.cnr.it/privkey.pem;

    location = /climate-change-kg {
        return 301 /climate-change-kg/;
    }

    location /climate-change-kg/ {
        proxy_pass http://127.0.0.1:8080/;   # the trailing slash strips the prefix
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

Certificates, redirects from old paths and other sites are then all handled in the front
server, as usual. To serve several knowledge graphs, run one instance per graph (one
directory each, with its own `config.json` and `KG_LISTEN` port) and add one location per
graph.

To make the IRIs resolvable, redirect their namespace to the resource path. For
example, a w3id.org rule sending `https://w3id.org/climate-change-kg/data/(.*)` to
`https://keng.istc.cnr.it/climate-change-kg/id/$1`, as is set up today for the CCKG.

## Other triple stores

The store is replaced by adding a compose file:

* **Apache Jena Fuseki** (with a Lucene index on labels):
  `docker compose -f compose.yaml -f compose.fuseki.yaml up -d --build`
* **GraphDB**: `docker compose -f compose.yaml -f compose.graphdb.yaml up -d --build`. GraphDB 11
  needs a license file, also for the free edition: put it in `./graphdb.license`. The sync
  creates the repository `kg` and replaces its content in one transaction per release, so
  queries never see partial data.

Any store that supports SPARQL Update and the Graph Store Protocol works the same way.
Point the services at it with `KG_STORE_TYPE` (`oxigraph`, `fuseki` or `graphdb`, which
select default endpoints), or explicitly with `KG_STORE_QUERY`, `KG_STORE_UPDATE`,
`KG_STORE_DATA`, `KG_STORE_INFERENCE` (Fuseki's inference service), `KG_STORE_USER` and
`KG_STORE_PASSWORD`, and set `SPARQL_UPSTREAM` and `SPARQL_UPSTREAM_EXPLICIT` (without
inference) for nginx (see the two override files).

## Inference

With GraphDB or Fuseki, the SPARQL endpoint can answer with the statements that the
knowledge graph entails, under an inference regime chosen with `inference` in
`config.json`:

```json
{"zenodo": "10.5281/zenodo.24305088", "inference": "rdfs"}
```

| Store | Regimes |
| --- | --- |
| GraphDB | its [rulesets](https://graphdb.ontotext.com/documentation/11.0/reasoning.html#predefined-rulesets): `rdfs`, `rdfsplus`, `owl-horst`, `owl-max`, `owl2-ql`, `owl2-rl`, and the same names with `-optimized` |
| Fuseki | Jena's [reasoners](https://jena.apache.org/documentation/inference/): `rdfs`, `transitive` (`rdfs:subClassOf` and `rdfs:subPropertyOf` only), `owl-micro`, `owl-mini`, `owl` |
| Oxigraph | none |

`none` (the default) means explicit statements only. A regime that the store does not
offer stops the services with an error.

* **GraphDB** materialises the inferences when it loads a release: queries stay fast, the
  load takes longer. The ruleset is set when the repository is created, so when it changes,
  the sync recreates the repository and loads the release again. Restart the sync after
  editing `config.json`: `docker compose restart sync linked-data`.
* **Fuseki** infers at query time, with a Jena reasoner over the union of the graphs.
  There, the inferences are in the default graph only: named graphs (`GRAPH`) and the text
  index (`text:query`) are not available with inference, but are with `infer=false`. After
  each load, the sync makes the reasoner start again from the new release, and asks it a
  first query (the reasoner computes part of its deductions then, which may take a while on
  large graphs). The store reads the regime when it starts, so restart it too after editing
  `config.json`: `docker compose restart store sync linked-data`. Until then, the sync
  reports that the store does not serve the configured regime. Jena's reasoners work in
  memory: `rdfs` and `transitive` are workable on millions of statements, the OWL
  reasoners only on small graphs.

With inference, add `infer=false` to the URL of the SPARQL endpoint (e.g.
`/sparql?infer=false&query=...`) to query the explicit statements only. The resource pages,
RDF serialisations and statistics always show the explicit statements, with their graphs.

## Operations

```bash
docker compose logs -f sync              # synchronisation progress
```

```bash
docker compose kill -s HUP sync          # check Zenodo for a new version now
```

```bash
docker compose kill -s USR1 sync         # reload the current version
```

```bash
docker compose run --rm --no-deps sync resolve 10.5281/zenodo.24305088   # versions and files of a record
```

The sync runs continuously: no cron job is needed. It checks at start-up and then every
`syncInterval` seconds. A failed check (Zenodo unreachable, a download or a load failing) is
retried after `retryInterval` seconds, doubling at each further failure. If the store
loses its data (e.g. its volume is recreated), the next check reloads the release. The
state (loaded version, statement count, last error, next check) is in
`data/sync/state.json` and at `/status.json`.

## Development

```bash
pip install -e '.[test]'
```

```bash
python -m pytest
```

```bash
python bench/bench.py http://127.0.0.1:8080/sparql
```

Code layout:

* `kgserve/config.py`: `config.json`.
* `kgserve/zenodo.py`: the Zenodo API.
* `kgserve/rdffiles.py`: formats and archives.
* `kgserve/backends.py`: loading into the store, inference.
* `kgserve/hashindex.py`: hash IRIs, collected while loading.
* `kgserve/sync.py`: the sync service.
* `kgserve/ld/`: the linked-data server.
* `fuseki/entrypoint.sh`: Fuseki's inference service, from `config.json`.

The choice of components is discussed in [docs/alternatives.md](docs/alternatives.md).
