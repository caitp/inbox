""" IMAPClient wrapper for Inbox.

Unfortunately, due to IMAP's statefulness, to implement connection pooling we
have to shunt off dealing with the connection pool to the caller or we'll end
up trying to execute calls with the wrong folder selected some amount of the
time. That's why functions take a connection argument.
"""
import imaplib
import functools

from collections import namedtuple

from gevent import socket

import geventconnpool

from inbox.util.misc import or_none, timed
from inbox.server.auth.base import verify_imap_account
from inbox.server.auth.gmail import verify_gmail_account
from inbox.server.auth.yahoo import verify_yahoo_account
from inbox.server.basicauth import AUTH_TYPES
from inbox.server.models import session_scope
from inbox.server.models.tables.imap import ImapAccount
from inbox.server.log import get_logger
log = get_logger()

__all__ = ['CrispinClient', 'GmailCrispinClient', 'YahooCrispinClient']

# Unify flags API across IMAP and Gmail
Flags = namedtuple("Flags", "flags")
# Flags includes labels on Gmail because Gmail doesn't use \Draft.
GmailFlags = namedtuple("GmailFlags", "flags labels")

GMetadata = namedtuple('GMetadata', 'msgid thrid')
RawMessage = namedtuple(
    'RawImapMessage',
    'uid internaldate flags body g_thrid g_msgid g_labels created')


def connection_pool(account_id, pool_size=4, connection_pool_for=dict()):
    """ Per-account crispin connection pool.

    Use like this:

        with crispin.connection_pool(account_id).get() as crispin_client:
            # your code here
            pass

    Note that the returned CrispinClient could have ANY folder selected, or
    none at all! It's up to the calling code to handle folder sessions
    properly. We don't reset to a certain select state because it's slow.
    """
    pool = connection_pool_for.get(account_id)
    if pool is None:
        pool = connection_pool_for[account_id] \
            = CrispinConnectionPool(account_id, num_connections=pool_size,
                                    readonly=True)
    return pool


def writable_connection_pool(account_id, pool_size=2,
                             connection_pool_for=dict()):
    """ Per-account crispin connection pool, with *read-write* connections.

    Use like this:

        with crispin.writable_connection_pool(account_id).get() as crispin_client:
            # your code here
            pass
    """
    pool = connection_pool_for.get(account_id)
    if pool is None:
        pool = connection_pool_for[account_id] \
            = CrispinConnectionPool(account_id, num_connections=pool_size,
                                    readonly=False)
    return pool

CONN_DISCARD_EXC_CLASSES = (socket.error, imaplib.IMAP4.error)


class CrispinConnectionPool(geventconnpool.ConnectionPool):
    """
    Connection pool for Crispin clients.

    Connections in a pool are specific to an IMAPAccount.

    Parameters
    ----------
    account_id : int
        Which IMAPAccount to open up a connection to.
    num_connections : int
        How many connections in the pool.
    readonly : bool
        Is the connection to the IMAP server read-only?
    """
    def __init__(self, account_id, num_connections, readonly):
        log.info('Creating Crispin connection pool for account {} with {} '
                 'connections'.format(account_id, num_connections))
        self.account_id = account_id
        self.readonly = readonly
        self._set_account_info()
        # 1200s == 20min
        geventconnpool.ConnectionPool.__init__(
            self, num_connections, keepalive=1200,
            exc_classes=CONN_DISCARD_EXC_CLASSES)

    def _set_account_info(self):
        with session_scope() as db_session:
            account = db_session.query(ImapAccount).get(self.account_id)

            # Refresh token if need be, for OAuthed accounts
            if AUTH_TYPES.get(account.provider) == 'OAuth':
                account = verify_imap_account(db_session, account)
                self.o_access_token = account.o_access_token

            self.email_address = account.email_address
            self.provider = account.provider

    # TODO: simplify auth flow, preferably not need to use the db in this mod
    def _new_connection(self):
        with session_scope() as db_session:
            account = db_session.query(ImapAccount).get(self.account_id)

            if (account.provider == 'Gmail'):
                conn = verify_gmail_account(account)

            elif (account.provider == 'Yahoo'):
                conn = verify_yahoo_account(account)

            # Reads from db, therefore shouldn't get here
            else:
                raise

        return new_crispin(self.account_id, self.provider, conn, self.readonly)

    def _keepalive(self, c):
        c.conn.noop()


