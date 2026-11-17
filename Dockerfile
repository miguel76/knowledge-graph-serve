ARG OXIGRAPH_VERSION=0.5.13

FROM ghcr.io/oxigraph/oxigraph:${OXIGRAPH_VERSION} AS oxigraph

FROM python:3.13-slim
COPY --from=oxigraph /usr/local/bin/oxigraph /usr/local/bin/oxigraph

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kgserve ./kgserve
RUN pip install --no-cache-dir . && oxigraph --version

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["python", "-m", "kgserve"]
CMD ["--help"]
