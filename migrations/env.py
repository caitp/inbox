from __future__ import with_statement
from alembic import context
from sqlalchemy import create_engine, pool

from logging.config import fileConfig

from inbox.server.config import load_config

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)

# If alembic was invoked with --tag=test, load the test Inbox config. Otherwise
# load the default config.
if context.get_tag_argument() == 'test':
    load_config('tests/config.cfg')
else:
    load_config()

from inbox.server.models.tables.base import register_backends
table_mod_for = register_backends()


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
from inbox.server.models import Base
target_metadata = Base.metadata

from inbox.server.models.ignition import db_uri

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(url=db_uri())

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    engine = create_engine(db_uri(), poolclass=pool.NullPool)

    connection = engine.connect()
    context.configure(
                connection=connection,
                target_metadata=target_metadata
                )

    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.close()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
