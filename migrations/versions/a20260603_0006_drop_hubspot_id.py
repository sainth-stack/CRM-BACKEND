"""Drop the unused hubspot_id column from decision_makers

The HubSpot integration was removed entirely. The hubspot_id column on
decision_makers is no longer read or written by any code path, so we drop it.

Revision ID: a20260603_0006
Revises: a20260526_0005
Create Date: 2026-06-03 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "a20260603_0006"
down_revision = "a20260526_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("decision_makers") as batch_op:
        batch_op.drop_column("hubspot_id")


def downgrade() -> None:
    with op.batch_alter_table("decision_makers") as batch_op:
        batch_op.add_column(sa.Column("hubspot_id", sa.String(), nullable=True))
