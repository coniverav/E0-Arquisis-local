## AD3: Manejo de timeouts de negociación  

### Contexto

Las negociaciones son operaciones asíncronas con confirmaciones y pagos sujetos a plazos de 30 segundos. Cuando una operación deba reintentarse según las reglas del protocolo, deberá conservar el mismo \`idpk\`, aunque el nuevo mensaje utilice un msgId diferente. Para cumplir AD3 y RF05, el sistema debe impedir que una operación duplicada modifique nuevamente el ledger y debe registrar los mensajes repetidos. Como existen dos instancias de master, no resulta conveniente utilizar timers independientes, ya que ambas podrían procesar el mismo timeout o reintento.

### Alternativas evaluadas

1. **Mantener la solicitud HTTP esperando:** El endpoint que inicia una negociación mantendría abierta la request durante los 30 segundos mientras espera una confirmación.  
1. Ventajas:   
   1. Implementación sencilla.  
   2. No requiere infraestructura adicional.  
   3. El flujo completo podría verse de manera secuencial desde el código del endpoint.  
2. Desventajas:  
   1. Mantiene conexiones HTTP abiertas innecesariamente.   
   2. Acopla la duración de la request al protocolo de negociación.  
   3. El timeout desaparece si el proceso se reinicia.  
   4. Funciona mal con múltiples réplicas del backend.  
   5. Es difícil recuperar una negociación después de una caída.  
   6. No entrega por sí mismo un registro persistente del estado de la operación.  
2. **Timers o tareas async dentro de master:** Al crear una negociación se iniciaría una tarea asíncrona que esperaría el vencimiento de los 30 segundos. Al cumplirse el plazo, la tarea revisaría si llegó la respuesta correspondiente y, de no ser así, registraría el timeout o iniciaría el reintento.  
   1. Ventajas:  
      1. Implementación relativamente simple.  
      2. No requiere un nuevo servicio.  
      3. Consume pocos recursos.  
      4. Puede reaccionar con buena precisión mientras el proceso permanece vivo.  
   2. Desventajas:  
      1. Los timers se pierden al reiniciar el container.  
      2. Un deployment cancela las tareas pendientes.  
      3. Con dos instancias de master pueden crearse timers duplicados.  
      4. El estado relevante debe persistir de todas formas para reconstruir timers después de un reinicio.  
      5. La memoria de una instancia no puede actuar como fuente de verdad de una negociación distribuida.  
3. **Deadline persistido y worker independiente:** Cada operación o intento de negociación persistiría su estado y una fecha límite \`deadline\_at\` en PostgreSQL. Un servicio \`worker\` independiente consultaría periódicamente las operaciones pendientes y procesaría aquellas cuyo plazo hubiera vencido.  
   1. Ventajas:  
      1. Los deadlines sobreviven a reinicios.  
      2. La ejecución no depende del ciclo de vida de una instancia de la API.  
      3. Funciona correctamente con varias instancias de master.  
      4. Los estados pueden consultarse y auditarse.  
      5. Los retries pueden persistirse.   
      6. Permite recuperar automáticamente trabajo pendiente después de una caída.  
      7. PostgreSQL puede utilizarse también para coordinar concurrencia.  
   2. Desventajas:  
      1. Agrega un servicio adicional.  
      2. Requiere persistir estados y deadlines.  
      3. Requiere coordinación transaccional.  
      4. El worker puede detectar el vencimiento algunos segundos después de la hora exacta.  
      5. Se necesita implementar recuperación y reclamación segura de tareas.  
4. **Scheduler especializado mediante Celery/Redis u otra cola de tareas:** Los timeouts se programarían mediante un sistema especializado de background jobs.  
   1. Ventajas:  
      1. Herramientas maduras para programación de tareas.  
      2. Reintentos y scheduling incluidos.  
      3. Puede escalar fácilmente a múltiples workers.  
      4. Existen mecanismos específicos de observabilidad para jobs.  
   2. Desventajas:  
      1. Agrega nueva infraestructura.  
      2. Requiere operar un segundo sistema de colas además de RabbitMQ.  
      3. No elimina la necesidad de persistir el estado de negocio.  
      4. No resuelve automáticamente la idempotencia de la operación.  
      5. Incrementa la complejidad del deployment para un volumen bajo de negociaciones.

### Decisión

**Utilizar deadlines persistidos en PostgreSQL y un worker independiente**: PostgreSQL almacenará el estado, los intentos y el plazo de cada negociación, mientras que el worker procesará los vencimientos y reintentos. Cada reintento conservará el mismo idpk y utilizará un nuevo msgId. La aplicación de un efecto sobre el ledger y el registro que determina que dicho efecto ya fue procesado se realizarán dentro de una misma transacción, evitando estados parciales y reduciendo la posibilidad de que una operación duplicada vuelva a modificar el ledger (Microsoft, 2026a; PostgreSQL Global Development Group, s. f.-a). 

### Tradeoff

Esta decisión prioriza la recuperación, la idempotencia y la compatibilidad con múltiples instancias de master por sobre la simplicidad. Los timers en memoria serían más sencillos, pero se perderían ante reinicios y podrían ejecutarse de forma duplicada. A cambio, se incorpora un worker y estructuras adicionales para almacenar estados, intentos y fechas límite.

### Bibliografía

Microsoft. (2026). *Patrón de consumidor idempotente*. Microsoft Learn. Recuperado de [https://learn.microsoft.com/es-es/azure/architecture/patterns/idempotent-consumer](https://learn.microsoft.com/es-es/azure/architecture/patterns/idempotent-consumer)

PostgreSQL Global Development Group. (s. f.). *3.4. Transactions*. PostgreSQL Documentation. Recuperado de [https://www.postgresql.org/docs/16/tutorial-transactions.html](https://www.postgresql.org/docs/16/tutorial-transactions.html)