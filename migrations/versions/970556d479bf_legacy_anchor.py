"""legacy migration anchor

Revision ID: 970556d479bf
Revises:
Create Date: 2026-04-25 16:05:00
"""

# This revision acts as a compatibility anchor for older deployed databases
# that still reference a historical Alembic revision no longer present in the
# repository. It is intentionally a no-op so those databases can migrate into
# the current managed chain safely.

revision = "970556d479bf"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
