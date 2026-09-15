# Objetivo

La E1 busca extender el nodo energético construido en la E0 para permitir que cada ciudad mantenga su propio ledger de energía y presupuesto, interactúe con la central mediante RabbitMQ y ejecute de forma autónoma los ciclos de negociación definidos por el protocolo. El sistema deberá mantener un historial persistente y que sea consultable, tolerar reintentos y los distintos escenarios de falla definidos para la entrega , exponer una API backend y una SPA frontend independientes, y operar completamente desplegado en AWS.

# Alcance

* Consumo y publicación de mensajes mediante RabbitMQ.  
* Implementación del protocolo de mensajes de la E1.  
* Manejo de ciclos de energía y negociación.  
* Ledger local de presupuesto y balance energético.  
* Aplicación de status-statement, transfer y demand-statement.  
* Negociaciones voluntarias mediante negotiation-proposal, give, take y transfer.  
* Manejo de timeouts y reintentos.  
* Persistencia e idempotencia de operaciones.  
* Persistencia y visualización de distance-table.  
* Historial de ciclos y operaciones.  
* Interfaz administrativa para negociaciones voluntarias.  
* Registro de mensajes duplicados, descartados y respondidos con NACK.  
* Autenticación y autorización.  
* Despliegue del backend y frontend en AWS.  
* Monitoreo de aplicación e infraestructura.

# Fuera de alcance

* No se implementará transmisión directa de energía entre ciudades. La distance-table solamente debe recibirse, persistirse y visualizarse.  
* El ledger continúa siendo responsabilidad del nodo de cada ciudad, no se implementará un servicio centralizado de presupuesto.  
* demand-statement corresponde al protocolo de E1 y no debe confundirse con demand-set de la E0. 

# Requisitos funcionales 

* El nodo deberá consumir su cola city.{cityId} y responder a los mensajes siguiendo las reglas de ACK, NACK y error definidas por el protocolo.  
* El ciclo básico deberá operar de forma autónoma, procesando el status-statement y enviando el negotiation-report dentro de la ventana correspondiente.  
* Para cada ciclo deberá mantenerse un historial que permita consultar el status-statement, fondos recibidos, demand-statement aplicados, negociaciones realizadas, reporte enviado, balances finales y última operación aplicada.  
* La distance-table vigente deberá persistirse, actualizarse cuando cambie y estar disponible para visualización.  
* El sistema deberá ejecutar el flujo completo de negociación voluntaria, el cual incluye negotiation-proposal, give/take y pago, incluyendo los timeouts de 30 segundos y los reintentos correspondientes.  
* La interfaz administrativa deberá permitir crear negociaciones voluntarias y consultar su progreso y estado final.  
* Los mensajes duplicados, descartados y respondidos con NACK deberán quedar registrados y disponibles para consulta. Los duplicados no deberán volver a modificar el ledger.

# Reglas principales del protocolo 

Todos los mensajes deberán seguir el envelope de la E1, incluyendo al menos idpk, msgId, type y timestamp. Los mensajes enviados por la ciudad deberán incluir además su cityId y la propiedad AMQP user\_id correspondiente. Es importante mencionar que msgId identifica un mensaje particular, mientras que idpk actúa como llave de idempotencia para una operación y debe ser distinto de msgId. Los reintentos de una misma operación deberán conservar su idpk.

El manejo de mensajes inválidos seguirá estas reglas:

* Mensaje no parseable o sin msgId, se descarta y registra.  
* Mensaje parseable pero inválido, se responde con NACK.  
* Mensaje válido cuya operación no puede ejecutarse, se responde con error.  
* Un ACK confirma la recepción del mensaje, no la aceptación de la operación.

# Ciclos y ledger

Cada ciclo tiene una duración de 2 horas. Durante los últimos 20 minutos se realiza la negociación del ciclo siguiente y, durante los últimos 5 minutos de dicha ventana, debe enviarse el negotiation-report. Cada nodo será la fuente de verdad de su propio ledger, que se compone por presupuesto y balance energético. El presupuesto sobrante se mantiene entre ciclos. La energía, en cambio, no se almacena y el excedente se pierde al finalizar el ciclo. Los demand-statement deberán aplicarse inmediatamente al ledger según su convención de signos y su efecto deberá quedar reflejado en el negotiation-report

# Negociaciones y reintentos 

Las negociaciones voluntarias deberán permitir operaciones give y take. Las confirmaciones de la central deben ser consideradas dentro de una ventana de 30 segundos. Cuando la ciudad deba recibir un pago mediante transfer, esperará hasta 30 segundos después de la confirmación, si este no llega, deberá reintentar la operación conservando el mismo idpk. Una operación reintentada o duplicada nunca deberá producir dos veces el mismo efecto sobre el ledger.

# Requisitos de infraestructura 

* Backend y frontend mediante HTTPS.  
* Backend y frontend separados en repositorios independientes.  
* API detrás de AWS API Gateway.  
* Servicio de autenticación/autorización.  
* SPA React o Vue desplegada mediante S3 y CloudFront.  
* Backend desplegado mediante ECR sobre EC2.  
* Monitoreo de aplicación e infraestructura.  
* AWS Budget Alerts configuradas.