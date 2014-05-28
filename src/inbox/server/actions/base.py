""" Code for propagating Inbox datastore changes to the account backend.

Syncback actions don't update anything in the local datastore; the Inbox
datastore is updated asynchronously (see namespace.py) and bookkeeping about
the account backend state is updated when the changes show up in the mail sync
engine.

Dealing with write actions separately from read syncing allows us more
flexibility in responsiveness/latency on data propagation, and also makes us
unable to royally mess up a sync and e.g. accidentally delete a bunch of
messages on the account backend because our local datastore is messed up.

This read/write separation also allows us to easily disable syncback for
testing.

The main problem the separation presents is the fact that the read syncing
needs to deal with the fact that the local datastore may have new changes to
it that are not yet reflected in the account backend. In practice, this is
not really a problem because of the limited ways mail messages can change.
(For more details, see individual account backend submodules.)

ACTIONS MUST BE IDEMPOTENT! We are going to have task workers guarantee
at-least-once semantics.
"""
from redis import Redis
from rq import Queue, Connection

from inbox.util.misc import load_modules
from inbox.server.util.concurrency import GeventWorker
from inbox.server.models import session_scope
from inbox.server.models.tables.base import Account
from inbox.server.config import config
import inbox.server.actions

ACTION_MOD_FOR = {}


def register_backends():
    """
    Finds the action modules for the different providers
    (in the actions/ directory) and imports them.

    Creates a mapping of provider:actions_mod for each backend found.
    """
    # Find and import
    modules = load_modules(inbox.server.actions)

    # Create mapping
    for module in modules:
        if hasattr(module, 'PROVIDER'):
            provider = module.PROVIDER
            ACTION_MOD_FOR[provider] = module


def get_queue():
    # The queue label is set via config to allow multiple distinct Inbox
    # instances to hit the same Redis server without interfering with each
    # other.
    label = config.get('ACTION_QUEUE_LABEL', None)
    assert label, "Must set ACTION_QUEUE_LABEL in config.cfg"
    return Queue(label, connection=Redis())


def archive(account_id, thread_id):
    """Sync an archive action back to the backend. """
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)

        set_remote_archived = ACTION_MOD_FOR[account.provider]. \
            set_remote_archived
        set_remote_archived(account, thread_id, True, db_session)


def unarchive(account_id, thread_id):
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)

        set_remote_archived = ACTION_MOD_FOR[account.provider]. \
            set_remote_archived
        set_remote_archived(account, thread_id, False, db_session)


def star(account_id, thread_id):
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)

        set_remote_starred = ACTION_MOD_FOR[account.provider]. \
            set_remote_starred
        set_remote_starred(account, thread_id, True, db_session)


def unstar(account_id, thread_id):
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)

        set_remote_starred = ACTION_MOD_FOR[account.provider]. \
            set_remote_starred
        set_remote_starred(account, thread_id, False, db_session)


def mark_unread(account_id, thread_id):
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)
        set_remote_unread = ACTION_MOD_FOR[account.provider]. \
            set_remote_unread
        set_remote_unread(account, thread_id, True, db_session)


def mark_read(account_id, thread_id):
    with session_scope() as db_session:
        account = db_session.query(Account).get(account_id)
        set_remote_unread = ACTION_MOD_FOR[account.provider]. \
            set_remote_unread
        set_remote_unread(account, thread_id, False, db_session)


def mark_spam(account_id, thread_id):
    raise NotImplementedError


def unmark_spam(account_id, thread_id):
    raise NotImplementedError


def mark_trash(account_id, thread_id):
    raise NotImplementedError


def unmark_trash(account_id, thread_id):
    raise NotImplementedError


# Later we're going to want to consider a pooling mechanism. We may want to
# split actions queues by remote host, for example, and have workers for a
# given host share a connection pool.
def rqworker(burst=False):
    """ Runs forever unless burst=True.

    More details on how workers work at: http://python-rq.org/docs/workers/
    """
    register_backends()

    with Connection():
        q = get_queue()

        w = GeventWorker([q])
        w.work(burst=burst)
