import pytest

from sqlalchemy import inspect, text

from app.database import engine, run_migrations


@pytest.fixture(scope="session", autouse=True)
def preparar_base_de_pruebas():
    """Verifica que usamos una base de pruebas y aplica migraciones."""

    nombre_bd = engine.url.database or ""

    if not nombre_bd.startswith("energyshark_test_"):
        raise RuntimeError(
            f"No se permite limpiar una base que no sea de pruebas: {nombre_bd}"
        )

    run_migrations()


@pytest.fixture(autouse=True)
def aislar_datos_entre_tests(preparar_base_de_pruebas):
    """Limpia los datos sin eliminar el cursor de control SSE."""

    inspector = inspect(engine)

    tablas = [
        tabla
        for tabla in inspector.get_table_names(schema="public")
        if tabla not in ("alembic_version", "sse_feed_cursor")
    ]

    if tablas:
        quote = engine.dialect.identifier_preparer.quote

        nombres = ", ".join(
            f"public.{quote(tabla)}"
            for tabla in tablas
        )

        with engine.begin() as conexion:
            conexion.execute(
                text(
                    f"TRUNCATE TABLE {nombres} "
                    "RESTART IDENTITY CASCADE"
                )
            )

            # Restaurar cursor inicial para cada test.
            conexion.execute(
                text("""
                    INSERT INTO public.sse_feed_cursor
                        (singleton_id, last_id)
                    VALUES (1, 0)
                    ON CONFLICT (singleton_id)
                    DO UPDATE SET last_id = 0
                """)
            )

    yield