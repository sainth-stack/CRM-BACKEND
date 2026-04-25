"""dispatch and identity hardening

Revision ID: a20260424_0003
Revises: a20260423_0002
Create Date: 2026-04-24 00:45:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a20260424_0003"
down_revision = "a20260423_0002"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_unique(inspector, table_name: str, constraint_name: str) -> bool:
    return any(constraint["name"] == constraint_name for constraint in inspector.get_unique_constraints(table_name))


def _build_identity_key(row) -> str | None:
    domain = (row.get("domain") or "").strip().lower()
    if domain:
        return domain

    website = (row.get("website") or "").strip().lower()
    if website:
        if "://" in website:
            host = website.split("://", 1)[1]
        else:
            host = website
        host = host.split("/", 1)[0].strip().lower().removeprefix("www.")
        if host:
            return host

    name = (row.get("name") or "").strip().lower()
    if name:
        return " ".join(name.split())
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("target_companies"):
        if not _has_column(inspector, "target_companies", "identity_key"):
            op.add_column("target_companies", sa.Column("identity_key", sa.String(), nullable=True))

        rows = bind.execute(
            sa.text(
                "SELECT id, campaign_id, domain, website, name, identity_key "
                "FROM target_companies ORDER BY campaign_id, id"
            )
        ).mappings()

        seen: set[tuple[str, str]] = set()
        for row in rows:
            identity_key = row.get("identity_key") or _build_identity_key(row)
            if not identity_key:
                continue

            identity_key = identity_key.strip().lower()
            scoped_key = (row["campaign_id"], identity_key)
            if scoped_key in seen:
                identity_key = f"{identity_key}::{row['id']}"
                scoped_key = (row["campaign_id"], identity_key)

            seen.add(scoped_key)
            bind.execute(
                sa.text("UPDATE target_companies SET identity_key = :identity_key WHERE id = :id"),
                {"identity_key": identity_key, "id": row["id"]},
            )

        inspector = sa.inspect(bind)
        if not _has_index(inspector, "target_companies", "ix_target_companies_identity_key"):
            op.create_index("ix_target_companies_identity_key", "target_companies", ["identity_key"], unique=False)
        if not _has_unique(inspector, "target_companies", "_campaign_identity_uc"):
            op.create_unique_constraint("_campaign_identity_uc", "target_companies", ["campaign_id", "identity_key"])

    inspector = sa.inspect(bind)
    if inspector.has_table("email_drafts"):
        if not _has_column(inspector, "email_drafts", "dispatch_state"):
            op.add_column("email_drafts", sa.Column("dispatch_state", sa.String(), nullable=True, server_default="IDLE"))
        if not _has_column(inspector, "email_drafts", "dispatch_started_at"):
            op.add_column("email_drafts", sa.Column("dispatch_started_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "email_drafts", "dispatch_completed_at"):
            op.add_column("email_drafts", sa.Column("dispatch_completed_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "email_drafts", "dispatch_error"):
            op.add_column("email_drafts", sa.Column("dispatch_error", sa.Text(), nullable=True))

        bind.execute(sa.text("UPDATE email_drafts SET dispatch_state = 'IDLE' WHERE dispatch_state IS NULL"))
        bind.execute(
            sa.text(
                "UPDATE email_drafts "
                "SET dispatch_state = 'SENT', dispatch_completed_at = COALESCE(dispatch_completed_at, sent_at) "
                "WHERE status = 'SENT'"
            )
        )

        inspector = sa.inspect(bind)
        if not _has_index(inspector, "email_drafts", "ix_email_drafts_dispatch_state"):
            op.create_index("ix_email_drafts_dispatch_state", "email_drafts", ["dispatch_state"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("email_drafts"):
        if _has_index(inspector, "email_drafts", "ix_email_drafts_dispatch_state"):
            op.drop_index("ix_email_drafts_dispatch_state", table_name="email_drafts")
        for column_name in [
            "dispatch_error",
            "dispatch_completed_at",
            "dispatch_started_at",
            "dispatch_state",
        ]:
            if _has_column(inspector, "email_drafts", column_name):
                op.drop_column("email_drafts", column_name)

    inspector = sa.inspect(bind)
    if inspector.has_table("target_companies"):
        if _has_unique(inspector, "target_companies", "_campaign_identity_uc"):
            op.drop_constraint("_campaign_identity_uc", "target_companies", type_="unique")
        if _has_index(inspector, "target_companies", "ix_target_companies_identity_key"):
            op.drop_index("ix_target_companies_identity_key", table_name="target_companies")
        if _has_column(inspector, "target_companies", "identity_key"):
            op.drop_column("target_companies", "identity_key")
