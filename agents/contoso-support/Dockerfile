FROM python:3.13.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
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

USER 65532:65532
EXPOSE 8088

CMD ["python", "-m", "contoso_foundry.support_agent.runtime"]
