"""commissioned-device roster (tenant-scoped, survives phone changes)

Adds the commissioned_devices table so the app's Devices list lives server-side
per tenant instead of in one phone's local cache.

Revision ID: c3d7e1f4a9b2
Revises: 23a1d4dec2fe
Create Date: 2026-06-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d7e1f4a9b2'
down_revision: Union[str, Sequence[str], None] = '23a1d4dec2fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'commissioned_devices',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('eui', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('role', sa.String(length=2), nullable=False),
        sa.Column('added_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_commissioned_devices_eui'), 'commissioned_devices', ['eui'], unique=False)
    op.create_index(op.f('ix_commissioned_devices_tenant_id'), 'commissioned_devices', ['tenant_id'], unique=False)
    op.create_index('ix_commdev_tenant_eui', 'commissioned_devices', ['tenant_id', 'eui'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_commdev_tenant_eui', table_name='commissioned_devices')
    op.drop_index(op.f('ix_commissioned_devices_tenant_id'), table_name='commissioned_devices')
    op.drop_index(op.f('ix_commissioned_devices_eui'), table_name='commissioned_devices')
    op.drop_table('commissioned_devices')