retry_crispin = functools.partial(
    geventconnpool.retry, exc_classes=CONN_DISCARD_EXC_CLASSES, logger=log,
    interval=5, max_failures=5)


def new_crispin(account_id, provider, conn, readonly=True):
    crispin_module_for = dict(Gmail=GmailCrispinClient, IMAP=CrispinClient,
                              Yahoo=YahooCrispinClient)

    cls = crispin_module_for[provider]
    return cls(account_id, conn, readonly=readonly)


class CrispinClient(object):
    """ Generic IMAP client wrapper.

    One thing to note about crispin clients is that *all* calls operate on
    the currently selected folder.

    Crispin will NEVER implicitly select a folder for you.

    This is very important! IMAP only guarantees that folder message UIDs
    are valid for a "session", which is defined as from the time you
    SELECT a folder until the connection is closed or another folder is
    selected.

    Crispin clients *always* return long ints rather than strings for number
    data types, such as message UIDs, Google message IDs, and Google thread
    IDs.

    All inputs are coerced to strings before being passed off to the IMAPClient
    connection.

    You should really be interfacing with this class via a connection pool,
    see `connection_pool()`.

    Parameters
    ----------
    account_id : int
        Database id of the associated IMAPAccount.
    conn : IMAPClient
        Open IMAP connection (should be already authed).
    readonly : bool
        Whether or not to open IMAP connections as readonly.
    """
    PROVIDER = 'IMAP'
    # NOTE: Be *careful* changing this! Downloading too much at once may
    # cause memory errors that only pop up in extreme edge cases.
    CHUNK_SIZE = 1

    def __init__(self, account_id, conn, readonly=True):
        self.log = get_logger(account_id)
        self.account_id = account_id
        # IMAP isn't stateless :(
        self.selected_folder = None
        self._folder_names = None
        self.conn = conn
        self.readonly = readonly

    def select_folder(self, folder, uidvalidity_cb):
        """ Selects a given folder.

        Makes sure to set the 'selected_folder' attribute to a
        (folder_name, select_info) pair.

        Selecting a folder indicates the start of an IMAP session.  IMAP UIDs
        are only guaranteed valid for sessions, so the caller must provide a
        callback that checks UID validity.

        Starts a new session even if `folder` is already selected, since
        this does things like e.g. makes sure we're not getting
        cached/out-of-date values for HIGHESTMODSEQ from the IMAP server.
        """
        select_info = self.conn.select_folder(
            folder, readonly=self.readonly)
        select_info['UIDVALIDITY'] = long(select_info['UIDVALIDITY'])
        select_info['HIGHESTMODSEQ'] = long(select_info['HIGHESTMODSEQ'])
        select_info['UIDNEXT'] = long(select_info['UIDNEXT'])
        self.selected_folder = (folder, select_info)
        # don't propagate cached information from previous session
        self._folder_names = None
        self.log.info('Selected folder {0} with {1} messages.'.format(
            folder, select_info['EXISTS']))
        return uidvalidity_cb(folder, select_info)

    @property
    def selected_folder_name(self):
        return or_none(self.selected_folder, lambda f: f[0])

    @property
    def selected_folder_info(self):
        return or_none(self.selected_folder, lambda f: f[1])

    @property
    def selected_highestmodseq(self):
        return or_none(self.selected_folder_info, lambda i: i['HIGHESTMODSEQ'])

    @property
    def selected_uidvalidity(self):
        return or_none(self.selected_folder_info, lambda i: i['UIDVALIDITY'])

    def sync_folders(self):
        # TODO: probabaly all of the folders
        raise NotImplementedError

    def folder_names(self):
        # TODO: parse out contents of non-Gmail-specific LIST
        raise NotImplementedError

    def _fetch_folder_list(self):
        """ NOTE: XLIST is deprecated, so we just use LIST.

        An example response with some other flags:

            * LIST (\HasNoChildren) "/" "INBOX"
            * LIST (\Noselect \HasChildren) "/" "[Gmail]"
            * LIST (\HasNoChildren \All) "/" "[Gmail]/All Mail"
            * LIST (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
            * LIST (\HasNoChildren \Important) "/" "[Gmail]/Important"
            * LIST (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
            * LIST (\HasNoChildren \Junk) "/" "[Gmail]/Spam"
            * LIST (\HasNoChildren \Flagged) "/" "[Gmail]/Starred"
            * LIST (\HasNoChildren \Trash) "/" "[Gmail]/Trash"

        IMAPClient parses this response into a list of
        (flags, delimiter, name) tuples.
        """
        folders = self.conn.list_folders()

        return folders

    def folder_status(self, folder):
        status = [long(val) for val in self.conn.folder_status(
            folder, ('UIDVALIDITY', 'HIGHESTMODSEQ', 'UIDNEXT'))]

        return status

    def next_uid(self, folder):
        status = self.folder_status(folder)
        return status['UIDNEXT']

    def search_uids(self, criteria):
        """ Find not-deleted UIDs in this folder matching the criteria.

        See http://tools.ietf.org/html/rfc3501.html#section-6.4.4 for valid
        criteria.
        """
        full_criteria = ['NOT DELETED']
        if isinstance(criteria, list):
            full_criteria.extend(criteria)
        else:
            full_criteria.append(criteria)
        return sorted([long(uid) for uid in self.conn.search(full_criteria)])

    def all_uids(self):
        """ Fetch all UIDs associated with the currently selected folder.

        Returns
        -------
        list
            UIDs as integers sorted in ascending order.
        """
        data = self.conn.search(['NOT DELETED'])
        return sorted([long(s) for s in data])

    @timed
    def new_and_updated_uids(self, modseq):
        return sorted([long(s) for s in self.conn.search(
            ['NOT DELETED', "MODSEQ {}".format(modseq)])])


