"""thresholds.hum_min / hum_max / hum_enabled — relative-humidity alert band

Humidity is a band (min AND max), unlike the temperature limits already on this
table which are ceilings only: air that is too dry is an ESD risk and air that
is too damp risks condensation.

`hum_enabled` defaults to FALSE on purpose. Existing tenants have never chosen a
humidity limit, and defaulting them into alerting would email them about a
condition they never asked to watch.

Revision ID: b7d3e5c1a802
Revises: a1b8e6c2f9d4
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d3e5c1a802'
down_revision: Union[str, Sequence[str], None] = 'a1b8e6c2f9d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('thresholds', sa.Column(
        'hum_min', sa.Float(), nullable=False, server_default='0'))
    op.add_column('thresholds', sa.Column(
        'hum_max', sa.Float(), nullable=False, server_default='100'))
    op.add_column('thresholds', sa.Column(
        'hum_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('thresholds', 'hum_enabled')
    op.drop_column('thresholds', 'hum_max')
    op.drop_column('thresholds', 'hum_min')
