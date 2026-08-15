"""Add panel_id and monitoring fields to users table

Revision ID: 005_panel_monitoring
Revises: 004_limit_patterns
Create Date: 2026-08-15

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '005_panel_monitoring'
down_revision = '004_limit_patterns'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns to users table safely if not already present
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}
    
    if 'panel_id' not in existing_columns:
        op.add_column('users', sa.Column('panel_id', sa.Integer(), nullable=True))
        try:
            op.create_index('ix_users_panel_id', 'users', ['panel_id'], unique=True)
        except Exception:
            pass
        
    if 'is_monitored' not in existing_columns:
        op.add_column('users', sa.Column('is_monitored', sa.Boolean(), nullable=True, server_default=sa.text('1')))
        
    if 'effective_ip_limit' not in existing_columns:
        op.add_column('users', sa.Column('effective_ip_limit', sa.Integer(), nullable=True))


def downgrade() -> None:
    try:
        op.drop_index('ix_users_panel_id', table_name='users')
    except Exception:
        pass
    try:
        op.drop_column('users', 'effective_ip_limit')
        op.drop_column('users', 'is_monitored')
        op.drop_column('users', 'panel_id')
    except Exception:
        pass
