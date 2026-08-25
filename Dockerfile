FROM python:3.13.7-slim

WORKDIR /app

COPY agents/research/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY config /app/config
COPY data /app/data
RUN python -m pip install --no-cache-dir --no-deps .

ENV OTEL_SERVICE_NAME=contoso-research
EXPOSE 8088

CMD ["python", "-m", "contoso_foundry.research.hosted"]
