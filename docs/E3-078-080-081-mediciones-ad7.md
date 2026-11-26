# E3-078 · E3-080 · E3-081 — vista previa de infraestructura y medición de AD7

Herramientas y evidencia para tres tarjetas del carril de Cloud/IaC y QA del AD7.
Las tres comparten la misma regla: **no modifican el estado productivo**. La vista
previa de E3-078 nunca aplica un change set y las mediciones de E3-080/E3-081 solo
ejecutan consultas de lectura.

| Tarjeta | Entregable | Criterio de aceptación |
| --- | --- | --- |
| E3-078 | `tools/e3_078_change_set_preview.py` | Plan/synth y validación sin apply contra recursos productivos |
| E3-080 | `tools/e3_080_saga_timing.py` | Consulta que demuestra el criterio de AD7 o registra desviaciones y causa por `cycleId` |
| E3-081 | `tools/e3_081_reservas.py` | Reservas locales retenidas al cierre y 2–3 objetivos del ADR-007 medidos |

## Estado de la evidencia

| Evidencia | Estado |
| --- | --- |
| `cdk synth` de los tres stacks | Ejecutado; conteo de recursos abajo |
| Vista previa de change set (E3-078) | Herramienta probada con cliente falso; corrida real pendiente de credenciales |
| Consulta AD7 sobre PostgreSQL (E3-080/E3-081) | Ejecutada contra un PostgreSQL 16 efímero con datos sintéticos |
| Medición sobre datos productivos | **Pendiente.** Requiere `DATABASE_URL` del entorno real |

La corrida con datos sintéticos valida el esquema, las consultas y los dos
caminos (cumple y no cumple). No es evidencia de producción y no se presenta como
tal.

## E3-078 — vista previa sin apply

`tools/e3_078_change_set_preview.py` crea un change set a partir del template
sintetizado, describe los recursos que se crearían o modificarían y lo elimina.

Garantías que el código sostiene:

- No existe ninguna llamada a `ExecuteChangeSet`. Las pruebas unitarias usan un
  cliente falso sin ese método y fallan si se invocara.
- Con `--change-set-type CREATE` (por defecto) rechaza un stack existente que no
  esté en `REVIEW_IN_PROGRESS`, para no tocar recursos productivos.
- El change set se elimina siempre (`finally`). Si la corrida creó el stack en
  `REVIEW_IN_PROGRESS`, también se elimina.
- No crea buckets. Si el template supera 51200 bytes, exige `--upload-bucket`
  con un bucket existente (el objeto se borra al terminar) o `--template-url`.
- Quita solo el guardia `BootstrapVersion` que CDK agrega, para que el change set
  se pueda describir en una cuenta no bootstrapeada. El archivo original no se
  modifica.

```bash
cd infra/cdk && python app.py   # genera el assembly
python3 tools/e3_078_change_set_preview.py \
  cdk.out/EnergySharkDev-ExistingInfrastructure.template.json \
  --stack-name e3078-preview-existing --region us-east-1 --output markdown
```

Síntesis local de los templates (`aws-cdk-lib==2.278.0`):

| Stack | Recursos | Bytes del template | Vía |
| --- | --- | --- | --- |
| `EnergySharkDev-ExistingInfrastructure` | 96 | 52781 → 37392 sin guardia | `--template-body` |
| `EnergySharkDev-Sagas` | 67 | 79060 → 59604 sin guardia | requiere `--upload-bucket` o `--template-url` |
| `EnergySharkDev-Monitoring` | 7 | 9736 | `--template-body` |

La validación del template con `aws cloudformation validate-template` requiere
credenciales y no se ejecutó en esta corrida.

## E3-080 — ejecuciones abiertas después de `closesAt + 60 s`

Criterio del ADR-007: no quedan ejecuciones abiertas 60 segundos después de
`closesAt`. La herramienta clasifica cada saga asociada a un ciclo cerrado:

- `open_after_limit`: sigue en estado no terminal y ya pasó `closesAt + 60 s`.
- `settled_late`: terminó, pero `terminal_at` cae después de ese plazo.
- `terminal_without_timestamp`: estado terminal sin `terminal_at`; el plazo no es
  demostrable y se registra como desviación.

Cada desviación incluye `cycle_id`, `idpk`, `role`, estado, atraso en segundos y
causa, que es lo que pide la tarjeta.

```bash
python3 tools/e3_080_saga_timing.py --db-url "$DATABASE_URL" --output markdown
```

Un ciclo cerrado sin sagas asociadas se informa aparte y el resultado es `FAIL`:
cero desviaciones sin datos no demuestra el criterio.

## E3-081 — reservas retenidas y objetivos del ADR-007

Mide los tres objetivos de aceptación del ADR-007:

| Objetivo | Meta |
| --- | --- |
| Ejecuciones abiertas después de `closesAt + 60 s` | 0 |
| Reservas `HOLD` retenidas al cierre o con saga terminal | 0 |
| Inicios duplicados de saga por el mismo `idpk` | 0 |

```bash
python3 tools/e3_081_reservas.py --db-url "$DATABASE_URL" --output markdown
```

Una reserva `HOLD` cuenta como retenida si su oferta pertenece a un ciclo cerrado
y no tiene `released_at`, y además su saga está en estado terminal o superó el
plazo. El inicio duplicado se mide agrupando `idpk`; la restricción
`uq_saga_executions_idpk` ya lo impide en la base, por lo que el valor esperado
es 0 y cualquier otro valor señala datos migrados o un esquema alterado.

## Corrida de humo con PostgreSQL efímero

Esquema creado con las migraciones Alembic del repositorio sobre `postgres:16-alpine`
con `tmpfs`, sin volúmenes ni puertos públicos. Datos sintéticos: un ciclo limpio y
uno con dos desviaciones.

Ciclo limpio:

```text
Sagas medidas: 1 · abiertas fuera de plazo: 0 · tarde: 0 · PASS
Ciclos cerrados: 1 · sagas: 1 · reservas revisadas: 0 · PASS
```

Ciclo con desviaciones:

```text
settled_late     c-bad  bid-bad   SELLER  SETTLED  35.0
open_after_limit c-bad  bid-open  SELLER  WAITING  61.6
Sagas medidas: 3 · abiertas fuera de plazo: 1 · tarde: 1 · FAIL

Reservas HOLD retenidas al cierre | 0 | 2 | no
Ejecuciones abiertas después de closesAt + 60 s | 0 | 2 | no
```

Base vacía:

```text
Ciclos cerrados: 0 · sagas: 0 · reservas revisadas: 0 · FAIL
```

Los tres casos devuelven el código de salida esperado: 0 cuando cumple, 1 cuando
no cumple o no hay datos.

## Pruebas

```bash
python3 -m unittest discover -s tools/tests -p "test_*.py"
```

55 pruebas, incluidas las de estas tres herramientas. Las de E3-078 verifican
que no se ejecuta el change set, que no se borra un stack preexistente y que el
guardia de bootstrap se quita solo del template enviado.

## Pendientes declarados

- Correr la vista previa real de E3-078 con credenciales y adjuntar la salida.
- Ejecutar E3-080 y E3-081 con `DATABASE_URL` del entorno real y adjuntar los
  resultados; E3-080 depende además de que existan sagas producidas por E3-058 y
  E3-066.
- Actualizar el ADR-007 con los valores medidos (E3-084).
