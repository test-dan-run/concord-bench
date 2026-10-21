"""Phase 2 (Label): segment uid / edited_at / meta, sync_package

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('uid', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('edited_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('meta', sa.JSON(), nullable=True))
    op.execute("UPDATE segment SET uid = lower(hex(randomblob(16))), meta = '{}'")
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.alter_column('uid', existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column('meta', existing_type=sa.JSON(), nullable=False)
        batch_op.create_index('ix_segment_uid', ['uid'], unique=False)

    op.create_table('sync_package',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=False),
    sa.Column('counts', sa.JSON(), nullable=False),
    sa.Column('manifest', sa.JSON(), nullable=False),
    sa.Column('conflicts', sa.JSON(), nullable=False),
    sa.Column('path', sa.String(length=1000), nullable=True),
    sa.Column('snapshot_path', sa.String(length=1000), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('applied_at', sa.DateTime(), nullable=True),
    sa.Column('undone_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['dataset_id'], ['dataset.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('sync_package')
    with op.batch_alter_table('segment', schema=None) as batch_op:
        batch_op.drop_index('ix_segment_uid')
        batch_op.drop_column('meta')
        batch_op.drop_column('edited_at')
        batch_op.drop_column('uid')
