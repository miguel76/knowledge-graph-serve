#!/bin/bash
# Entry point of the Fuseki store container (compose.fuseki.yaml), run before the image's own.
#
# To the image's configuration (the dataset "ds": TDB with a union default graph and a
# Lucene index, where the sync loads the data) it adds the service "kg", which answers the
# public SPARQL endpoint with the inference regime chosen by "inference" in config.json:
#
#   none        the dataset "ds" itself (named graphs, text search)
#   rdfs        Jena's RDFS reasoner           owl-micro   Jena's OWL Micro reasoner
#   transitive  subClassOf/subPropertyOf only  owl-mini    Jena's OWL Mini reasoner
#               (Jena's transitive reasoner)   owl         Jena's OWL (full) reasoner
#
# With a reasoner, the default graph of "kg" is the union of the graphs plus the inferences
# (no named graphs, no text search: those stay on "ds"). Its query endpoint
# "inference-<regime>" tells the sync which regime the store runs, and its update endpoint
# lets the sync discard the reasoner's deductions after loading a release (every update
# there is refused: its union graph is read-only). Only kg/sparql is published, by nginx.
#
# The regime is read when the store starts: after changing it, restart the store.
set -euo pipefail

config=${KG_CONFIG:-/config/config.json}
regime=$(sed -n 's/.*"inference"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$config" 2>/dev/null | head -n 1 \
         | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
regime=${regime:-none}

case "$regime" in
  none) reasoner="" ;;
  rdfs) reasoner=RDFSExptRuleReasoner ;;
  transitive) reasoner=TransitiveReasoner ;;
  owl-micro) reasoner=OWLMicroFBRuleReasoner ;;
  owl-mini) reasoner=OWLMiniFBRuleReasoner ;;
  owl) reasoner=OWLFBRuleReasoner ;;
  *)
    echo "config.json: inference \"$regime\" is not offered by Fuseki" \
         "(none, rdfs, transitive, owl-micro, owl-mini, owl)" >&2
    exit 1 ;;
esac

# Start each time from the image's configuration (the container may be restarted).
[ -f "$ASSEMBLER.image" ] || cp "$ASSEMBLER" "$ASSEMBLER.image"
cp "$ASSEMBLER.image" "$ASSEMBLER"

if [ -z "$reasoner" ]; then
  cat >> "$ASSEMBLER" <<EOF

# Added by knowledge-graph-serve (fuseki/entrypoint.sh): no inference.
<#kg> rdf:type fuseki:Service ;
    fuseki:name "kg" ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "sparql" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "inference-none" ] ;
    fuseki:dataset <#text> .
EOF
else
  cat >> "$ASSEMBLER" <<EOF

# Added by knowledge-graph-serve (fuseki/entrypoint.sh): inference "$regime".
<#kg> rdf:type fuseki:Service ;
    fuseki:name "kg" ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "sparql" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "inference-$regime" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:update ; fuseki:name "update" ] ;
    fuseki:dataset <#inference> .

<#inference> rdf:type ja:RDFDataset ;
    ja:defaultGraph [
        rdf:type ja:InfModel ;
        ja:reasoner [ ja:reasonerURL <http://jena.hpl.hp.com/2003/$reasoner> ] ;
        ja:baseModel [ rdf:type tdb:GraphTDB ; tdb:dataset <#tdb> ; tdb:graphName <urn:x-arq:UnionGraph> ]
    ] .
EOF
fi
echo "knowledge-graph-serve: service /kg with inference: $regime"

exec /docker-entrypoint.sh "$@"
