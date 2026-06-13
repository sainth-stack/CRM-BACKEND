"""Remove the REVIEW verdict: reclassify TargetCompany 'REVIEW' -> 'REJECTED'.

The ICP verdict is now binary: a company is ACCEPTED iff its score clears
ICP_ACCEPT_THRESHOLD (and has no firmographic violation), otherwise REJECTED.
The old REVIEW band only ever held sub-threshold companies — borderline operators
(35 <= score < threshold) and competitor/vendor overlaps (score 0) — so every
existing REVIEW row is, by definition, below threshold and maps to REJECTED.

Data-only migration; idempotent and safe to re-run. The status column is a
free-form String (no Postgres ENUM), so no type change is needed.

Revision ID: a20260612_0012
Revises: a20260612_0011
Create Date: 2026-06-12 00:00:00
"""
from alembic import op

revision = "a20260612_0012"
down_revision = "a20260612_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE target_companies SET status = 'REJECTED' WHERE status = 'REVIEW'"
    )


def downgrade() -> None:
    # Irreversible in principle (REVIEW rows are indistinguishable from other rejects
    # after the merge). No-op so the migration chain can still be stepped back.
    pass
