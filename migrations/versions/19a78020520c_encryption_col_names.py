""" Clarify encryption-related db column names.

We also delete the reference to "AES" in the column name to allow for the
encryption strategy to change under the hood without the column name becoming
misleading.

Revision ID: 19a78020520c
Revises: 563d405d1f99
Create Date: 2014-04-23 00:42:47.752432

"""

# revision identifiers, used by Alembic.
revision = '19a78020520c'
down_revision = '563d405d1f99'

from alembic import op

import sqlalchemy as sa


def upgrade():
    op.alter_column('account', 'password_aes',
                    new_column_name='encrypted_password',
                    existing_type=sa.BLOB(256))
    op.alter_column('account', 'key',
                    new_column_name='encryption_key',
                    existing_type=sa.BLOB(128))


def downgrade():
    op.alter_column('account', 'encrypted_password',
                    new_column_name='password_aes',
                    existing_type=sa.BLOB(256))
    op.alter_column('account', 'encryption_key', new_column_name='key',
                    existing_type=sa.BLOB(128))
