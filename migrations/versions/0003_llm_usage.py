"""Durable per-call accounting, including retries and unknown crash outcomes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_llm_usage"
down_revision = "0002_job_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("llm_usage", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column("jobs", "llm_usage")
