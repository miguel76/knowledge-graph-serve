# The images of knowledge-graph-serve, one per target, published by
# .github/workflows/docker.yml (the compose files name them):
#
#   kgserve  ghcr.io/miguel76/knowledge-graph-serve         the sync and linked-data services
#   web      ghcr.io/miguel76/knowledge-graph-serve-web     nginx with its configuration
#   fuseki   ghcr.io/miguel76/knowledge-graph-serve-fuseki  Fuseki with the inference service
#
# The web and fuseki images are their vendor images plus one file. Without --target, the
# kgserve image is built.

FROM nginx:1.30-alpine AS web
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
# The store's query endpoints (here Oxigraph's); the compose files set them for each store.
ENV SPARQL_UPSTREAM=http://store:7878/query \
    SPARQL_UPSTREAM_EXPLICIT=http://store:7878/query

FROM secoresearch/fuseki:6.2.0 AS fuseki
COPY fuseki/entrypoint.sh /kgserve/entrypoint.sh
ENTRYPOINT ["/bin/bash", "/kgserve/entrypoint.sh"]
# The image's command (setting an entrypoint clears it).
CMD ["java", "-cp", "*:/javalibs/*", "org.apache.jena.fuseki.main.cmds.FusekiServerCmd"]

FROM python:3.13-slim AS kgserve
WORKDIR /app
COPY pyproject.toml README.md ./
COPY kgserve ./kgserve
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["python", "-m", "kgserve"]
CMD ["--help"]
