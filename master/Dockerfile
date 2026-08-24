FROM python:3.11-slim

WORKDIR /code

# curl se necesita para el HEALTHCHECK
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copiar el código de la aplicación de la API al contenedor
COPY app ./app

# exponer el puerto 8000 para la API
EXPOSE 8000

# HEALTHCHECK para verificar que la aplicación esté viva (cada 30 segundos)
# (lo que hace es hacer un GET a /health y si no responde con 200, el contenedor se considera no saludable)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Ejecutar la aplicación con uvicorn para servir la API en el puerto 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]