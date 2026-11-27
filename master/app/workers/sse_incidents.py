"""Import verified New Relic incident openings into the node's SSE queue."""
import argparse
import hashlib
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from ..database import engine

_QUERY = """query($account: Int!, $query: Nrql!) {
  actor { account(id: $account) { nrql(query: $query) { results } } }
}"""


def import_incidents(account, conditions, since_ms, until_ms, client):
    """Read one bounded window. Overlap/re-run windows safely on any replica."""
    if not (account > 0 and 0 < since_ms < until_ms and conditions
            and all(type(k) is int and k > 0 for k in conditions)
            and set(conditions.values()) <= {'wallet-slow', 'consumer-lag'}):
        raise ValueError('Invalid account, conditions or observation window')
    ids = ','.join(str(k) for k in conditions)
    query = (
        "SELECT timestamp, incidentId, conditionId FROM NrAiIncident "
        f"WHERE event = 'open' AND numeric(conditionId) IN ({ids}) "
        f"SINCE {since_ms} UNTIL {until_ms} LIMIT 1000"
    )
    response = client.post('https://api.newrelic.com/graphql', json={
        'query': _QUERY, 'variables': {'account': account, 'query': query},
    })
    response.raise_for_status()
    payload = response.json()
    if payload.get('errors'):
        raise RuntimeError('Incident query rejected')
    rows = payload['data']['actor']['account']['nrql']['results']
    if not isinstance(rows, list) or len(rows) >= 1000:
        raise ValueError('Incomplete incident window: retry with smaller intervals')
    count = 0
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        # Deterministic acquisition order for overlapping observation windows.
        ordered = sorted(rows, key=lambda r: (r['timestamp'], int(r['conditionId']), str(r['incidentId'])))
        for row in ordered:
            timestamp = row['timestamp']
            incident = row['incidentId']
            if not isinstance(incident, (str, int)) or not str(incident) or not since_ms <= timestamp < until_ms:
                raise ValueError('Invalid incident evidence')
            condition = int(row['conditionId'])
            if condition not in conditions:
                raise ValueError('Incident from unconfigured condition')
            family = conditions[condition]
            key = hashlib.sha256(f'{account}/{condition}/{incident}'.encode()).hexdigest()
            result = conn.execute(text("""
                WITH opening AS (
                    INSERT INTO sse_alert_observations(family, observation_key)
                    VALUES (:family, :key) ON CONFLICT DO NOTHING RETURNING id
                )
                INSERT INTO sse_feed_pending
                    (event_type, source, source_id, message_type, status, occurred_at)
                SELECT 'alert','MONITORING',id,:family,'OPEN',:at FROM opening
            """), {'family': family, 'key': key, 'at': datetime.fromtimestamp(timestamp / 1000, timezone.utc)})
            count += result.rowcount
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', type=int, required=True)
    parser.add_argument('--wallet-condition', type=int)
    parser.add_argument('--consumer-condition', type=int)
    now = int(time.time() * 1000)
    parser.add_argument('--since-ms', type=int, default=now - 3600000)
    parser.add_argument('--until-ms', type=int, default=now)
    args = parser.parse_args()
    if args.wallet_condition == args.consumer_condition:
        parser.error('Supply a distinct condition for each enabled family')
    conditions = {k: v for k, v in (
        (args.wallet_condition, 'wallet-slow'), (args.consumer_condition, 'consumer-lag')
    ) if k is not None}
    with httpx.Client(headers={'API-Key': os.environ['NEW_RELIC_USER_API_KEY']}, timeout=20) as client:
        count = import_incidents(args.account, conditions, args.since_ms, args.until_ms, client)
    print(f'Published candidates: {count}')


if __name__ == '__main__':
    main()
