import os

# No usamos python-dotenv dentro del container: Docker Compose inyecta las variables.
DATABASE_URL = os.environ["DATABASE_URL"]
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "master")

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