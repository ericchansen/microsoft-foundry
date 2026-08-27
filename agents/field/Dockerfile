FROM python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

ARG PIP_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data/seed ./data/seed

RUN python -m pip install --no-cache-dir ".[field]" \
    && python -m pip check \
    && addgroup --system --gid 65532 field \
    && adduser --system --uid 65532 --ingroup field --home /nonexistent --no-create-home field \
    && mkdir -p /tmp/contoso-field-data \
    && chown -R field:field /tmp/contoso-field-data

RUN rm -rf \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/site-packages/setuptools \
    /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
    /usr/local/bin/pip \
    /usr/local/bin/pip3 \
    /usr/local/bin/pip3.13

USER 65532:65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["contoso_foundry.field.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
