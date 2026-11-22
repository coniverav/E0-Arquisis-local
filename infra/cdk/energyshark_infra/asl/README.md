# Definiciones ASL de sagas

Estas definiciones describen máquinas AWS Step Functions **Standard**. CDK las carga, agrega el timeout total por ambiente, sustituye los ARN de las Lambdas y publica una versión detrás del alias `live`.

El proceso que llama `StartExecution` debe usar `$.idpk` como nombre. Las Lambdas reciben siempre las llaves de idempotencia desde el input y no deben reemplazarlas al reintentar.

Las tareas `waitForTaskToken` deben persistir el token antes de invocar Wallet. Un timeout conduce a reconciliación; solo un resultado autoritativo permite liquidar o compensar. Un resultado desconocido falla de forma visible y no libera reservas por suposición.

El `swapToken` permanece en PostgreSQL y las Lambdas lo recuperan mediante la saga o el `SwapRecord`; no se copia al input ni al historial de Step Functions.

Los valores `${...FunctionArn}` son sustituciones resueltas por CDK. No contienen cuentas, regiones, secretos ni ARN productivos. Los handlers declarados fallan de forma segura hasta que las tareas posteriores implementen los adaptadores Wallet y la persistencia token-antes-HTTP.
