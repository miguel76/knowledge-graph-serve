FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kgserve ./kgserve
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["python", "-m", "kgserve"]
CMD ["--help"]
