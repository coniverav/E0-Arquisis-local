# Plan de milestones

### Milestone 0: Preparación y decisiones 06–08 Sep

* Preparación de repositorios y documentación.  
* Spec y milestones.  
* Decisiones de ADR.  
* Inicialización del frontend.  
* Configuración inicial de autenticación.  
* AWS Budget Alert.

### Milestone 1: Core de protocolo y ledger 09–15 Sep

* Adaptación al protocolo v2.  
* ACK, NACK y manejo de errores.  
* Reconexión al broker.  
* Modelo de datos y ledger.  
* Idempotencia.  
* OpenAPI inicial, mocks del frontend y login.

### Milestone 2: Ciclo autónomo y negociaciones 16–22 Sep

* Manejo automático de ventanas de ciclo.  
* negotiation-report.  
* negotiation-proposal.  
* Confirmaciones give y take.  
* Pagos.  
* Timeouts y reintentos.  
* Avance de API y frontend sobre contratos estables.

### Milestone 3: Cloud e integración 23–28 Sep

* ECR y EC2.  
* API Gateway y authorizer.  
* S3 y CloudFront.  
* Dominios y HTTPS.  
* New Relic.  
* Integración frontend–auth–API.

### Milestone 4: Pruebas y monitoreo 29 Sep–01 Oct

* Pruebas de duplicados.  
* Mensajes malformados.  
* Corte del broker.  
* Caída de containers.  
* Monitoreo.  
* Ejecución de varios ciclos.  
* Cierre de documentación y contratos.

### Milestone 5: Ensayo 02–03 Oct

* Ensayo de la demo.  
* Preparación de defensa individual.  
* Release candidata.  
* Revisión de health, accesos y documentación final.