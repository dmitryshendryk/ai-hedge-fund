"""add headcount_snapshots table

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-10 13:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5'
down_revision: str | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('headcount_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticker', sa.String(length=20), nullable=False),
        sa.Column('employee_count', sa.Integer(), nullable=False),
        sa.Column('snapshot_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_headcount_snapshots_id'), 'headcount_snapshots', ['id'], unique=False)
    op.create_index(op.f('ix_headcount_snapshots_ticker'), 'headcount_snapshots', ['ticker'], unique=False)
    op.create_index(op.f('ix_headcount_snapshots_snapshot_at'), 'headcount_snapshots', ['snapshot_at'], unique=False)
    op.create_index('ix_headcount_snapshots_ticker_at', 'headcount_snapshots', ['ticker', 'snapshot_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_headcount_snapshots_ticker_at', table_name='headcount_snapshots')
    op.drop_index(op.f('ix_headcount_snapshots_snapshot_at'), table_name='headcount_snapshots')
    op.drop_index(op.f('ix_headcount_snapshots_ticker'), table_name='headcount_snapshots')
    op.drop_index(op.f('ix_headcount_snapshots_id'), table_name='headcount_snapshots')
    op.drop_table('headcount_snapshots')
