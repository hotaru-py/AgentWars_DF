"""Database schema (spec §5). SQLAlchemy Core; runs on Postgres (production) and SQLite (dev/tests).

Each table stores the full Pydantic model as a JSON document (`doc`) plus the indexed columns
we query on. The models in dispatch/core stay the single source of truth, so adding a model
field doesn't need a migration unless we want to query on it.

Integrity rules enforced by the database itself (triggers, see install_triggers):
- `events` is append-only: no UPDATE, no DELETE.
- `decisions` rows are immutable except `outcome` / `outcome_at`, which may be set once.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

JSONDoc = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")     # sqlite autoincrement needs INTEGER
TS = sa.DateTime(timezone=True)


def _id() -> sa.Column:
    return sa.Column("id", sa.String(40), primary_key=True)


depots = sa.Table("depots", metadata, _id(), sa.Column("doc", JSONDoc, nullable=False))

vehicles = sa.Table(
    "vehicles", metadata, _id(),
    sa.Column("depot_id", sa.String(40), sa.ForeignKey("depots.id"), nullable=False, index=True),
    sa.Column("state", sa.String(20), nullable=False, index=True),
    sa.Column("doc", JSONDoc, nullable=False),
)

drivers = sa.Table(
    "drivers", metadata, _id(),
    sa.Column("vehicle_id", sa.String(40), sa.ForeignKey("vehicles.id"), nullable=True, index=True),
    sa.Column("doc", JSONDoc, nullable=False),
)

customers = sa.Table(
    "customers", metadata, _id(),
    sa.Column("phone", sa.String(32), nullable=False, index=True),
    sa.Column("doc", JSONDoc, nullable=False),
)

orders = sa.Table(
    "orders", metadata, _id(),
    sa.Column("depot_id", sa.String(40), sa.ForeignKey("depots.id"), nullable=False, index=True),
    sa.Column("customer_id", sa.String(40), sa.ForeignKey("customers.id"), nullable=False, index=True),
    sa.Column("state", sa.String(20), nullable=False, index=True),
    sa.Column("lat", sa.Float, nullable=True),
    sa.Column("lng", sa.Float, nullable=True),
    sa.Column("digipin", sa.String(10), nullable=True, index=True),
    sa.Column("created_at", TS, nullable=False, index=True),
    sa.Column("doc", JSONDoc, nullable=False),
)

plans = sa.Table(
    "plans", metadata, _id(),
    sa.Column("depot_id", sa.String(40), sa.ForeignKey("depots.id"), nullable=False, index=True),
    sa.Column("created_at", TS, nullable=False, index=True),
    sa.Column("parent_plan_id", sa.String(40), nullable=True),
    sa.Column("doc", JSONDoc, nullable=False),
)

current_plans = sa.Table(
    "current_plans", metadata,
    sa.Column("depot_id", sa.String(40), sa.ForeignKey("depots.id"), primary_key=True),
    sa.Column("plan_id", sa.String(40), sa.ForeignKey("plans.id"), nullable=False),
)

incidents = sa.Table(
    "incidents", metadata, _id(),
    sa.Column("kind", sa.String(20), nullable=False, index=True),
    sa.Column("state", sa.String(20), nullable=False, index=True),
    sa.Column("starts_at", TS, nullable=False),
    sa.Column("doc", JSONDoc, nullable=False),
)

policies = sa.Table(
    "policies", metadata,
    sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("doc", JSONDoc, nullable=False),
)

events = sa.Table(
    "events", metadata,
    sa.Column("seq", BigId, primary_key=True, autoincrement=True),
    sa.Column("ts", TS, nullable=False, index=True),
    sa.Column("type", sa.String(40), nullable=False, index=True),
    sa.Column("entity", sa.String(40), nullable=False, index=True),
    sa.Column("payload", JSONDoc, nullable=False),
)

decisions = sa.Table(
    "decisions", metadata, _id(),
    sa.Column("ts", TS, nullable=False, index=True),
    sa.Column("type", sa.String(40), nullable=False, index=True),
    sa.Column("subject", sa.String(40), nullable=False, index=True),
    sa.Column("decider", sa.String(40), nullable=False, index=True),
    sa.Column("route", sa.String(20), nullable=False, index=True),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("outcome", sa.String(80), nullable=True),
    sa.Column("outcome_at", TS, nullable=True),
    sa.Column("doc", JSONDoc, nullable=False),
)


# ---------------------------------------------------------------- integrity triggers
_SQLITE_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
       BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
       BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS decisions_immutable
       BEFORE UPDATE OF id, ts, type, subject, decider, route, confidence, doc ON decisions
       BEGIN SELECT RAISE(ABORT, 'decisions are immutable except outcome'); END""",
    """CREATE TRIGGER IF NOT EXISTS decisions_outcome_once BEFORE UPDATE OF outcome ON decisions
       WHEN OLD.outcome IS NOT NULL
       BEGIN SELECT RAISE(ABORT, 'decision outcome is already set'); END""",
    """CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
       BEGIN SELECT RAISE(ABORT, 'decisions cannot be deleted'); END""",
]

_PG_TRIGGERS = [
    """CREATE OR REPLACE FUNCTION dispatch_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'events are append-only'; END $$ LANGUAGE plpgsql""",
    """CREATE OR REPLACE FUNCTION dispatch_decision_guard() RETURNS trigger AS $$
       BEGIN
         IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'decisions cannot be deleted'; END IF;
         IF (NEW.id, NEW.ts, NEW.type, NEW.subject, NEW.decider, NEW.route, NEW.confidence, NEW.doc::text)
            IS DISTINCT FROM (OLD.id, OLD.ts, OLD.type, OLD.subject, OLD.decider, OLD.route, OLD.confidence, OLD.doc::text)
         THEN RAISE EXCEPTION 'decisions are immutable except outcome'; END IF;
         IF OLD.outcome IS NOT NULL AND NEW.outcome IS DISTINCT FROM OLD.outcome
         THEN RAISE EXCEPTION 'decision outcome is already set'; END IF;
         RETURN NEW;
       END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS events_append_only ON events",
    """CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
       FOR EACH ROW EXECUTE FUNCTION dispatch_append_only()""",
    "DROP TRIGGER IF EXISTS decisions_guard ON decisions",
    """CREATE TRIGGER decisions_guard BEFORE UPDATE OR DELETE ON decisions
       FOR EACH ROW EXECUTE FUNCTION dispatch_decision_guard()""",
]


def install_triggers(conn: sa.Connection) -> None:
    statements = _PG_TRIGGERS if conn.dialect.name == "postgresql" else _SQLITE_TRIGGERS
    for stmt in statements:
        conn.exec_driver_sql(stmt)


def create_all(conn: sa.Connection) -> None:
    """Create every table and trigger (dev/tests and the baseline migration)."""
    metadata.create_all(conn)
    install_triggers(conn)
