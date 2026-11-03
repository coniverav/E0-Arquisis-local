## AD6: Estrategia como configuración

### Contexto

En la E2, EnergyShark participa autónomamente en un mercado donde las ciudades publican ofertas, aceptan bids y compran energía mediante swaps. Su comportamiento comercial depende de parámetros como precio y cantidad ofrecida, política de aceptación, selección de ofertas y tiempo de espera de compra (`span`).

RF04 exige modificar estas políticas desde la interfaz **sin redeploy** y RF06 pide comparar resultados obtenidos con distintos valores de `span`. Cambiar una política durante el ciclo podría hacer que sus resultados mezclen estrategias. Como existen `master`, `master2` y un worker independiente, la configuración debe compartirse, persistir ante reinicios y dejar evidencia de la versión utilizada en cada ciclo.

**Escenario de atributo de calidad**

- **Fuente:** Integrantes del grupo durante la demo.
- **Estímulo:** Se modifica el `span`, la política de aceptación o el precio de oferta a mitad de un ciclo.
- **Ambiente:** Nodo operando en producción.
- **Artefacto:** Configuración de políticas de mercado y mecanismos que las ejecutan.
- **Respuesta:** Los cambios se guardan sin reinicio ni redeploy, se activan en el siguiente ciclo y cada ciclo conserva su estrategia efectiva.
- **Medida:** El experimento de RF06 puede ejecutarse cambiando la configuración y mostrando el `span` utilizado realmente en cada ciclo.

### Alternativas evaluadas

1. **Parámetros en variables de entorno:** Configurar el `span`, los precios y las políticas mediante `.env` o variables de contenedor.
   1. **Ventajas:**
      1. Implementación sencilla y compatible con la configuración actual.
      2. No exige tablas adicionales.
      3. Mantiene los parámetros separados de la lógica principal.
   2. **Desventajas:**
      1. Los cambios normalmente exigen reiniciar o recrear servicios.
      2. No permite edición directa desde el frontend.
      3. Dificulta sincronizar instancias y conservar el historial.
      4. No cumple adecuadamente AD6.

2. **Configuración modificable en memoria:** Exponer una API que actualice los parámetros guardados en el proceso.
   1. **Ventajas:**
      1. Permite cambios durante la ejecución sin editar archivos.
      2. Implementación relativamente simple.
      3. Evita consultas constantes a PostgreSQL.
   2. **Desventajas:**
      1. Los cambios se pierden al reiniciar el contenedor.
      2. `master`, `master2` y el scheduler pueden usar valores diferentes.
      3. No conserva versiones ni permite saber qué estrategia utilizó cada ciclo.

3. **Configuración centralizada sobrescrita en PostgreSQL:** Mantener un único registro editable que todos los servicios consultan.
   1. **Ventajas:**
      1. La configuración persiste y es compartida entre instancias.
      2. No agrega infraestructura externa.
      3. Permite administración desde la interfaz sin redeploy.
   2. **Desventajas:**
      1. Un cambio podría afectar un ciclo en ejecución.
      2. Sobrescribir valores dificulta reconstruir versiones anteriores.
      3. Diferentes procesos podrían leer parámetros distintos en el mismo ciclo.
      4. Dificulta atribuir correctamente resultados de RF06.

4. **Configuración versionada en PostgreSQL con activación por ciclo:** Cada modificación crea una versión persistida y pendiente, asociada al siguiente ciclo cuando comienza.
   1. **Ventajas:**
      1. Permite cambios sin reiniciar ni redesplegar servicios.
      2. Comparte parámetros entre instancias y sobrevive a reinicios.
      3. Mantiene estable la estrategia durante cada ciclo.
      4. Conserva versiones históricas y el `span` realmente utilizado.
      5. Facilita experimentar y comparar resultados de RF06.
      6. Reutiliza PostgreSQL como almacenamiento y coordinación.
   2. **Desventajas:**
      1. Requiere tablas, versiones y estados activo/pendiente.
      2. Debe asociar transaccionalmente cada ciclo a una versión.
      3. Los cambios no afectan de inmediato el ciclo actual.
      4. Exige manejar concurrencia entre ediciones y aperturas de ciclo.

5. **Servicio externo de configuración:** Administrar las políticas mediante una herramienta especializada de distribución y versionado.
   1. **Ventajas:**
      1. Centraliza la configuración de múltiples instancias.
      2. Puede ofrecer versionamiento, recuperación y extensibilidad.
   2. **Desventajas:**
      1. Introduce dependencia, integración y posibles costos adicionales.
      2. Aumenta la complejidad operacional del despliegue.
      3. Sigue siendo necesario registrar qué versión utilizó cada ciclo.
      4. Es excesivo para el alcance del proyecto.

### Decisión

**Utilizar estrategias versionadas y persistidas en PostgreSQL, con activación al comienzo de cada ciclo** (alternativa 4). El backend registrará versiones de configuración con los parámetros de precio y cantidad de oferta, política de aceptación, política de selección de compras y `span`.

El frontend permitirá consultar y editar los parámetros mediante una API protegida. Cada edición creará una nueva versión sin sobrescribir las anteriores. Al recibir el `status-statement` de un nuevo ciclo, el backend seleccionará transaccionalmente la versión que corresponde y la asociará al ciclo, evitando diferencias por actualizaciones concurrentes.

