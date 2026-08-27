FROM python:3.12-slim

WORKDIR /app

# ca-certificates mantiene activa la validación TLS del broker público.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connector.py healthcheck.py ./

# File checking: no hace falta exponer una API HTTP para connector.
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
  CMD python /app/healthcheck.py

CMD ["python", "connector.py"]
