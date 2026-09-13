## AD1: Topología del consumo 

### Contexto

El sistema debe consumir mensajes desde RabbitMQ sin acoplar la disponibilidad del broker con la disponibilidad de la API. La solución que se termine implementando debe permitir la reconexión automática y evitar la pérdida de mensajes que ya hayan sido recibidos.

### Alternativas evaluadas

1. **Consumidor integrado en el backend:** La idea es que el propio master mantenga la conexión con RabbitMQ.   
   1. Ventajas:  
      1. Menos servicios en el sistema  
      2. No existe comunicación HTTP interna  
   2. Desventajas:  
      1. Acopla RabbitMQ con la API, reduciendo la independencia de los dos componentes.  
      2. Si hubiesen múltiples instancias de master, se necesita coordinar a los consumidores  
      3. Mezcla responsabilidad de mensajería y API  
2. **Connector independiente comunicándose por HTTP:** Se mantiene la arquitectura actual, el connector consume mensajes y los envía al backend mediante HTTP.  
   1. Ventajas:  
      1. Mantiene la API separada e independiente del broker.  
      2. Reutiliza la arquitectura existente en el repositorio de la entrega 0\.  
      3. Implementación que resulta relativamente simple.  
   2. Desventajas:  
      1. Una falla entre el procesamiento y el ACK puede producir reentregas, ya que RabbitMQ utiliza acknowledgements como transferencia de responsabilidad y puede volver a entregar mensajes que no fueron confirmados ante una falla (RabbitMQ, s. f.).   
      2. Los mensajes pendientes de publicación pueden perderse si solo existen en la memoria y no poseen persistencia.  
      3. Requiere que se refuerce la persistencia para poder poseer la auditoría y recuperación  
3. **Connector independiente con persistencia durable de mensajes:** Se mantiene el connector, pero los mensajes recibidos persisten antes de considerarlos procesados. Esta estrategia permite que el sistema asuma responsabilidad durable sobre el mensaje antes de enviar el ACK al broker, comportamiento recomendado por RabbitMQ para evitar pérdidas ante fallos (RabbitMQ, s. f.).   
   1. Ventajas:  
      1. Los mensajes sobreviven reinicios.  
      2. Facilita reintentos y auditoría.  
      3. Mantiene RabbitMQ desacoplado de la API.  
      4. Funciona de forma correcta con varias instancias del backend.  
   2. Desventajas:  
      1. Requiere persistencia adicional, por ejemplo del inbox y outbox.  
      2. Aumenta la cantidad de estados en el sistema y la lógica de coordinación presente.

### Decisión

**Mantener un connector independiente con persistencia en PostgreSQL:** El connector seguirá siendo responsable de la comunicación con RabbitMQ, mientras que el backend será responsable de persistir y procesar los mensajes. Los mensajes entrantes serán persistidos antes de confirmar su recepción al broker, de modo que el sistema haya asumido durablemente responsabilidad sobre ellos antes del ACK (RabbitMQ, s. f.). Para aquellas operaciones que además deban producir mensajes salientes, se considerará persistir la intención de publicación siguiendo una estrategia de tipo Transactional Outbox (Amazon Web Services, s. f.). 

### *Tradeoff*

Esta decisión está basada más en la necesidad de resiliencia y la posibilidad de auditar por sobre la simplicidad de la misma. La alternativa HTTP directa podría ser más sencilla, pero la capa de persistencia adicional permite la recuperación de mensajes y publicaciones después de reinicios. A cambio, se deben mantener estructuras y estados adicionales.

### Bibliografía

RabbitMQ. (s. f.). *Reliability guide*. Recuperado de [https://www.rabbitmq.com/docs/reliability](https://www.rabbitmq.com/docs/reliability)

Amazon Web Services. (s. f.). *Transactional outbox pattern*. AWS Prescriptive Guidance. Recuperado de [https://docs.aws.amazon.com/en\_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)