class YahooCrispinClient(CrispinClient):
    """ NOTE: This implementation is NOT FINISHED. """
    def __init__(self, account_id, conn, readonly=True):
        # CrispinClient.__init__(self, account_id, conn, readonly=readonly)
        # TODO: Remove this once this client is usable.
        raise NotImplementedError

    def sync_folders(self):
        return self.folder_names()

    def flags(self, uids):
        uids = [str(u) for u in uids]
        data = self.conn.fetch(uids, ['FLAGS'])
        return dict([(long(uid), Flags(msg['FLAGS']))
                     for uid, msg in data.iteritems()])

    def folder_names(self):
        if self._folder_names is None:
            folders = self._fetch_folder_list()
            self._folder_names = [name for flags, delimiter, name in folders]
        return self._folder_names

    def uids(self, uids):
        uids = [str(u) for u in uids]
        raw_messages = self.conn.fetch(uids,
                                       ['BODY.PEEK[] INTERNALDATE FLAGS'])
        for uid, msg in raw_messages.iteritems():
            # NOTE: flanker needs encoded bytestrings as its input, since to
            # deal properly with MIME-encoded email you need to do part
            # decoding based on message / MIME part headers anyway. imapclient
            # tries to abstract away bytes and decodes all bytes received from
            # the wire as _latin-1_, which is wrong in any case where 8bit MIME
            # is used. so we have to reverse the damage before we proceed.
            #
            # We should REMOVE this XXX HACK XXX when we finish working with
            # Menno to fix this problem upstream.
            msg['BODY[]'] = msg['BODY[]'].encode('latin-1')

        messages = []
        for uid in sorted(raw_messages.iterkeys(), key=long):
            msg = raw_messages[uid]
            messages.append(RawMessage(uid=long(uid),
                                       internaldate=msg['INTERNALDATE'],
                                       flags=msg['FLAGS'],
                                       body=msg['BODY[]']))
        return messages

    def expand_threads(self, thread_ids):
        NotImplementedError


