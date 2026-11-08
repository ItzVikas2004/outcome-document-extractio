"""Durable leases, retry scheduling, deadline tracking and forward-only states."""
from alembic import op
import sqlalchemy as sa

revision = "0002_job_leases"
down_revision = "0001_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER immutable_terminal_job ON jobs")
    for name, default in (("attempt_count", "0"), ("max_attempts", "3")):
        op.add_column("jobs", sa.Column(name, sa.Integer(), nullable=False, server_default=default))
    for name in ("lease_token", "worker_id"):
        op.add_column("jobs", sa.Column(name, sa.String(36)))
    for name in ("lease_expires_at", "heartbeat_at", "next_attempt_at", "first_started_at", "finished_at", "deadline_at"):
        op.add_column("jobs", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("queue_duration_ms", sa.Integer()))
    op.execute("UPDATE jobs SET deadline_at = created_at + interval '1800 seconds'")
    op.alter_column("jobs", "deadline_at", nullable=False)
    op.create_check_constraint("job_attempts", "jobs", "attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0")
    op.create_index("jobs_queue", "jobs", ["created_at"], postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"))
    op.create_index("jobs_deadline", "jobs", ["deadline_at"], postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"))
    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_terminal_job_update() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'PENDING' THEN
                    RAISE EXCEPTION 'Jobs must start PENDING';
                END IF;
                NEW.created_at := clock_timestamp();
                NEW.updated_at := NEW.created_at;
                NEW.deadline_at := NEW.created_at + interval '1800 seconds';
                RETURN NEW;
            END IF;
            IF OLD.status IN ('SUCCESS','PARTIAL','FAILED') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Terminal jobs are immutable';
            END IF;
            IF OLD.status <> NEW.status AND NOT (
                (OLD.status = 'PENDING' AND NEW.status = 'RUNNING') OR
                (OLD.status = 'RUNNING' AND NEW.status IN ('SUCCESS','PARTIAL','FAILED'))
            ) THEN
                RAISE EXCEPTION 'Invalid job state transition';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""CREATE TRIGGER immutable_terminal_job BEFORE INSERT OR UPDATE ON jobs
                  FOR EACH ROW EXECUTE FUNCTION prevent_terminal_job_update()""")


def downgrade():
    op.execute("DROP TRIGGER immutable_terminal_job ON jobs")
    op.execute("""CREATE OR REPLACE FUNCTION prevent_terminal_job_update() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('SUCCESS','PARTIAL','FAILED') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Terminal jobs are immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER immutable_terminal_job BEFORE UPDATE ON jobs
                  FOR EACH ROW EXECUTE FUNCTION prevent_terminal_job_update()""")
    op.drop_index("jobs_deadline", table_name="jobs")
    op.drop_index("jobs_queue", table_name="jobs")
    op.drop_constraint("job_attempts", "jobs")
    for name in ("attempt_count", "max_attempts", "lease_token", "worker_id", "lease_expires_at",
                 "heartbeat_at", "next_attempt_at", "first_started_at", "finished_at", "deadline_at", "queue_duration_ms"):
        op.drop_column("jobs", name)
