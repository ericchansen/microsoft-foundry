FROM python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

ARG PIP_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/var/lib/contoso-support \
    OTEL_SERVICE_NAME=contoso-support \
    ENABLE_INSTRUMENTATION=true \
    ENABLE_SENSITIVE_DATA=false

WORKDIR /app

COPY agents/contoso-support/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data ./data

RUN python -m pip install --no-cache-dir --no-deps .
RUN python -m pip check
RUN python -m contoso_foundry.cli data build --out /tmp/contoso-build \
    && mkdir --parents /opt/contoso-support \
    && cp /tmp/contoso-build/contoso.db /opt/contoso-support/contoso.db \
    && sha256sum /opt/contoso-support/contoso.db | cut -d " " -f 1 \
        > /opt/contoso-support/contoso.db.sha256 \
    && rm -rf /tmp/contoso-build \
    && chmod 0555 /opt/contoso-support \
    && chmod 0444 /opt/contoso-support/contoso.db /opt/contoso-support/contoso.db.sha256 \
    && mkdir --parents /var/lib/contoso-support \
    && chown 65532:65532 /var/lib/contoso-support

RUN rm -rf \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/site-packages/setuptools \
    /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
    /usr/local/bin/pip \
    /usr/local/bin/pip3 \
    /usr/local/bin/pip3.13

USER 65532:65532
EXPOSE 8088

CMD ["python", "-m", "contoso_foundry.support_agent.runtime"]
