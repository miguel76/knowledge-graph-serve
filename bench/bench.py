"""Time a few representative queries against a SPARQL endpoint.

    python bench/bench.py http://localhost:8080/kg/sparql [--runs 3] [--iri <resource IRI>]

Queries: a full count, a type histogram, a resource description as done by the linked-data
server, a label substring search and a per-graph aggregation. Prints the median time of
each query and, separately, of its first (cold) run: some stores, like QLever, cache results.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgserve import http  # noqa: E402
from kgserve.ld.describe import describe_query, inverse_query  # noqa: E402

DEFAULT_IRI = "https://w3id.org/climate-change-kg/data/climdex/indices/CDD"


def queries(iri: str) -> dict[str, str]:
    return {
        "count": "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }",
        "types": "SELECT ?t (COUNT(?s) AS ?n) WHERE { GRAPH ?g { ?s a ?t } } GROUP BY ?t ORDER BY DESC(?n) LIMIT 20",
        "describe": describe_query([iri], 5000),
        "inverse": inverse_query(iri, 50),
        "label-search": ("SELECT ?s ?l WHERE { GRAPH ?g { ?s <http://www.w3.org/2000/01/rdf-schema#label> ?l } "
                         'FILTER(CONTAINS(LCASE(STR(?l)), "drought")) } LIMIT 100'),
        "per-graph": "SELECT ?g (COUNT(DISTINCT ?p) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--iri", default=DEFAULT_IRI)
    args = parser.parse_args()
    for name, query in queries(args.iri).items():
        times = []
        first = 0.0
        rows = None
        for run in range(args.runs + 1):
            started = time.perf_counter()
            try:
                rows = http.select(args.endpoint, query, timeout=600)
            except Exception as err:  # noqa: BLE001
                print(f"{name:14} ERROR {err}")
                break
            elapsed = time.perf_counter() - started
            if run:
                times.append(elapsed)
            else:
                first = elapsed
        else:
            print(f"{name:14} first {first * 1000:9.1f} ms   median {statistics.median(times) * 1000:9.1f} ms  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
