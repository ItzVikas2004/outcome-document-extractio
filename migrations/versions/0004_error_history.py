"""Preserve safe failure events across attempts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_error_history"
down_revision = "0003_llm_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("error_history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column("jobs", "error_history")
