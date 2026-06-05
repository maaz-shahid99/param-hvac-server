"""commissioned_devices.name (operator-assigned friendly name)

Revision ID: d4e8f1a2b6c9
Revises: b2f1a9c4d7e3
Create Date: 2026-06-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8f1a2b6c9'
down_revision: Union[str, Sequence[str], None] = 'b2f1a9c4d7e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('commissioned_devices', sa.Column(
        'name', sa.String(length=120), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('commissioned_devices', 'name')
