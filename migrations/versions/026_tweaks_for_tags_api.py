"""tweaks for tags api

Revision ID: 27363edd2661
Revises: 59b42d0ac749
Create Date: 2014-05-10 00:01:15.577360

"""

# revision identifiers, used by Alembic.
revision = '27363edd2661'
down_revision = '59b42d0ac749'

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


def upgrade():
    op.add_column('folder', sa.Column('exposed_name', sa.String(length=191),
                                      nullable=True))
    op.alter_column('internaltag', 'thread_id',
                    existing_type=mysql.INTEGER(display_width=11),
                    nullable=True)

    from inbox.server import tags
    from inbox.server.models import session_scope
    from inbox.server.models.tables.base import Folder
    with session_scope(versioned=False) as db_session:
        for folder in db_session.query(Folder):
            folder.exposed_name = tags.util.exposed_name(folder, folder.name)

        db_session.commit()


def downgrade():
    op.alter_column('internaltag', 'thread_id',
                    existing_type=mysql.INTEGER(display_width=11),
                    nullable=False)
    op.drop_column('folder', 'exposed_name')
