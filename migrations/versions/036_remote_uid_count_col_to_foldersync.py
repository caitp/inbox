"""add remote_uid_count col to foldersync

Revision ID: 318fa043403e
Revises: 563d405d1f99
Create Date: 2014-04-23 19:13:58.962983

"""

# revision identifiers, used by Alembic.
revision = '318fa043403e'
down_revision = '563d405d1f99'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('foldersync', sa.Column('remote_uid_count', sa.Integer(),
                                          server_default='0', nullable=False))
    op.add_column('foldersync', sa.Column('remote_uids_checked', sa.DateTime(),
                                          nullable=True))


def downgrade():
    op.drop_column('foldersync', 'remote_uid_count')
    op.drop_column('foldersync', 'remote_uids_checked')
