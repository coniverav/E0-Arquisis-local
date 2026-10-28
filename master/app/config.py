import os
from urllib.parse import urlsplit

# No usamos python-dotenv dentro del container: Docker Compose inyecta las variables.
DATABASE_URL = os.environ["DATABASE_URL"]
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "master")

CITY_ID = os.getenv(
    "CITY_ID",
    "KLD",
)

RABBITMQ_CENTRAL_ROUTING_KEY = os.getenv(
    "RABBITMQ_CENTRAL_ROUTING_KEY",
    "central",
)

CYCLE_REPORT_WINDOW_SECONDS = int(
    os.getenv(
        "CYCLE_REPORT_WINDOW_SECONDS",
        "300",
    )
)

CYCLE_SCHEDULER_POLL_SECONDS = float(
    os.getenv(
        "CYCLE_SCHEDULER_POLL_SECONDS",
        "5",
    )
)

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173",
    ).split(",")
    if origin.strip()
]

AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE")
AUTH_ISSUER = os.getenv("AUTH_ISSUER")

# Configuración Wallet Central v3 (E2-019)
WALLET_BASE_URL = os.getenv("WALLET_BASE_URL", "http://wallet-mock:8080").rstrip("/")
WALLET_API_KEY = os.getenv("WALLET_API_KEY", "")
WALLET_TIMEOUT_SECONDS = float(os.getenv("WALLET_TIMEOUT_SECONDS", "10.0"))

# E2-040: los endpoints REST pueden vivir bajo /v1, pero JWKS se publica en
# la raíz del origen. Si Compose transmite una variable vacía, usar el fallback.
_wallet_origin = urlsplit(WALLET_BASE_URL)
WALLET_JWKS_URL = os.getenv("WALLET_JWKS_URL") or (
    f"{_wallet_origin.scheme}://{_wallet_origin.netloc}/.well-known/jwks.json"
)
WALLET_ISSUER = os.getenv("WALLET_ISSUER", "wallet.energyshark")

# E2-044: intervalo mínimo entre conciliaciones automáticas con /balance y
# /statement. El scheduler puede llamar al tick en cada vuelta sin saturar la
# wallet.
WALLET_RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("WALLET_RECONCILE_INTERVAL_SECONDS", "60")
)

# E2-055: margen de gracia tras `market.lastBidAt` antes de expirar un bid
# saliente que nunca recibió respuesta.
SWAP_TIMEOUT_GRACE_SECONDS = float(
    os.getenv("SWAP_TIMEOUT_GRACE_SECONDS", "15")
)

# E2-054: cadencia mínima del disparador autónomo de compra. La consulta a
# wallet se amortiza entre vueltas del scheduler.
AUTONOMOUS_BUY_INTERVAL_SECONDS = float(
    os.getenv("AUTONOMOUS_BUY_INTERVAL_SECONDS", "45")
)

