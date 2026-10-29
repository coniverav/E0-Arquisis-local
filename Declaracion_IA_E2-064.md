## Declaración de uso de IA — E2-064

Para el desarrollo de la tarjeta **E2-064: Política de selección por precio efectivo y rutas**, se utilizó ChatGPT como herramienta de apoyo mediante consultas por chat.

La herramienta se utilizó para integrar las políticas de compra **CHEAPEST_EFFECTIVE_PRICE** (menor precio efectivo, incluyendo transporte) y **MIN_DISTANCE** (menor distancia), respetar el tiempo de espera `span` y los plazos oficiales del mercado, comprobar la disponibilidad de ofertas y la capacidad de Wallet, y eliminar el límite artificial de precio de E1.

ChatGPT proporcionó propuestas de código, scripts y tests, los cuales fueron revisados para su integración al proyecto.

### Prompts relevantes utilizados

1. "¿Me ayudas a implementar E2-064 con selección por precio efectivo y rutas, respetando AD6 y el span, sin el límite de precio de E1, incluyendo scripts y pruebas?"
