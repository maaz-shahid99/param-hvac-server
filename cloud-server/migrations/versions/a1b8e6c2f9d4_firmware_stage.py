"""firmware_releases.stage (canary|full)

Revision ID: a1b8e6c2f9d4
Revises: f3a9c4d27e10
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b8e6c2f9d4'
down_revision: Union[str, Sequence[str], None] = 'f3a9c4d27e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('firmware_releases', sa.Column(
        'stage', sa.String(length=10), nullable=False, server_default='full'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('firmware_releases', 'stage')
