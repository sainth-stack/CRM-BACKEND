"""Rename TargetCompany.status 'RESEARCH_COMPLETE' -> 'ACCEPTED'.

The deep-research stage was merged into ICP qualification, so every company now
carries a research dossier regardless of verdict. The per-company status value
'RESEARCH_COMPLETE' was a leftover from the old separate-stage design and read as
if research were gated on it. It is purely the "accepted/qualified" verdict marker
that lets a company proceed to stakeholder ranking + drafting, so it is renamed to
'ACCEPTED' for clarity.

Data-only migration: updates existing rows in place. Idempotent and safe to re-run.
Note: this column is a free-form String (no Postgres ENUM), so no type change is
needed. Accepted companies that already advanced past Stage 5 are 'STAKEHOLDERS_
IDENTIFIED' and are intentionally left untouched.

Revision ID: a20260612_0011
Revises: a20260612_0010
Create Date: 2026-06-12 00:00:00
"""
from alembic import op

revision = "a20260612_0011"
down_revision = "a20260612_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE target_companies SET status = 'ACCEPTED' "
        "WHERE status = 'RESEARCH_COMPLETE'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE target_companies SET status = 'RESEARCH_COMPLETE' "
        "WHERE status = 'ACCEPTED'"
    )
