#!/usr/bin/env python3
"""E2-061: provisiona el panel y las alertas New Relic via NerdGraph.

Solo usa metadatos de E2-027. Nunca lee API keys de wallet/broker.
Con --dry-run (por defecto) no hace peticiones HTTP.
"""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DASHBOARD_NAME = "EnergyShark E2 | Wallet y broker"
POLICY_NAME = "EnergyShark E2 | Anomalias wallet y broker"


class EnumValue(str):
    """Enum GraphQL, no string quoted."""


def graphql_value(value):
    if isinstance(value, EnumValue):
        return value
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(graphql_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{key}: {graphql_value(item)}" for key, item in value.items()
        ) + "}"
    raise TypeError(f"Tipo no soportado: {type(value).__name__}")


def widget(title, query, chart_type, *, row, column, width=6, height=3, account_id):
    return {
        "title": title,
        "visualization": {"id": "viz." + chart_type},
        "layout": {"row": row, "column": column, "width": width, "height": height},
        "rawConfiguration": {"nrqlQueries": [{"accountIds": [account_id], "query": query}]},
    }


def dashboard_definition(account_id):
    # Sin SINCE fijo: el selector temporal de New Relic controla todos los widgets.
    rows = [
        ("Errores wallet (timeout / 429 / 5xx)",
         "SELECT count(*) FROM EnergySharkWalletHttp WHERE outcome IN ('timeout', 'transport_error', 'http_429', 'http_502', 'http_503', 'http_504') FACET outcome TIMESERIES", "line"),
        ("Wallet: 429 por endpoint", "SELECT count(*) FROM EnergySharkWalletHttp WHERE statusCode = 429 FACET endpoint TIMESERIES", "line"),
        ("Latencia wallet p50 y p95 (ms)", "SELECT percentile(latencyMs, 50, 95) FROM EnergySharkWalletHttp FACET endpoint TIMESERIES", "line"),
        ("Retry-After en 429 (segundos)", "SELECT max(retryAfterSeconds) FROM EnergySharkWalletHttp WHERE statusCode = 429 FACET endpoint TIMESERIES", "line"),
        ("Reconexiones de broker (city / market)", "SELECT count(*) FROM EnergySharkBrokerConnection WHERE state = 'retrying' FACET consumer TIMESERIES", "line"),
        ("Feed market: entregas consumidas", "SELECT count(*) FROM EnergySharkBrokerDelivery WHERE consumer = 'market' FACET outcome TIMESERIES", "line"),
        ("Heartbeat del consumidor market", "SELECT count(*) FROM EnergySharkBrokerHeartbeat WHERE consumer = 'market' TIMESERIES", "line"),
        ("Reencolados market por fallos", "SELECT count(*) FROM EnergySharkBrokerDelivery WHERE consumer = 'market' AND outcome = 'broker_reject' AND requeue = true TIMESERIES", "line"),
    ]
    widgets = [
        widget(title, query, kind, row=1 + (index // 2) * 3,
               column=1 if index % 2 == 0 else 7,
               account_id=account_id)
        for index, (title, query, kind) in enumerate(rows)
    ]
    return {
        "name": DASHBOARD_NAME,
        "description": "E2-061 · Wallet HTTP y consumidores RabbitMQ (E2-027). Sin secretos ni cuerpos de mensajes.",
        "permissions": EnumValue("PUBLIC_READ_ONLY"),
        "pages": [{"name": "Wallet y broker", "widgets": widgets}],
    }


def condition_definitions():
    # 60 s de agregación; se aplica a eventos reales, no a silencios del mercado.
    base = {
        "enabled": True,
        "signal": {"aggregationWindow": 60, "aggregationMethod": EnumValue("EVENT_TIMER"), "aggregationTimer": 60},
        "expiration": {"expirationDuration": 300, "openViolationOnExpiration": False,
                       "closeViolationsOnExpiration": True},
        "terms": {
            "threshold": 0, "thresholdOccurrences": EnumValue("AT_LEAST_ONCE"),
            "thresholdDuration": 60, "operator": EnumValue("ABOVE"),
            "priority": EnumValue("WARNING"),
        },
        "valueFunction": EnumValue("SINGLE_VALUE"),
        "violationTimeLimitSeconds": 3600,
    }
    rules = [
        ("E2-061 | Timeout wallet",
         "SELECT count(*) FROM EnergySharkWalletHttp WHERE outcome = 'timeout'"),
        ("E2-061 | Wallet rate limit 429",
         "SELECT count(*) FROM EnergySharkWalletHttp WHERE statusCode = 429"),
        ("E2-061 | Reconexiones broker",
         "SELECT count(*) FROM EnergySharkBrokerConnection WHERE state = 'retrying'"),
        ("E2-061 | Reencolados del market",
         "SELECT count(*) FROM EnergySharkBrokerDelivery WHERE consumer = 'market' AND outcome = 'broker_reject' AND requeue = true"),
    ]
    conditions = []
    for name, query in rules:
        conditions.append({**base, "name": name, "nrql": {"query": query}})

    # Esta condición monitorea la conexión y NO asume que siempre hay ofertas.
    # Solo abre una anomalía si se pierde una señal que previamente existía.
    conditions.append({
        **base,
        "name": "E2-061 | Sin heartbeat market 5 min",
        "nrql": {"query": "SELECT count(*) FROM EnergySharkBrokerHeartbeat WHERE consumer = 'market'"},
        "signal": {"aggregationWindow": 60, "aggregationMethod": EnumValue("EVENT_TIMER"), "aggregationTimer": 120},
        "terms": {
            "threshold": 0, "thresholdOccurrences": EnumValue("ALL"),
            "thresholdDuration": 300, "operator": EnumValue("BELOW"),
            "priority": EnumValue("CRITICAL"),
        },
        "expiration": {
            "expirationDuration": 300, "openViolationOnExpiration": True,
            "closeViolationsOnExpiration": False,
        },
    })
    return conditions


class NerdGraph:
    def __init__(self, api_key, endpoint="https://api.newrelic.com/graphql"):
        self.api_key = api_key
        self.endpoint = endpoint

    def execute(self, query):
        body = json.dumps({"query": query}).encode("utf-8")
        request = Request(self.endpoint, data=body, method="POST", headers={
            "API-Key": self.api_key,
            "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
        except (HTTPError, URLError) as exc:
            # No volcar headers ni cuerpo HTTP (pueden incluir datos sensibles).
            raise RuntimeError(f"NerdGraph no disponible: {type(exc).__name__}") from None
        if result.get("errors"):
            details = "; ".join(str(err.get("message", "error"))[:160] for err in result["errors"])
            raise RuntimeError(f"NerdGraph rechazó la operación: {details}")
        return result["data"]


def find_policy(client, account_id):
    # Búsqueda exacta. Si ya existe, la reutilizamos.
    query = (f'{{ actor {{ account(id: {account_id}) {{ alerts {{ '
             f'policiesSearch(searchCriteria: {{ name: {graphql_value(POLICY_NAME)} }}) '
             '{ policies { id name } } } } } }')
    policies = client.execute(query)["actor"]["account"]["alerts"]["policiesSearch"]["policies"]
    for policy in policies:
        if policy["name"] == POLICY_NAME:
            return int(policy["id"])
    return None


def ensure_policy(client, account_id):
    existing = find_policy(client, account_id)
    if existing is not None:
        return existing
    policy = {"name": POLICY_NAME, "incidentPreference": EnumValue("PER_CONDITION")}
    query = (f'mutation {{ alertsPolicyCreate(accountId: {account_id}, '
             f'policy: {graphql_value(policy)}) {{ id name }} }}')
    return int(client.execute(query)["alertsPolicyCreate"]["id"])


def existing_conditions(client, account_id, policy_id):
    names = set()
    cursor = None
    while True:
        cursor_arg = f', cursor: {graphql_value(cursor)}' if cursor else ''
        query = (f'{{ actor {{ account(id: {account_id}) {{ alerts {{ '
                 'nrqlConditionsSearch(searchCriteria: '
                 f'{{ policyId: {policy_id} }}{cursor_arg}) '
                 '{ nrqlConditions { name } nextCursor } } } } }')
        page = client.execute(query)["actor"]["account"]["alerts"]["nrqlConditionsSearch"]
        names.update(row["name"] for row in page["nrqlConditions"])
        cursor = page.get("nextCursor")
        if not cursor:
            return names


def provision_alerts(client, account_id):
    policy_id = ensure_policy(client, account_id)
    names = existing_conditions(client, account_id, policy_id)
    for condition in condition_definitions():
        if condition["name"] in names:
            print("Alerta existente:", condition["name"])
            continue
        query = (f'mutation {{ alertsNrqlConditionStaticCreate(accountId: {account_id}, '
                 f'policyId: {policy_id}, condition: {graphql_value(condition)}) '
                 '{ id name } }')
        result = client.execute(query)["alertsNrqlConditionStaticCreate"]
        print("Alerta creada:", result["name"], "id=", result["id"])
    return policy_id


def provision_dashboard(client, account_id, dashboard_guid=None):
    dashboard = graphql_value(dashboard_definition(account_id))
    if dashboard_guid:
        query = (f'mutation {{ dashboardUpdate(guid: {graphql_value(dashboard_guid)}, '
                 f'dashboard: {dashboard}) {{ entityResult {{ guid }} errors {{ description }} }} }}')
        result = client.execute(query)["dashboardUpdate"]
    else:
        query = (f'mutation {{ dashboardCreate(accountId: {account_id}, '
                 f'dashboard: {dashboard}) {{ entityResult {{ guid }} errors {{ description }} }} }}')
        result = client.execute(query)["dashboardCreate"]
    if result.get("errors"):
        raise RuntimeError("Dashboard rechazado: " + str(result["errors"]))
    guid = result["entityResult"]["guid"]
    print("Dashboard GUID:", guid)
    return guid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, default=int(os.getenv("NEW_RELIC_ACCOUNT_ID", "0")))
    parser.add_argument("--dashboard-guid", default=os.getenv("NEW_RELIC_DASHBOARD_GUID"))
    parser.add_argument("--apply", action="store_true", help="Crear/actualizar en New Relic")
    args = parser.parse_args(argv)
    if args.account_id <= 0:
        parser.error("Indique --account-id o NEW_RELIC_ACCOUNT_ID")
    if not args.apply:
        print(json.dumps({
            "dashboard": dashboard_definition(args.account_id),
            "alerts": condition_definitions(),
        }, ensure_ascii=False, indent=2))
        print("\nDry-run: sin solicitudes HTTP. Ejecute con --apply para publicar.")
        return
    key = os.getenv("NEW_RELIC_USER_API_KEY")
    if not key:
        parser.error("Para --apply, configure NEW_RELIC_USER_API_KEY (user key, no license key)")
    client = NerdGraph(key)
    policy_id = provision_alerts(client, args.account_id)
    guid = provision_dashboard(client, args.account_id, args.dashboard_guid)
    print(f"Política: {policy_id}. Guarde NEW_RELIC_DASHBOARD_GUID={guid} para reutilizar el panel.")
    print("Revise los canales de notificación en New Relic: crear alertas NO configura el destinatario.")


if __name__ == "__main__":
    main()
