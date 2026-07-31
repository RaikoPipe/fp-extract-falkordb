# syntax=docker/dockerfile:1.7

# Pinned base image for reproducible builds.
FROM python:3.11-slim

WORKDIR /app

# System deps required by some Python wheels (e.g. unstructured, lxml).
# gosu is used by the entrypoint to drop from root to appuser at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gosu \
        libxml2 \
        libxslt1.1 \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Newer pip is required to parse the `file:src/document-to-markdown` URL
# dependency declared in pyproject.toml (pip <25 rejects that form).
RUN pip install --no-cache-dir --upgrade pip

# Copy project metadata + source FIRST so the path dependency
# (docprep[ollama] @ file:src/document-to-markdown) resolves during install.
COPY pyproject.toml ./
COPY src/ src/

# Non-editable install so the image is self-contained (no source bind-mount).
RUN pip install --no-cache-dir ".[chainlit]"

# Runtime assets: Chainlit config + localized chat UI markdown.
COPY .chainlit/ .chainlit/
COPY chainlit.md chainlit_en-US.md chainlit_de-DE.md ./

# Entrypoint: chowns the bind-mounted /app/data to appuser (the image's
# own chown is shadowed by the host-owned bind mount), then drops to
# appuser via gosu and execs the CMD. Image runs as root only during
# entrypoint; the long-running chainlit process runs as appuser.
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create the non-root user the entrypoint drops to.
RUN useradd --create-home --uid 1001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

# Chainlit UI.
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["chainlit", "run", "src/falkordb_harness/chainlit_app.py", "--host", "0.0.0.0", "--port", "8000"]