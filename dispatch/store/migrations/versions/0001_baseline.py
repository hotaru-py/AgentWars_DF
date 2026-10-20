"""Baseline: every v2 table plus the append-only / immutability triggers.

Revision ID: 0001_baseline
Revises:
"""
from alembic import op

from dispatch.store import schema

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.create_all(op.get_bind())


def downgrade() -> None:
    schema.metadata.drop_all(op.get_bind())
