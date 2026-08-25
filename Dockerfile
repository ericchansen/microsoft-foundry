FROM python:3.13.7-slim-bookworm

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

USER 65532:65532
EXPOSE 8088

CMD ["python", "-m", "contoso_foundry.support_agent.runtime"]
