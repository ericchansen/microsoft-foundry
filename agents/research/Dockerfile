FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ARG PIP_INDEX_URL

ENV PYTHONPATH=/app/src \
    HOME=/tmp

WORKDIR /app

COPY agents/research/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY config /app/config
COPY data /app/data
RUN python -m pip install --no-cache-dir --no-deps .
RUN python -m pip check

RUN rm -rf \
    /usr/local/lib/python3.13/site-packages/pip \
    /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.13/site-packages/setuptools \
    /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
    /usr/local/bin/pip \
    /usr/local/bin/pip3 \
    /usr/local/bin/pip3.13

USER 65532:65532

ENV OTEL_SERVICE_NAME=contoso-research
EXPOSE 8088

CMD ["python", "-m", "contoso_foundry.research.hosted"]
