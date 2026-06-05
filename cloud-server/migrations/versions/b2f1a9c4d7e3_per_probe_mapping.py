"""per-probe mapping: sensor_map.probe_rom + tenants.alert_granularity

Re-keys SensorMap from (tenant, eui) to (tenant, eui, probe_rom) so one sensor's
DS18B20 probes can fan out to many ports, and adds the tenant alert-granularity
setting ('sensor' = hottest probe, legacy; 'probe' = each probe alerts on its own).

Revision ID: b2f1a9c4d7e3
Revises: c3d7e1f4a9b2
Create Date: 2026-06-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2f1a9c4d7e3'
down_revision: Union[str, Sequence[str], None] = 'c3d7e1f4a9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Tenant-level alert granularity (default preserves the original behavior).
    op.add_column('tenants', sa.Column(
        'alert_granularity', sa.String(length=10),
        nullable=False, server_default='sensor'))

    # sensor_map gains the probe ROM and re-keys uniqueness to include it.
    op.add_column('sensor_map', sa.Column(
        'probe_rom', sa.String(length=32),
        nullable=False, server_default=''))
    op.create_index(op.f('ix_sensor_map_probe_rom'), 'sensor_map', ['probe_rom'], unique=False)
    op.drop_index('ix_sensormap_tenant_eui', table_name='sensor_map')
    op.create_index('ix_sensormap_tenant_eui_probe', 'sensor_map',
                    ['tenant_id', 'eui', 'probe_rom'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_sensormap_tenant_eui_probe', table_name='sensor_map')
    op.create_index('ix_sensormap_tenant_eui', 'sensor_map', ['tenant_id', 'eui'], unique=True)
    op.drop_index(op.f('ix_sensor_map_probe_rom'), table_name='sensor_map')
    op.drop_column('sensor_map', 'probe_rom')
    op.drop_column('tenants', 'alert_granularity')
