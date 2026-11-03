# QA E2-088 + E2-089 — Ejecucion local

Ejecutar desde una rama que contenga el codigo vigente:

```bash
chmod +x probar_E2_088_089.sh
./probar_E2_088_089.sh
```

Precondiciones: Docker Desktop operativo, acceso a Docker Hub/PyPI durante `docker build`, espacio suficiente en el disco, sin credenciales de la central. El runner crea **solo** una imagen QA y un contenedor/base PostgreSQL desechables. Se borran el contenedor y la red al salir, y **no** se borran imagenes, volumenes ni datos de otros servicios. Excluye expresamente la ruta `/opt/energyshark`.

**Criterios de aprobacion:** salida de pytest sin fallos ni errores, `E2-046 seller` con un asiento `SWAP_SETTLED` adicional y sin duplicados economicos, `DUPLICATE` y `relatedMsgId` consultables, NACK 422/403 y descarte del connector sin invocar negocio, anomalias consultables con `reasonCode` y sin payload sensible.

**Limitaciones:** Wallet se simula con `MockTransport` y AMQP con dobles locales. Las pruebas de backend requieren PostgreSQL real **solo en un contenedor efimero local**. Las filas de auditoria del nuevo test son fabricadas expresamente para probar las proyecciones. No son evidencia de una entrega AMQP real ni de una falla vista en produccion.

**Evidencias:** `datos_e2088_089/*_backend.txt`, `*_connector.txt` y `git rev-parse HEAD`. Los logs de pytest son los resultados; nunca convertir la existencia de estas instrucciones en un PASS ficticio.
