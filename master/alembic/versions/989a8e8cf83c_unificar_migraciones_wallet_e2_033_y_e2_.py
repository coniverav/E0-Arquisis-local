"""Unificar migraciones Wallet E2-033 y E2-040

Revision ID: 989a8e8cf83c
Revises: e233bidaccepted, e240swaplc
Create Date: 2026-10-28 03:11:13.363106

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '989a8e8cf83c'
down_revision: Union[str, Sequence[str], None] = ('e233bidaccepted', 'e240swaplc')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
