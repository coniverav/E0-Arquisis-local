## AD2: Persistencia del ledger 

### Contexto

El sistema debe asegurar una trazabilidad y consistencia de los datos para cada ciclo, posibilitando la explicación y reconstrucción de estados históricos mediante la información almacenada (RF01). La solución que se adopte deberá buscar un equilibrio entre el rendimiento de las lecturas y la claridad para revisar y entender cómo se llegó a cada resultado, definiendo un modelo de persistencia que mantenga coherencia entre las operaciones realizadas y los estados resultantes. 

### Alternativas evaluadas

1. **Almacenamiento exclusivo del estado final por ciclo:** Registrar únicamente el balance energético y presupuesto definitivos al finalizar cada ciclo, considerando las transacciones en el tiempo de ejecución pero guardando sólo los montos de cierre.  
   1. Ventajas:  
      1. Modelo sencillo y requiere pocas estructuras adicionales.  
      2. Consultas rápidas del estado actual y final.  
      3. Es fácil generar el negotiation-report a partir del estado almacenado.  
   2. Desventajas:  
      1. Permite conocer el resultado final, pero no explicar cómo se obtuvo.  
      2. Una corrupción o error en el valor acumulado es difícil de diagnosticar, ya que no permite reconstruir estados intermedios.  
      3. Por sí sola no satisface la propiedad exigida por AD2.  
2. **Registro inmutable de operaciones:** Almacenar cada modificación al ledger como una nueva entrada inmutable, como una tabla *append-only* en la base de datos que no se modifique, de modo que se podría reconstruir el estado de cada ciclo reproduciendo en orden todas las operaciones que lo afectaron. Este enfoque se relaciona con el patrón Event Sourcing, donde los cambios se almacenan como una secuencia ordenada de eventos de solo anexión y el estado puede reconstruirse mediante su reproducción (Microsoft, s. f).   
1. Ventajas:  
   1. Mantiene trazabilidad completa y cada modificación podría relacionarse con su causa.  
   2. Permite reconstruir el estado desde la información persistida.  
   3. Facilita auditorías y diagnóstico de inconsistencias.  
   4. Las correcciones pueden representarse mediante nuevas entradas sin modificar el historial anterior.  
2. Desventajas:  
   1. Se necesita mantener un orden determinista de aplicación.  
   2. Consultar el estado actual puede requerir reproducir múltiples entradas, por lo que resultaría innecesariamente costoso si todas las consultas habituales requieren volver a calcular el estado.  
   3. La lógica de reconstrucción se vuelve parte importante del sistema.  
   4. El frontend y endpoints que solo necesitan el balance actual tendrían que depender de una reconstrucción o proyección adicional.

3. ### **Modelo que junte ambas opciones: registro inmutable y estado materializado por ciclo:** Mantener el registro inmutable de las operaciones del ledger, pero además conservar un estado materializado del ciclo, de modo que se pueda acceder rápidamente a los balances resultantes. Las vistas o estados materializados permiten mantener una representación derivada optimizada para consultas, evitando tener que reconstruir el estado desde todo el historial en cada lectura (Microsoft, s. f.-a; Microsoft, s. f.-b).  

   1. Ventajas:  
      1. Permite reconstruir y explicar el ledger, permitiendo identificar cada operación y su efecto.  
      2. Mantiene consultas simples del estado actual.  
      3. Facilita satisfacer RF01 (“vista de historial de ciclos”) sin reproducir todo el historial en cada consulta.  
      4. Permite comparar el estado reconstruido con el estado materializado, permitiendo validar que el estado guardado refleja fielmente el historial de operaciones.   
      5. Se integra naturalmente con la idempotencia de AD3.  
   2. Desventajas:  
      1. Mantiene información derivable en más de una representación.  
      2. Las entradas históricas y el estado materializado deben mantenerse siempre consistentes.  
      3. Las modificaciones requieren transacciones.  
      4. Se necesita definir claramente el orden de aplicación de las operaciones.  
      5. Tiene mayor complejidad que almacenar solamente el balance.

### Decisión

Se optará por utilizar la estrategia de registro inmutable junto al estado materializado de cada ciclo (alternativa 3). Es decir, se implementará un modelo híbrido sobre PostgreSQL, compuesto por un registro *append-only* de las modificaciones del ledger y un estado de los resultados acumulados para cada ciclo.

El registro de operaciones será la evidencia histórica utilizada para reconstruir y explicar el ledger. El estado materializado será una representación derivada utilizada para las consultas habituales de la API y para obtener los balances finales del ciclo.

### *Tradeoff*

El principal *tradeoff* de esta alternativa sería la redundancia y complejidad del modelo a cambio de simplificar y acelerar las consultas, ya que esta decisión implica mantener dos representaciones del mismo conjunto de datos (el registro histórico y el estado materializado). Sin embargo, esta estructura permitiría garantizar la consistencia mediante transacciones, facilitaría la trazabilidad y la reconstrucción de ciclos anteriores, y limitaría el enfoque *append‑only* al ledger para reducir el impacto sobre el resto del sistema.

### Bibliografía

Microsoft. (s. f.-a). *Patrón Event Sourcing*. Microsoft Learn. Recuperado de [https://learn.microsoft.com/es-es/azure/architecture/patterns/event-sourcing](https://learn.microsoft.com/es-es/azure/architecture/patterns/event-sourcing)

Microsoft. (s. f.-b). *Patrón de vista materializada*. Microsoft Learn. Recuperado de [https://learn.microsoft.com/es-es/azure/architecture/patterns/materialized-view](https://learn.microsoft.com/es-es/azure/architecture/patterns/materialized-view)