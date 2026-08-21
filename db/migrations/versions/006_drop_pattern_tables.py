"""Drop admin_patterns and limit_patterns tables

Revision ID: 006_drop_patterns
Revises: 005_panel_monitoring
Create Date: 2026-08-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006_drop_patterns'
down_revision = '005_panel_monitoring'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'admin_patterns' in tables:
        try:
            op.drop_index('ix_admin_patterns_type', table_name='admin_patterns')
        except Exception:
            pass
        try:
            op.drop_index('ix_admin_patterns_admin', table_name='admin_patterns')
        except Exception:
            pass
        try:
            op.drop_table('admin_patterns')
        except Exception:
            pass

    if 'limit_patterns' in tables:
        try:
            op.drop_index('ix_limit_patterns_type', table_name='limit_patterns')
        except Exception:
            pass
        try:
            op.drop_table('limit_patterns')
        except Exception:
            pass


def downgrade() -> None:
    pass
