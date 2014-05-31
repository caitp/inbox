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
from redis import StrictRedis
from rq import Queue, Connection

from inbox.util.misc import load_modules
from inbox.server.util.concurrency import GeventWorker
from inbox.server.models.tables.base import Account
from inbox.server.config import config
import inbox.server.actions

ACTION_MODULES = {}


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
            ACTION_MODULES[provider] = module


def get_queue():
    # The queue label is set via config to allow multiple distinct Inbox
    # instances to hit the same Redis server without interfering with each
    # other.
    host = config.get('REDIS_HOST', None)
    port = config.get('REDIS_PORT', None)
    db = config.get('ACTIONS_REDIS_DB', None)
    assert host and port and db, \
        "Must set REDIS_HOST, REDIS_PORT, and ACTIONS_REDIS_DB in config.cfg"
    label = config.get('ACTIONS_QUEUE_LABEL', None)
    assert label, "Must set ACTIONS_QUEUE_LABEL in config.cfg"
    return Queue(label, connection=StrictRedis(host=host, port=port, db=db))


def archive(db_session, account_id, thread_id):
    """ Archive thread locally and also sync back to the backend. """
    account = db_session.query(Account).get(account_id)

    # make local change
    local_archive = ACTION_MODULES[account.provider].local_archive
    local_archive(db_session, account, thread_id)

    # sync it to the account backend
    q = get_queue()
    remote_archive = ACTION_MODULES[account.provider].remote_archive
    q.enqueue(remote_archive, account.id, thread_id)


def set_unread(db_session, account, thread, unread):
    ACTION_MODULES[account.provider].set_local_unread(
        db_session, account, thread, unread)

    q = get_queue()
    set_remote_unread = ACTION_MODULES[account.provider]. \
        set_remote_unread
    q.enqueue(set_remote_unread, account.id, thread.id, unread)


def move(db_session, account_id, thread_id, from_folder, to_folder):
    """ Move thread locally and also sync back to the backend. """
    account = db_session.query(Account).get(account_id)

    # make local change
    local_move = ACTION_MODULES[account.provider].local_move
    local_move(db_session, account, thread_id, from_folder, to_folder)

    # sync it to the account backend
    q = get_queue()
    remote_move = ACTION_MODULES[account.provider].remote_move
    q.enqueue(remote_move, account.id, thread_id, from_folder, to_folder)

    # XXX TODO register a failure handler that reverses the local state
    # change if the change fails to go through?


def copy(db_session, account_id, thread_id, from_folder, to_folder):
    """ Copy thread locally and also sync back to the backend. """
    account = db_session.query(Account).get(account_id)

    # make local change
    local_copy = ACTION_MODULES[account.provider].local_copy
    # make local change
    local_copy(db_session, account, thread_id, from_folder, to_folder)

    # sync it to the account backend
    q = get_queue()
    remote_copy = ACTION_MODULES[account.provider].remote_copy
    q.enqueue(remote_copy, account.id, thread_id, from_folder, to_folder)

    # XXX TODO register a failure handler that reverses the local state
    # change if the change fails to go through?


def delete(db_session, account_id, thread_id, folder_name):
    """ Delete thread locally and also sync back to the backend.

    This really just removes the entry from the folder. Message data that
    no longer belongs to any messages is garbage-collected asynchronously.
    """
    account = db_session.query(Account).get(account_id)

    # make local change
    local_delete = ACTION_MODULES[account.provider].local_delete
    local_delete(db_session, account, thread_id, folder_name)

    # sync it to the account backend
    q = get_queue()
    remote_delete = ACTION_MODULES[account.provider].remote_delete
    q.enqueue(remote_delete, account.id, thread_id, folder_name)

    # XXX TODO register a failure handler that reverses the local state
    # change if the change fails to go through?


def save_draft(db_session, log, account_id, draftmsg):
    """ Save draft locally and also sync back to the backend. """
    register_backends()

    account = db_session.query(Account).get(account_id)

    # Make local change
    local_save_draft = ACTION_MODULES[account.provider].local_save_draft
    imapuid = local_save_draft(db_session, log, account.id,
                               account.drafts_folder.name, draftmsg)

    # Sync it to the account backend
    q = get_queue()
    remote_save_draft = ACTION_MODULES[account.provider].remote_save_draft
    q.enqueue(remote_save_draft, account.id, account.drafts_folder.name,
              draftmsg.msg, draftmsg.flags, draftmsg.date)

    # XXX TODO register a failure handler that reverses the local state
    # change if the change fails to go through?

    return imapuid


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
