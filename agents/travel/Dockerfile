FROM python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca

ARG PIP_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app/agents/travel/src \
    OTEL_SERVICE_NAME=contoso-travel

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data ./data
COPY agents/travel ./agents/travel

RUN python -m pip install --no-cache-dir . \
    && python -m pip install --no-cache-dir -r agents/travel/requirements.txt \
    && python -m pip check

RUN rm -rf \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/site-packages/setuptools \
    /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
    /usr/local/bin/pip \
    /usr/local/bin/pip3 \
    /usr/local/bin/pip3.13

USER 65532:65532

ENTRYPOINT ["python", "-m", "contoso_travel_agent.traffic"]
