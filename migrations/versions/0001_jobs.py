"""Persist jobs and enforce immutable terminal rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("schema", postgresql.JSONB(), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("webhook_url", sa.Text()),
        sa.Column("stage", sa.Text()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("result", postgresql.JSONB()),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCESS','PARTIAL','FAILED')", name="job_status"),
        sa.CheckConstraint("retry_count >= 0", name="job_retries"),
        sa.UniqueConstraint("organization_id", "document_hash", "schema_hash", name="job_submission"),
    )
    op.execute("""
        CREATE FUNCTION prevent_terminal_job_update() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('SUCCESS', 'PARTIAL', 'FAILED') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Terminal jobs are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""CREATE TRIGGER immutable_terminal_job BEFORE UPDATE ON jobs
                  FOR EACH ROW EXECUTE FUNCTION prevent_terminal_job_update()""")


def downgrade():
    op.drop_table("jobs")
    op.execute("DROP FUNCTION prevent_terminal_job_update()")
