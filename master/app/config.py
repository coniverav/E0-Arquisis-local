import os

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

AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE")
AUTH_ISSUER = os.getenv("AUTH_ISSUER")

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173",
    ).split(",")
    if origin.strip()
]