class GmailCrispinClient(CrispinClient):
    PROVIDER = 'Gmail'

    def sync_folders(self):
        """ Gmail-specific list of folders to sync.

        In Gmail, every message is a subset of All Mail, with the exception of
        the Trash and Spam folders. So we only sync All Mail, Trash, Spam,
        and Inbox (for quickly downloading initial inbox messages and
        continuing to receive new Inbox messages while a large mail archive is
        downloading).

        Returns
        -------
        list
            Folders to sync (as strings).
        """
        folders = [self.folder_names()['inbox'], self.folder_names()['all']]
        # Gmail allows users to configure which folders appear in the IMAP
        # interface, so accounts MAY NOT have these folders available to sync!
        for tag in ('trash', 'spam'):
            if tag in self.folder_names():
                folders.append(self.folder_names()[tag])
        return folders

    def flags(self, uids):
        """ Gmail-specific flags.

        Returns
        -------
        dict
            Mapping of `uid` (str) : GmailFlags.
        """
        uids = [str(u) for u in uids]
        data = self.conn.fetch(uids, ['FLAGS X-GM-LABELS'])
        return dict([(long(uid), GmailFlags(msg['FLAGS'], msg['X-GM-LABELS']))
                     for uid, msg in data.iteritems()])

    def folder_names(self):
        """ Parses out Gmail-specific folder names based on Gmail IMAP flags.

        If the user's account is localized to a different language, it will
        return the proper localized string.

        Caches the call since we use it all over the place and folders never
        change names during a session.
        """
        if self._folder_names is None:
            folders = self._fetch_folder_list()
            self._folder_names = dict()
            for flags, delimiter, name in folders:
                if u'\\Noselect' in flags:
                    # special folders that can't contain messages, usually
                    # just '[Gmail]'
                    pass
                elif '\\All' in flags:
                    self._folder_names['all'] = name
                elif name.lower() == 'inbox':
                    self._folder_names[name.lower()] = name.capitalize()
                    continue
                else:
                    for flag in ['\\Drafts', '\\Important', '\\Sent', '\\Junk',
                                 '\\Flagged', '\\Trash']:
                        # find localized names for Gmail's special folders
                        if flag in flags:
                            k = flag.replace('\\', '').lower()
                            if k == 'flagged':
                                self._folder_names['starred'] = name
                            elif k == 'junk':
                                self._folder_names['spam'] = name
                            else:
                                self._folder_names[k] = name
                            break
                    else:
                        # everything else is a label
                        self._folder_names.setdefault('labels', list())\
                            .append(name)
            if 'labels' in self._folder_names:
                self._folder_names['labels'].sort()
                # synonyms on Gmail
                self._folder_names['extra'] = self._folder_names['labels']
        return self._folder_names

    def uids(self, uids):
        uids = [str(u) for u in uids]
        raw_messages = self.conn.fetch(uids, ['BODY.PEEK[] INTERNALDATE FLAGS',
                                              'X-GM-THRID', 'X-GM-MSGID',
                                              'X-GM-LABELS'])
        for uid, msg in raw_messages.iteritems():
            # NOTE: flanker needs encoded bytestrings as its input, since to
            # deal properly with MIME-encoded email you need to do part
            # decoding based on message / MIME part headers anyway. imapclient
            # tries to abstract away bytes and decodes all bytes received from
            # the wire as _latin-1_, which is wrong in any case where 8bit MIME
            # is used. so we have to reverse the damage before we proceed.
            #
            # We should REMOVE this XXX HACK XXX when we finish working with
            # Menno to fix this problem upstream.
            msg['BODY[]'] = msg['BODY[]'].encode('latin-1')

        messages = []
        for uid in sorted(raw_messages.iterkeys(), key=long):
            msg = raw_messages[uid]
            messages.append(RawMessage(uid=long(uid),
                                       internaldate=msg['INTERNALDATE'],
                                       flags=msg['FLAGS'],
                                       body=msg['BODY[]'],
                                       g_thrid=long(msg['X-GM-THRID']),
                                       g_msgid=long(msg['X-GM-MSGID']),
                                       g_labels=msg['X-GM-LABELS'],
                                       created=False))
        return messages

    def g_metadata(self, uids):
        """ Download Gmail MSGIDs and THRIDs for the given messages.

        NOTE: only UIDs are guaranteed to be unique to a folder, X-GM-MSGID
        and X-GM-THRID may not be.

        Parameters
        ----------
        uids : list
            UIDs to fetch data for. Must be from the selected folder.

        Returns
        -------
        dict
            uid: GMetadata(msgid, thrid)
        """
        uids = [str(u) for u in uids]
        self.log.debug(
            "Fetching X-GM-MSGID and X-GM-THRID for {} uids."
            .format(len(uids)))
        return dict([(long(uid), GMetadata(long(ret['X-GM-MSGID']),
                                           long(ret['X-GM-THRID']))) for uid,
                     ret in self.conn.fetch(uids, ['X-GM-MSGID',
                                                   'X-GM-THRID']).iteritems()])

    def expand_threads(self, g_thrids):
        """ Find all message UIDs in this account with X-GM-THRID in g_thrids.

        Requires the "All Mail" folder to be selected.

        Returns
        -------
        list
            All Mail UIDs (as integers), sorted most-recent first.
        """
        assert self.selected_folder_name == self.folder_names()['all'], \
            "must select All Mail first ({})".format(
                self.selected_folder_name)
        criteria = ('OR ' * (len(g_thrids)-1)) + ' '.join(
            ['X-GM-THRID {}'.format(thrid) for thrid in g_thrids])
        uids = [long(uid) for uid in self.conn.search(['NOT DELETED',
                                                       criteria])]
        # UIDs ascend over time; return in order most-recent first
        return sorted(uids, reverse=True)

    def find_messages(self, g_thrid):
        """ Get UIDs for the [sub]set of messages belonging to the given thread
            that are in the current folder.
        """
        criteria = 'X-GM-THRID {}'.format(g_thrid)
        return sorted([long(uid) for uid in
                       self.conn.search(['NOT DELETED', criteria])])

    ### the following methods WRITE to the IMAP account!

    def archive_thread(self, g_thrid):
        assert self.selected_folder_name == self.folder_names()['inbox'], \
            "must select INBOX first ({0})".format(self.selected_folder_name)
        uids = self.find_messages(g_thrid)
        # delete from inbox == archive for Gmail
        if uids:
            self.conn.delete_messages(uids)

    def copy_thread(self, g_thrid, to_folder):
        """ NOTE: Does nothing if the thread isn't in the currently selected
            folder.
        """
        uids = self.find_messages(g_thrid)
        if uids:
            self.conn.copy(uids, to_folder)

    def add_label(self, g_thrid, label_name):
        """ NOTE: Does nothing if the thread isn't in the currently selected
            folder.
        """
        uids = self.find_messages(g_thrid)
        self.conn.add_gmail_labels(uids, [label_name])

    def remove_label(self, g_thrid, label_name):
        """ NOTE: Does nothing if the thread isn't in the currently selected
            folder.
        """
        # Gmail won't even include the label of the selected folder (when the
        # selected folder is a laebl) in the list of labels for a UID, FYI.
        assert self.selected_folder_name != label_name, \
            "Gmail doesn't support removing a selected label"
        uids = self.find_messages(g_thrid)
        self.conn.remove_gmail_labels(uids, [label_name])

    def set_unread(self, g_thrid, unread):
        uids = self.find_messages(g_thrid)
        if unread:
            self.conn.remove_flags(uids, '\\Seen')
        else:
            self.conn.add_flags(uids, '\\Seen')

    def save_draft(self, message, flags=None, date=None):
        self.selected_folder_name == self.folder_names()['drafts'], \
            'Must select drafts folder first ({0})'.format(
                self.selected_folder_name)

        self.conn.append(self.selected_folder_name, message, flags, date)
