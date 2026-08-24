FROM python:3.11-slim

WORKDIR /code

# Necesario para que Python pueda validar el certificado TLS del broker
# > Instala el paquete de certificados de CA y luego actualiza el store de certificados de Python
RUN apt-get update && apt-get install -y ca-certificates && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connector.py .

# Healthcheck por archivo (RNF7): revisa que el heartbeat se haya escrito
# en los últimos 60 segundos. find no encuentra nada -> falla el check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD find /tmp/connector_alive -mmin -1 | grep -q connector_alive || exit 1

CMD ["python", "connector.py"]
