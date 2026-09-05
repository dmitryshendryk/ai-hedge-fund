"""add stop-loss columns to positions

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-08-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: str | None = 'e1f2a3b4c5d6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: a position added before this migration, or one yfinance cannot
    # price, has no stop.
    op.add_column('positions', sa.Column('stop_loss_price', sa.Float(), nullable=True))
    op.add_column('positions', sa.Column('stop_atr', sa.Float(), nullable=True))
    op.add_column('positions', sa.Column('stop_multiple', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('positions', 'stop_multiple')
    op.drop_column('positions', 'stop_atr')
    op.drop_column('positions', 'stop_loss_price')
