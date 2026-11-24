# E3-079 · Caída post-commit y recuperación del relay

Prueba aislada del camino real de AD8: el productor confirma negocio y outbox en la misma transacción, el relay publica después, y el sistema no pierde la intención si el proceso muere o el broker se cae.

## Qué se probó

| Escenario | Qué simula | Qué se exige |
| :--- | :--- | :--- |
| 1. Crash post-commit | El productor hizo `commit` y ningún relay estaba vivo | Al arrancar el relay publica el envelope guardado, sin cambios |
| 2. Broker cortado | RabbitMQ detenido mientras el relay está vivo | La fila sigue `PENDING`; al volver el broker publica el mismo mensaje |
| 3. Lease vencido | Un relay reclama la fila y muere antes de publicar | Otro relay espera el lease, republica la misma intención y confirma |

En los tres casos se verifica: `msgId` e `idpk` idénticos, payload idéntico al persistido, una sola entrega y backlog (`dispatch_required AND status <> 'PUBLISHED'`) en cero.

## Entorno aislado

`docker-compose.e3-079.yml` levanta servicios propios, sin tocar el despliegue:

- `postgres:16-alpine` con `tmpfs`: la base desaparece al bajar el entorno.
- `rabbitmq:3.13-management-alpine` local, con usuario `city.KLD` creado por el driver. RabbitMQ exige que la propiedad `user_id` del mensaje coincida con el usuario autenticado, igual que el broker real.
- `master` y `migrate` construidos desde el repositorio, con `DATABASE_URL` local.
- `relay` con `connector/relay_only.py`: solo el relay de broker, sin consumidor de ciudad. Es el proceso que el driver arranca y mata.

Nada apunta al broker público (`broker.iic2173.org`) ni a la base real. No hay credenciales reales en el compose: los valores son locales y desechables.

## Resultado observado

```
$ python3 tools/e3_079_outbox_crash.py

== Levantando PostgreSQL, RabbitMQ y master aislados

== Escenario 1: crash tras commit, sin relay vivo
OK   la fila quedó confirmada en estado PENDING (PENDING)
OK   ningún intento de publicación ocurrió todavía
OK   la intención sigue marcada para despacho
OK   RabbitMQ no recibió nada antes del relay
OK   backlog inicial en 1 (1)
     recuperación crash post-commit: 1.2s (objetivo AD8 <= 60s)
OK   el envelope publicado conserva msgId
OK   el envelope publicado conserva idpk
OK   el payload publicado es idéntico al guardado
OK   se entregó exactamente un mensaje (1)
OK   el mensaje recibido tiene el mismo msgId
OK   el mensaje recibido tiene el mismo idpk
OK   el backlog vuelve a cero (0)

== Escenario 2: broker cortado y recuperación
OK   el broker quedó realmente inaccesible
OK   con RabbitMQ detenido la fila sigue PENDING (PENDING)
OK   el relay no inventó filas nuevas (filas=2)
OK   backlog conserva la intención (1)
     recuperación broker recuperado: 14.2s (objetivo AD8 <= 60s)
OK   el payload republicado es idéntico
OK   msgId sin cambios tras la reconexión
OK   el backlog vuelve a cero (0)

== Escenario 3: relay muerto con lease vigente
OK   la fila quedó reclamada por un relay que murió
OK   mientras el lease vive no se republica
     recuperación lease vencido: 4.1s (objetivo AD8 <= 60s)
OK   el segundo intento conserva el mismo payload
OK   el intento quedó registrado (2)
OK   el backlog vuelve a cero (0)

== Bajando el entorno aislado

TIEMPO crash post-commit: 1.2s
TIEMPO broker recuperado: 14.2s
TIEMPO lease vencido: 4.1s
RESULTADO: caída post-commit recuperada sin pérdidas; backlog en cero
```

Cuatro corridas consecutivas, todas verdes:

| Corrida | Crash post-commit | Broker recuperado | Lease vencido |
| :--- | ---: | ---: | ---: |
| 1 | 1.8 s | 8.8 s | 3.7 s |
| 2 | 1.2 s | 6.2 s | 4.1 s |
| 3 | 3.3 s | 17.4 s | 2.9 s |
| 4 | 1.2 s | 14.2 s | 4.1 s |

El peor caso observado es 17.4 s, por debajo del objetivo de 60 s de AD8. El tiempo del escenario 2 incluye el arranque completo de RabbitMQ, no solo la reconexión del relay.

## Cómo lo demuestra el código

- **Commit único:** `tools/e3_079_outbox_crash.py` siembra con `enqueue_broker` dentro de una sesión y hace `commit`; el proceso de siembra termina ahí. La fila queda `PENDING` con `attempt_count = 0` y `dispatch_required = true`.
- **Sin pérdida:** el relay reclama por `POST /internal/outbox-relay/claims`, publica con `mandatory=True` y exige `Basic.Ack`, y confirma con `POST /internal/outbox-relay/{id}/result`.
- **Sin invención:** el mensaje que llega a RabbitMQ conserva `msgId`, `idpk` y payload del registro. El escenario 2 comprueba además que no aparecen filas nuevas.
- **Lease:** el escenario 3 marca la fila `CLAIMED` con un `lease_until` de 5 s, como un relay muerto. El relay nuevo no la toca hasta que vence, y luego la publica.
- **Backlog:** se mide con `SELECT count(*) FROM outbound_messages WHERE dispatch_required AND status <> 'PUBLISHED'`; vale cero al final de cada escenario.

## Límites declarados

- **Semántica al-menos-una-vez.** Si el relay publica y muere antes de confirmar el resultado, el lease vence y republica el mismo envelope. El receptor puede ver dos copias con el mismo `msgId`; la deduplicación es responsabilidad del consumidor. El caso está cubierto a nivel de persistencia en `master/tests/test_e3036_outbox_faults.py`; esta prueba no fuerza la ventana exacta entre `publish` y `result`.
- **Solo relay de broker.** El relay de Step Functions y el de callbacks de comprador no se ejercitan aquí: no hay AWS en el entorno aislado.
- **No mide producción.** Los tiempos son de Docker local. La medición con telemetría real corresponde a E3-082.
- **No modifica código de producción.** El único archivo nuevo en `connector/` es `relay_only.py`, un entrypoint que arranca solo el relay existente; no cambia la lógica de publicación.

## Reproducir

```bash
docker compose version   # requiere Compose v2
python3 tools/e3_079_outbox_crash.py

# Dejar el entorno arriba para inspección manual:
python3 tools/e3_079_outbox_crash.py --keep
docker compose -f docker-compose.e3-079.yml logs -f relay
docker compose -f docker-compose.e3-079.yml down -v
```

## Archivos

- `docker-compose.e3-079.yml`: entorno aislado (Postgres, RabbitMQ, master, relay).
- `connector/relay_only.py`: entrypoint del relay aislado.
- `connector/Dockerfile`: copia el nuevo entrypoint.
- `tools/e3_079_outbox_crash.py`: driver con los tres escenarios y sus verificaciones.
- `docs/qa/E3-079-caida-post-commit-relay.md`: este documento.
