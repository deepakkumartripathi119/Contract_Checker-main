"""Create policy chunks and tenant configuration tables.

Revision ID: 001_policy_and_tenant
Revises:
Create Date: 2026-11-01
"""

from alembic import op

from core.pg_schema import apply_schema

revision = "001_policy_and_tenant"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_schema(op.get_bind())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS policy_chunks")
    op.execute("DROP TABLE IF EXISTS tenant_configs")
