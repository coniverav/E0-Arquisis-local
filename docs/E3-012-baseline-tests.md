# Baseline local de pruebas E2

Esta línea base registra la ejecución local aprobada antes de iniciar cambios de E3. El backend se evaluó sobre `develop` en el commit `22e9edd`.

## Resultados

| Componente | Resultado | Referencia |
| --- | --- | --- |
| Contratos | 15/15 aprobados | `bbd8eae` |
| Frontend | 30/30, lint y build OK | `e9b7424` |
| Backend | 525/525 + 3 subtests en 316.63 s | `22e9edd` |
| Connector | 74/74 + 12 subtests en 2.49 s | `22e9edd` |
| **Total** | **644 tests + 15 subtests aprobados** | — |

## Comandos esenciales

```bash
# Contracts
python -m unittest discover -s tests -v
# Frontend
npm test && npm run lint && npm run build
# Backend, desde master y con DATABASE_URL apuntando al PostgreSQL temporal
.venv/bin/python -m pytest -q
# Connector, desde connector
.venv/bin/python -m pytest -q
```

El backend usó un PostgreSQL temporal aislado, sin tocar la base local existente. Las pruebas del Connector emplearon RabbitMQ ficticio y no se conectaron al broker real. Vite emitió una advertencia no bloqueante, pero el build finalizó correctamente.

Estos resultados son evidencia local reproducible; no demuestran el estado de producción ni implican acceso a servicios productivos. No se registraron credenciales ni datos sensibles.
