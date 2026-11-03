# neo API + workers. The lake, ontology versions and run store live on mounted volumes.
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY neo ./neo
COPY config ./config
COPY ontology/source ./ontology/source
COPY rules ./rules
COPY golden ./golden
COPY scripts ./scripts
RUN pip install --no-cache-dir -e . && useradd --create-home neo && chown -R neo /app
USER neo
EXPOSE 8080
# `neo serve` runs the API and in-process workers; scale workers separately with `neo worker`
CMD ["neo", "serve", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
