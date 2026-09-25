FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY --from=ghcr.io/astral-sh/uv:0.12.16@sha256:adc68cd785ca65ea25c0611043b0a00b4ea3a22e1b54102fc084406d888082ee /uv /uvx /usr/local/bin/

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    poppler-utils tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/court-venv \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/court-venv/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

WORKDIR /workspace
CMD ["python", "text_finish.py", "status"]