Las decisiones de venta y compra ejecutadas durante ese ciclo utilizarán siempre la versión asociada. Una modificación realizada con el mercado abierto quedará pendiente hasta el siguiente ciclo; no requerirá reinicio ni redeploy.

Para RF06 se conservarán, junto a la estrategia y el `span` efectivo, el déficit cubierto por el mercado, el costo efectivo frente al *baseline* y los bids rechazados. Así podrán compararse resultados y asociarse a las políticas que realmente se utilizaron (Microsoft, 2026).

### *Tradeoff*

La decisión privilegia trazabilidad, consistencia entre procesos y capacidad de experimentación por sobre la simplicidad de una configuración única. Las variables de entorno o los parámetros en memoria serían más fáciles de administrar inicialmente, pero exigirían reinicios o dificultarían la sincronización y reconstrucción histórica.

A cambio, deben mantenerse versiones, configuraciones pendientes y asociaciones persistentes por ciclo. Se acepta que los cambios no se apliquen inmediatamente: esa espera evita mezclar políticas dentro de un mismo ciclo y permite interpretar mejor el experimento de RF06.

### Bibliografía

Microsoft. (2026). *External Configuration Store Pattern*. Microsoft Learn. Recuperado de https://learn.microsoft.com/azure/architecture/patterns/edge-workload-configuration

Fowler, M. (2017). *Feature Toggles (aka Feature Flags)*. MartinFowler.com. Recuperado de https://martinfowler.com/articles/feature-toggles.html

PostgreSQL Global Development Group. (s. f.). *3.4. Transactions*. PostgreSQL Documentation. Recuperado de https://www.postgresql.org/docs/16/tutorial-transactions.html

Pontificia Universidad Católica de Chile. (2026). *2026-2 / IIC2173 - E2 | EnergyShark*. Enunciado del curso.

---

### Revisión retrospectiva E2-081 — 3 de noviembre de 2026

Se conserva la decisión original como registro de diseño. Esta sección contrasta su alternativa 4 con las rutas y pruebas que aparecen en el repositorio recibido.

**Mecanismo implementado.** [`strategy_config.py`](../../master/app/services/strategy_config.py) almacena versiones `PENDING` de los parámetros de compra y venta en PostgreSQL. La edición y la apertura del ciclo utilizan un bloqueo compartido, comprueban versiones y asocian una estrategia a `Cycle.strategy_version` según `market.opensAt` oficial; un `status-statement` tardío no reasigna la política de un ciclo anterior. [`market_api.py`](../../master/app/routers/market_api.py) expone `GET /market/strategy`, `GET /market/strategies`, `GET /market/strategy/options`, `POST /market/strategy` y `GET /market/strategy/audit`; la escritura pide JWT con `sub`, admite `expectedVersion` y devuelve `409` ante una edición obsoleta. Los campos operacionales de las versiones históricas pueden cambiar de estado, mientras sus parámetros comerciales se conservan. La migración [`e2073_strategy_direct_offer.py`](../../master/alembic/versions/e2073_strategy_direct_offer.py) incorpora precio directo y tope de cantidad, junto con los controles documentados en [`E2-062`](../api/E2-062-strategy-edit-audit.md).

Los servicios de ofertas, selección y compras consultan la configuración fijada al ciclo; [`cycle_metrics.py`](../../master/app/services/cycle_metrics.py) registra `strategy_version`, `span_seconds` y resultados necesarios para RF06.

**Evidencia de pruebas.** [`test_strategy_config_ad6.py`](../../master/tests/test_strategy_config_ad6.py) verifica vigencia en el ciclo siguiente y llegada tardía de `status-statement`; [`test_e2062_strategy_edit.py`](../../master/tests/test_e2062_strategy_edit.py) contempla edición concurrente, error `409`, auditoría y preservación de la estrategia del ciclo abierto. [`test_e2065_cycle_metrics.py`](../../master/tests/test_e2065_cycle_metrics.py) comprueba cálculo y persistencia de métricas con datos controlados. Estos son casos presentes en el código; el ZIP no contiene una salida de pytest que permita atribuirles una ejecución aprobada.

**Tradeoff observado.** Persistir una versión pendiente y fijarla por ciclo obliga a coordinar las ediciones con la recepción del `status-statement`, pero permite explicar por qué un cambio no afectó el mercado en curso. `expectedVersion` añade control frente a ediciones simultáneas, y la auditoría agrega trabajo de consulta sin una segunda fuente de configuración. El retraso de activación, aceptado en el ADR, es una consecuencia visible del diseño.

**Divergencias y límites.** La API de edición está implementada, pero **el ZIP no incluye un frontend** con el cual verificar visualmente RF04 sin redeploy. Existen el plan [`E2-066`](../qa/E2-066-plan-spans.md) y el protocolo de captura [`E2-067`](../qa/E2-067-ejecucion-registro.md); los directorios `datos_e2066` y `datos_e2067` no contienen capturas de ciclos reales en este paquete. Por ello, no se declara concluido el experimento RF06 ni comprobada la edición mediante UI. Se **mantiene** AD6 en el backend, con validación de interfaz y resultados reales por `span` pendientes.
