"""firmware_releases + ota_state + fleet_status + support_audit

Revision ID: f3a9c4d27e10
Revises: e7c2a9f0b3d1
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c4d27e10'
down_revision: Union[str, Sequence[str], None] = 'e7c2a9f0b3d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'firmware_releases',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=4), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False, server_default='optional'),
        sa.Column('filename', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_firmware_releases_kind', 'firmware_releases', ['kind'])
    op.create_index('ix_firmware_releases_version', 'firmware_releases', ['version'])
    op.create_index('ix_firmware_releases_created_at', 'firmware_releases', ['created_at'])

    op.create_table(
        'ota_state',
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('approved_c3', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('approved_c6', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('tenant_id'),
    )

    op.create_table(
        'fleet_status',
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('fw_c3', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('fw_c6', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('heap_free', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('role', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('tenant_id'),
    )

    op.create_table(
        'support_audit',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('ts', sa.Float(), nullable=False),
        sa.Column('action', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('detail', sa.String(length=500), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_support_audit_ts', 'support_audit', ['ts'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('support_audit')
    op.drop_table('fleet_status')
    op.drop_table('ota_state')
    op.drop_table('firmware_releases')
