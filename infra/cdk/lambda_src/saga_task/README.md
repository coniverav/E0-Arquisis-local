# Artefacto de tareas de saga

CDK empaqueta este directorio por hash de contenido para todas las tareas. El
handler actual falla de forma explícita y segura: E3-020 declara funciones,
configuración e IAM, mientras los adaptadores Wallet y la persistencia previa
al HTTP corresponden a E3-022 y E3-023. Nunca registra el evento completo ni
task tokens, JWT, `swapToken` o valores recuperados desde Secrets Manager.
