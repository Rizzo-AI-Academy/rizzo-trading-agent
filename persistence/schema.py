"""Schema for the augmented persistence layer.

Tables introduced in the refactor live here. The legacy schema in `db_utils`
continues to exist; these additions cover the forensic + lifecycle needs:

- positions           -> authoritative local view of open positions
- position_events     -> every transition (open/partial/trail/close/...)
- sl_tp_updates       -> audit of SL/TP moves
- orders              -> idempotent order log (client_order_id PK)
- decisions           -> full decision packet + llm output
- reconcile_log       -> drift events detected at bootstrap / cycle
"""
from __future__ import annotations

NEW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    id                  BIGSERIAL PRIMARY KEY,
    client_position_id  TEXT UNIQUE NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    size                NUMERIC(30, 10) NOT NULL,
    entry_price         NUMERIC(30, 10) NOT NULL,
    leverage            NUMERIC(10, 4),
    sl_price            NUMERIC(30, 10),
    tp_price            NUMERIC(30, 10),
    trailing_active     BOOLEAN NOT NULL DEFAULT FALSE,
    mfe_pct             NUMERIC(20, 8) DEFAULT 0,
    mae_pct             NUMERIC(20, 8) DEFAULT 0,
    bars_held           INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'open',
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    close_reason        TEXT,
    meta                JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol_status
    ON positions(symbol, status);

CREATE TABLE IF NOT EXISTS position_events (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_position_id TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_position_events_cpid
    ON position_events(client_position_id, created_at);

CREATE TABLE IF NOT EXISTS sl_tp_updates (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_position_id TEXT NOT NULL,
    field           TEXT NOT NULL,   -- 'sl' | 'tp'
    old_price       NUMERIC(30, 10),
    new_price       NUMERIC(30, 10),
    basis           TEXT,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    size            NUMERIC(30, 10) NOT NULL,
    order_kind      TEXT NOT NULL,
    reduce_only     BOOLEAN NOT NULL DEFAULT FALSE,
    trigger_price   NUMERIC(30, 10),
    leverage        INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',
    filled_size     NUMERIC(30, 10) DEFAULT 0,
    avg_price       NUMERIC(30, 10),
    error           TEXT,
    raw             JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_created
    ON orders(symbol, created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id          TEXT NOT NULL,
    packet          JSONB NOT NULL,
    llm_output      JSONB,
    final_decision  JSONB NOT NULL,
    deterministic_override TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_run_id
    ON decisions(run_id);

CREATE TABLE IF NOT EXISTS reconcile_log (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol          TEXT,
    kind            TEXT NOT NULL,   -- 'drift' | 'missing_local' | 'missing_exchange' | 'ok'
    detail          JSONB NOT NULL
);
"""


def ensure_schema(conn) -> None:
    """Idempotently create the new tables on an open psycopg2 connection."""
    with conn.cursor() as cur:
        cur.execute(NEW_SCHEMA_SQL)
    conn.commit()
