"""Add public ids to transactions

Revision ID: 1edbd63582c2
Revises: 21878b1b3d4b
Create Date: 2014-05-20 23:31:48.924200

"""

# revision identifiers, used by Alembic.
revision = '1edbd63582c2'
down_revision = '21878b1b3d4b'

import sys
from gc import collect as garbage_collect
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


def upgrade():
    op.add_column('transaction',
                  sa.Column('public_id', mysql.BINARY(16), nullable=True))
    op.add_column('transaction',
                  sa.Column('object_public_id', mysql.BINARY(16),
                            nullable=True))
    op.create_index('ix_transaction_public_id', 'transaction', ['public_id'],
                    unique=False)

    from inbox.sqlalchemy.util import generate_public_id
    # TODO(emfree) reflect
    from inbox.server.models import session_scope
    from inbox.server.models.tables.base import Transaction

    with session_scope(versioned=False, ignore_soft_deletes=False) as db_session:
        count = 0
        for entry in db_session.query(Transaction).yield_per(500):
            entry.public_id = generate_public_id()
            count += 1
            if not count % 500:
                sys.stdout.write('.')
                sys.stdout.flush()
                db_session.commit()
                garbage_collect()

    op.alter_column('transaction', 'public_id',
                    existing_type=mysql.BINARY(16), nullable=False)


def downgrade():
    op.drop_index('ix_transaction_public_id', table_name='transaction')
    op.drop_column('transaction', 'public_id')
    op.drop_column('transaction', 'object_public_id')
