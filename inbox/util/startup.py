import os
import sys
import subprocess
from pkg_resources import require, DistributionNotFound, VersionConflict

from setproctitle import setproctitle; setproctitle('inbox')
import sqlalchemy
from alembic.config import Config as alembic_config
from alembic.script import ScriptDirectory

from inbox.server.config import config

from inbox.server.log import get_logger
log = get_logger()


def check_requirements(requirements_path):
    # Check our requirements package
    failed_deps = []
    with open(requirements_path, 'r') as f:
        for x in f:
            x = x.strip()
            if not x or x.startswith('#') or x.startswith('-e '):
                continue
            try:
                require(x)
            except (DistributionNotFound, VersionConflict):
                failed_deps.append(x)

    if failed_deps:
        raise ImportError("\nPython module dependency verification failed! \n\n"
                 "The following dependencies are either missing or out of "
                 "date: \n\t{}\n\nYou probably need to run --> sudo pip "
                 "install -r requirements.txt\n"
                 .format("\n\t".join(failed_deps)))


def check_db():
    # Check database revision
    from inbox.server.models.ignition import db_uri  # needs to be after load_config()
    inbox_db_engine = sqlalchemy.create_engine(db_uri())

    # top-level, with setup.sh
    alembic_ini_filename = config.get('ALEMBIC_INI', None)
    assert alembic_ini_filename, 'Must set ALEMBIC_INI config var'
    assert os.path.isfile(alembic_ini_filename), \
        'Must have alembic.ini file at {}'.format(alembic_ini_filename)
    alembic_cfg = alembic_config(alembic_ini_filename)

    try:
        inbox_db_engine.dialect.has_table(inbox_db_engine, 'alembic_version')
    except sqlalchemy.exc.OperationalError:
        sys.exit("Databases don't exist! Run create_db.py")

    if inbox_db_engine.dialect.has_table(inbox_db_engine, 'alembic_version'):
        res = inbox_db_engine.execute('SELECT version_num from alembic_version')
        current_revision = [r for r in res][0][0]
        assert current_revision, \
            'Need current revision in alembic_version table...'

        script = ScriptDirectory.from_config(alembic_cfg)
        head_revision = script.get_current_head()
        log.info('Head database revision: {0}'.format(head_revision))
        log.info('Current database revision: {0}'.format(current_revision))

        if current_revision != head_revision:
            raise Exception('Outdated database! Migrate using `alembic upgrade head`')
        else:
            log.info('[OK] Database scheme matches latest')
    else:
        raise Exception('Un-stamped database! `create_db.py` should have done this... bailing.')


def check_sudo():
    if os.getuid() == 0:
        raise Exception("Don't run Inbox as root!")


def clean_pyc():
    # Keep it clean for development
    log.debug('Removing pyc files...')
    for root, dir, files in os.walk('./src'):
        for filename in files:
            if filename[-4:] == '.pyc':
                full_path = os.path.join(root, filename)
                log.info('removing {0}'.format(full_path))
                os.remove(full_path)
    log.debug("Not writing pyc bytecode for this exectution")
    sys.dont_write_bytecode = True


def git_rev():
    return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'])


def preflight():
    check_sudo()
    requirements_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     '../../requirements.txt')
    check_requirements(requirements_path)
    clean_pyc()
    check_db()

    log.debug("Current rev: {}".format(git_rev()))
