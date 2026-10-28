"""E2-052: cierre de mercado y liquidación local antes del reporte.

La implementación vive en `cycle_accounting` junto a los efectos E2-051.
Este módulo expone el punto de entrada que usa el scheduler.
"""

from app.services.cycle_accounting import finalize_closed_markets

__all__ = ["finalize_closed_markets"]
