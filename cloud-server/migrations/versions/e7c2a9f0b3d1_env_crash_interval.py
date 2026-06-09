"""env_readings + crash_reports tables, tenants.collect_interval_s

Revision ID: e7c2a9f0b3d1
Revises: d4e8f1a2b6c9
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c2a9f0b3d1'
down_revision: Union[str, Sequence[str], None] = 'd4e8f1a2b6c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('tenants', sa.Column(
        'collect_interval_s', sa.Integer(), nullable=False, server_default='60'))

    op.create_table(
        'env_readings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('ts', sa.Float(), nullable=False),
        sa.Column('eui', sa.String(length=32), nullable=False),
        sa.Column('temp', sa.Float(), nullable=False, server_default='0'),
        sa.Column('hum', sa.Float(), nullable=False, server_default='0'),
        sa.Column('pres', sa.Float(), nullable=False, server_default='0'),
        sa.Column('voc', sa.Float(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_env_readings_tenant_id', 'env_readings', ['tenant_id'])
    op.create_index('ix_env_readings_ts', 'env_readings', ['ts'])
    op.create_index('ix_env_readings_eui', 'env_readings', ['eui'])
    op.create_index('ix_envreading_tenant_ts', 'env_readings', ['tenant_id', 'ts'])

    op.create_table(
        'crash_reports',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('eui', sa.String(length=32), nullable=False),
        sa.Column('ts', sa.Float(), nullable=False),
        sa.Column('reset_reason', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('fw', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('pc', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('backtrace', sa.Text(), nullable=False, server_default=''),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_crash_reports_tenant_id', 'crash_reports', ['tenant_id'])
    op.create_index('ix_crash_reports_eui', 'crash_reports', ['eui'])
    op.create_index('ix_crash_reports_ts', 'crash_reports', ['ts'])
    op.create_index('ix_crash_tenant_ts', 'crash_reports', ['tenant_id', 'ts'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('crash_reports')
    op.drop_table('env_readings')
    op.drop_column('tenants', 'collect_interval_s')
