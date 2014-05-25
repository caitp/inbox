from collections import defaultdict
import gevent
from sqlalchemy import asc, or_, func

from inbox.server.log import get_logger, log_uncaught_errors
from inbox.server.models import session_scope
from inbox.server.models.tables.base import Tag, Thread, Transaction
from inbox.server.actions.base import (get_queue, mark_read, mark_unread,
                                       archive, unarchive, star, unstar)


class ActionRegistry(object):
    """Keeps track of which actions to perform when a tag is applied to or
    removed from a thread."""
    def __init__(self):
        self._actions_on_apply = defaultdict(set)
        self._actions_on_remove = defaultdict(set)

    def __contains__(self, tag_public_id):
        return (tag_public_id in self._actions_on_apply or tag_public_id in
                self._actions_on_remove)

    def register_action(self, tag_public_id, apply_action, remove_action):
        """Register actions to execute when a tag add/remove event is
        processed.

        Parameters
        ----------
        tag_public_id: string
        apply_action, remove_action: function or None
            The functions to execute. Either may be None if nothing should
            execute. Each function should take a single argument that is the id
            of the thread to act on.
        """
        if apply_action is not None:
            self._actions_on_apply[tag_public_id].add(apply_action)
        if remove_action is not None:
            self._actions_on_remove[tag_public_id].add(remove_action)

    def on_apply(self, tag_public_id):
        """Returns the set of actions to execute when the tag with given public
        id is applied to a thread."""
        return self._actions_on_apply[tag_public_id]

    def on_remove(self, tag_public_id):
        """Returns the set of actions to execute when the tag with given public
        id is removed from a thread."""
        return self._actions_on_remove[tag_public_id]


class ListenerService(gevent.Greenlet):
    """Asynchronously consumes the transaction log and executes actions based
    on tag mutations."""

    def __init__(self, poll_interval=1, chunk_size=22, run_immediately=True):

        self.log = get_logger(purpose='actions')
        self.actions = ActionRegistry()
        self.queue = get_queue()

        self.poll_interval = poll_interval
        self.chunk_size = chunk_size
        with session_scope() as db_session:
            # Just start working from the head of the log.
            # TODO(emfree): once we can do retry, persist a pointer into the
            # transaction log and advance it only on syncback success.
            self.minimum_id, = db_session.query(
                func.max(Transaction.id)).first() or -1
        gevent.Greenlet.__init__(self)
        if run_immediately:
            self.start()

    def _process_log(self):
        with session_scope() as db_session:
            self.log.info('Processing log from entry {}'.
                          format(self.minimum_id))
            query = db_session.query(Transaction). \
                filter(or_(Transaction.table_name == 'thread',
                           Transaction.table_name == 'imapthread'),
                       Transaction.id > self.minimum_id). \
                order_by(asc(Transaction.id)).yield_per(self.chunk_size)

            for transaction in query:
                thread = db_session.query(Thread).get(transaction.record_id)
                account_id = thread.namespace.account_id

                tagitems = transaction.delta.get('tagitems')
                if tagitems is None:
                    continue
                added_tag_ids = [entry['tag_id'] for entry in tagitems['added']
                                 if entry['action_pending']]
                removed_tag_ids = [entry['tag_id'] for entry in tagitems['deleted']
                                   if entry['action_pending']]
                for tag_id in added_tag_ids:
                    tag = db_session.query(Tag).get(tag_id)
                    for action in self.actions.on_apply(tag.public_id):
                        self.queue.enqueue(action, account_id, thread.id)

                for tag_id in removed_tag_ids:
                    tag = db_session.query(Tag).get(tag_id)
                    for action in self.actions.on_remove(tag.public_id):
                        # TODO(emfree): should have some notion of retrying
                        # failed syncback actions here.
                        self.queue.enqueue(action, account_id, thread.id)
                self.minimum_id = transaction.id

    def register_default_actions(self):
        self.actions.register_action('unread', mark_unread, mark_read)
        self.actions.register_action('archive', archive, unarchive)
        self.actions.register_action('starred', star, unstar)
        # TODO(emfree) Also support marking trash and spam.

    def _run_impl(self):
        self.log.info('Starting action service')
        self.register_default_actions()
        while True:
            self._process_log()
            gevent.sleep(self.poll_interval)

    def _run(self):
        log_uncaught_errors(self._run_impl, self.log)()
