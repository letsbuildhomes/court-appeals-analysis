FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.12.16@sha256:adc68cd785ca65ea25c0611043b0a00b4ea3a22e1b54102fc084406d888082ee /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0
WORKDIR /build
COPY pyproject.toml uv.lock ./
# Export only the MCP closure from the existing lock, omitting pipeline-only roots.
RUN uv export --locked --extra research-mcp --no-dev --no-emit-project --prune beautifulsoup4 --prune duckdb -o requirements.txt \
    && uv venv /opt/research-venv \
    && uv pip install --python /opt/research-venv/bin/python --require-hashes -r requirements.txt

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/research-venv/bin:${PATH}"
COPY --from=dependencies /opt/research-venv /opt/research-venv
WORKDIR /app
# Only the snapshot reader and the MCP server; no pipeline code or data.
COPY court/__init__.py court/
COPY court/research/__init__.py court/research/snapshot.py court/research/mcp.py court/research/
USER 10001:10001
EXPOSE 8770
ENTRYPOINT ["python", "-m", "court.research.mcp", "--snapshot", "/snapshot", "--transport", "streamable-http"]
# Container networking requires external binding; publish its port on loopback.
CMD ["--host", "0.0.0.0", "--port", "8770"]
