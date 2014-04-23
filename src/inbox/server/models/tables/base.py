import os
import sys
import json
import traceback

from itertools import chain
from hashlib import sha256
from sqlalchemy import (Column, Integer, BigInteger, String, DateTime, Boolean,
                        Enum, ForeignKey, Text, func, event)
from sqlalchemy.orm import (reconstructor, relationship, backref, deferred,
                            validates)
from sqlalchemy.orm.collections import attribute_mapped_collection
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import BLOB
from sqlalchemy.sql.expression import true, false

from bs4 import BeautifulSoup, Doctype, Comment

from cryptography.fernet import Fernet

from inbox.server.log import get_logger
log = get_logger()

from inbox.server.config import config

from inbox.util.file import Lock, mkdirp
from inbox.util.html import plaintext2html
from inbox.util.misc import strip_plaintext_quote, load_modules
from inbox.sqlalchemy.util import JSON
from inbox.sqlalchemy.revision import Revision, gen_rev_role
from inbox.server.basicauth import AUTH_TYPES

from inbox.server.models.roles import JSONSerializable, Blob
from inbox.server.models import Base


def register_backends():
    import inbox.server.models.tables

    # Find and import
    modules = load_modules(inbox.server.models.tables)

    # Create mapping
    table_mod_for = {}
    for module in modules:
        if hasattr(module, 'PROVIDER'):
            provider = module.PROVIDER
            table_mod_for[provider] = module

    return table_mod_for


# global
class Account(Base):
    # user_id refers to Inbox's user id
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'),
                     nullable=False)
    user = relationship('User', backref='accounts')

    # http://stackoverflow.com/questions/386294/what-is-the-maximum-length-of-a-valid-email-address
    email_address = Column(String(254), nullable=True, index=True)
    provider = Column(Enum('Gmail', 'Outlook', 'Yahoo', 'EAS', 'Inbox'),
                      nullable=False)

    # local flags & data
    save_raw_messages = Column(Boolean, server_default=true())

    sync_host = Column(String(255), nullable=True)
    last_synced_contacts = Column(DateTime, nullable=True)

    # 'Required' folder name mappings for the Inbox datastore API
    inbox_folder_name = Column(String(255), nullable=True)
    drafts_folder_name = Column(String(255), nullable=True)
    # NOTE: Spam, Trash might be added later

    # Optional folder name mappings
    archive_folder_name = Column(String(255), nullable=True)
    sent_folder_name = Column(String(255), nullable=True)

    @property
    def sync_active(self):
        return self.sync_host is not None

    # oauth stuff (most providers support oauth at this point, shockingly)
    # TODO figure out the actual lengths of these
    # XXX we probably don't actually need to save all of this crap
    # XXX encrypt some of this crap?
    o_token_issued_to = Column(String(512))
    o_user_id = Column(String(512))
    o_access_token = Column(String(1024))
    o_id_token = Column(String(1024))
    o_expires_in = Column(Integer)
    o_access_type = Column(String(512))
    o_token_type = Column(String(512))
    o_audience = Column(String(512))
    o_scope = Column(String(512))
    o_refresh_token = Column(String(512))
    o_verified_email = Column(Boolean)

    # used to verify key lifespan
    date = Column(DateTime)

    # 'deferred' loads these large binary fields into memory only when needed
    # i.e. on direct access.
    encrypted_password = deferred(Column(BLOB(256)))
    encryption_key = deferred(Column(BLOB(128)))

    @classmethod
    def _get_lock_object(cls, account_id, lock_for=dict()):
        """ Make sure we only create one lock per account per process.

        (Default args are initialized at import time, so `lock_for` acts as a
        module-level memory cache.)
        """
        return lock_for.setdefault(account_id,
                                   Lock(cls._sync_lockfile_name(account_id),
                                        block=False))

    @classmethod
    def _sync_lockfile_name(cls, account_id):
        return "/var/lock/inbox_sync/{}.lock".format(account_id)

    @property
    def _sync_lock(self):
        return self._get_lock_object(self.id)

    def sync_lock(self):
        """ Prevent mailsync for this account from running more than once. """
        self._sync_lock.acquire()

    def sync_unlock(self):
        self._sync_lock.release()

    @property
    def password(self):
        if self.encrypted_password is not None:
            with open(self._keyfile, 'r') as f:
                key = f.read()

            key = self.encryption_key + key
            f = Fernet(key)
            return f.decrypt(self.encrypted_password)

    @password.setter
    def password(self, value):
        assert AUTH_TYPES.get(self.provider) == 'Password'
        assert value is not None

        key = Fernet.generate_key()
        f = Fernet(key)

        self.encrypted_password = f.encrypt(value)
        fs_key_half, db_key_half = key[len(key)/2:], key[:len(key)/2]
        self.encryption_key = db_key_half

        with open(self._keyfile, 'w+') as f:
            f.write(fs_key_half)

    @property
    def _keyfile(self, create_dir=True):
        assert self.encryption_key

        key_dir = config.get('KEY_DIR', None)
        assert key_dir
        if create_dir:
            mkdirp(key_dir)
        key_filename = '{}'.format(sha256(self.encryption_key).hexdigest())
        return os.path.join(key_dir, key_filename)

    discriminator = Column('type', String(16))
    __mapper_args__ = {'polymorphic_on': discriminator,
                       'polymorphic_identity': 'account'}


class UserSession(Base):
    """ Inbox-specific sessions. """
    token = Column(String(40))

    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'),
                     nullable=False)
    user = relationship('User', backref='sessions')


class Namespace(Base):
    """ A way to do grouping / permissions, basically. """
    # NOTE: only root namespaces have IMAP accounts
    account_id = Column(Integer,
                        ForeignKey('account.id', ondelete='CASCADE'),
                        nullable=True)
    # really the root_namespace
    account = relationship('Account',
                           backref=backref('namespace', uselist=False))

    # invariant: imapaccount is non-null iff type is root
    type = Column(Enum('root', 'shared_folder'), nullable=False,
                  server_default='root')

    @property
    def email_address(self):
        if self.account is not None:
            return self.account.email_address

    def cereal(self):
        return dict(id=self.id, type=self.type)


class SharedFolder(Base):
    # Don't delete shared folders if the user that created them is deleted.
    user_id = Column(Integer, ForeignKey('user.id', ondelete='SET NULL'),
                     nullable=True)
    user = relationship('User', backref='sharedfolders')

    namespace = relationship('Namespace', backref='sharedfolders')
    # Do delete shared folders if their associated namespace is deleted.
    namespace_id = Column(Integer, ForeignKey(
        'namespace.id', ondelete='CASCADE'), nullable=False)

    display_name = Column(String(40))

    def cereal(self):
        return dict(id=self.id, name=self.display_name)


class User(Base):
    name = Column(String(255))

# sharded (by namespace)


class Transaction(Base, Revision):
    """ Transactional log to enable client syncing. """
    # Do delete transactions if their associated namespace is deleted.
    namespace_id = Column(Integer,
                          ForeignKey('namespace.id', ondelete='CASCADE'),
                          nullable=False)
    namespace = relationship('Namespace')

    def set_extra_attrs(self, obj):
        try:
            self.namespace = obj.namespace
        except AttributeError:
            log.info("Couldn't create {2} revision for {0}:{1}".format(
                self.table_name, self.record_id, self.command))
            log.info("Delta is {0}".format(self.delta))
            log.info("Thread is: {0}".format(obj.thread_id))
            raise

HasRevisions = gen_rev_role(Transaction)


class SearchToken(Base):
    """A token to prefix-match against for contacts search.
    Right now these tokens consist of:
    - the contact's full name
    - the elements of the contact's name when split by whitespace
    - the contact's email address.
    """
    token = Column(String(255))
    source = Column('source', Enum('name', 'email_address'))
    contact_id = Column(ForeignKey('contact.id', ondelete='CASCADE'))
    contact = relationship('Contact', backref='token', cascade='all',
                           single_parent=True)


class SearchSignal(Base):
    """Represents a signal used for contacts search result ranking. Examples of
    signals might include number of emails sent to or received from this
    contact, or time since last interaction with the contact."""
    name = Column(String(40))
    value = Column(Integer)
    contact_id = Column(ForeignKey('contact.id', ondelete='CASCADE'),
                        nullable=False)


class Contact(Base, HasRevisions):
    """Data for a user's contact."""
    account_id = Column(ForeignKey('account.id', ondelete='CASCADE'),
                        nullable=False)
    account = relationship('Account', load_on_pending=True)

    # A server-provided unique ID.
    uid = Column(String(64), nullable=False)
    # A constant, unique identifier for the remote backend this contact came
    # from. E.g., 'google', 'EAS', 'inbox'
    provider_name = Column(String(64))

    # We essentially maintain two copies of a user's contacts.
    # The contacts with source 'remote' give the contact data as it was
    # immediately after the last sync with the remote provider.
    # The contacts with source 'local' also contain any subsequent local
    # modifications to the data.
    source = Column('source', Enum('local', 'remote'))

    email_address = Column(String(254), nullable=True, index=True)
    name = Column(Text)
    # phone_number = Column(String(64))

    raw_data = Column(Text)
    search_signals = relationship(
        'SearchSignal', cascade='all',
        collection_class=attribute_mapped_collection('name'))

    # A score to use for ranking contact search results. This should be
    # precomputed to facilitate performant search.
    score = Column(Integer)

    updated_at = Column(DateTime, default=func.now(),
                        onupdate=func.current_timestamp())
    created_at = Column(DateTime, default=func.now())

    # Flag to set if the contact is deleted in a remote backend.
    # (This is an unmapped attribute, i.e., it does not correspond to a
    # database column.)
    deleted = False

    __table_args__ = (UniqueConstraint('uid', 'source', 'account_id',
                                       'provider_name'),)

    @property
    def namespace(self):
        return self.account.namespace

    def cereal(self):
        return dict(id=self.id,
                    email=self.email_address,
                    name=self.name)

    def from_cereal(self, args):
        self.name = args['name']
        self.email_address = args['email']

    def __repr__(self):
        # XXX this won't work properly with unicode (e.g. in the name)
        return ('Contact({}, {}, {}, {}, {}, {})'
                .format(self.uid, self.name, self.email_address, self.source,
                        self.provider_name, self.deleted))

    def copy_from(self, src):
        """ Copy fields from src."""
        self.account_id = src.account_id
        self.account = src.account
        self.uid = src.uid
        self.name = src.name
        self.email_address = src.email_address
        self.provider_name = src.provider_name
        self.raw_data = src.raw_data

    @validates('name', include_backrefs=False)
    def tokenize_name(self, key, name):
        """ Update the associated search tokens whenever the contact's name is
        updated."""
        new_tokens = []
        # Delete existing 'name' tokens
        self.token = [token for token in self.token if token.source != 'name']
        if name is not None:
            new_tokens.extend(name.split())
            new_tokens.append(name)
            self.token.extend(SearchToken(token=token, source='name') for token
                              in new_tokens)
        return name

    @validates('email_address', include_backrefs=False)
    def tokenize_email_address(self, key, email_address):
        """ Update the associated search tokens whenever the contact's email
        address is updated."""
        self.token = [token for token in self.token if token.source !=
                      'email_address']
        if email_address is not None:
            new_token = SearchToken(token=email_address,
                                    source='email_address')
            self.token.append(new_token)
        return email_address


class Message(JSONSerializable, Base, HasRevisions):
    # XXX clean this up a lot - make a better constructor, maybe taking
    # a flanker object as an argument to prefill a lot of attributes

    # Do delete messages if their associated thread is deleted.
    thread_id = Column(Integer, ForeignKey('thread.id', ondelete='CASCADE'),
                       nullable=False)
    thread = relationship('Thread', backref=backref('messages'),
                          order_by='Message.received_date')

    from_addr = Column(JSON, nullable=True)
    sender_addr = Column(JSON, nullable=True)
    reply_to = Column(JSON, nullable=True)
    to_addr = Column(JSON, nullable=True)
    cc_addr = Column(JSON, nullable=True)
    bcc_addr = Column(JSON, nullable=True)
    in_reply_to = Column(JSON, nullable=True)
    message_id_header = Column(String(255), nullable=True)
    subject = Column(Text, nullable=True)
    received_date = Column(DateTime, nullable=False)
    size = Column(Integer, nullable=False)
    data_sha256 = Column(String(255), nullable=True)

    mailing_list_headers = Column(JSON, nullable=True)

    is_draft = Column(Boolean, server_default=false(), nullable=False)

    # Most messages are short and include a lot of quoted text. Preprocessing
    # just the relevant part out makes a big difference in how much data we
    # need to send over the wire.
    # Maximum length is determined by typical email size limits (25 MB body +
    # attachments on Gmail), assuming a maximum # of chars determined by
    # 1-byte (ASCII) chars.
    # NOTE: always HTML :)
    sanitized_body = Column(Text(length=26214400), nullable=False)
    snippet = Column(String(191), nullable=False)

    # we had to replace utf-8 errors before writing... this might be a
    # mail-parsing bug, or just a message from a bad client.
    decode_error = Column(Boolean, server_default=false(), nullable=False)

    # only on messages from Gmail
    g_msgid = Column(BigInteger, nullable=True, index=True)
    g_thrid = Column(BigInteger, nullable=True, index=True)

    # The uid as set in the X-INBOX-ID header of a sent message we create
    inbox_uid = Column(String(64), nullable=True)

    @property
    def namespace(self):
        return self.thread.namespace

    def calculate_sanitized_body(self):
        plain_part, html_part = self.body
        snippet_length = 191
        if html_part:
            assert '\r' not in html_part, "newlines not normalized"

            # Rudimentary stripping out quoted text in 'gmail_quote' div
            # Wrap this in a try/catch because sometimes BeautifulSoup goes
            # down a dark spiral of recursion death
            try:
                soup = BeautifulSoup(html_part.strip(), "lxml")
                for div in soup.findAll('div', 'gmail_quote'):
                    div.extract()
                for container in soup.findAll('div', 'gmail_extra'):
                    if container.contents is not None:
                        for tag in reversed(container.contents):
                            if not hasattr(tag, 'name') or tag.name != 'br':
                                break
                            else:
                                tag.extract()
                    if container.contents is None:
                        # we emptied it!
                        container.extract()

                # Paragraphs don't need trailing line-breaks.
                for container in soup.findAll('p'):
                    if container.contents is not None:
                        for tag in reversed(container.contents):
                            if not hasattr(tag, 'name') or tag.name != 'br':
                                break
                            else:
                                tag.extract()

                # Misc other crap.
                dtd = [item for item in soup.contents if isinstance(
                    item, Doctype)]
                comments = soup.findAll(text=lambda text: isinstance(
                    text, Comment))
                for tag in chain(dtd, comments):
                    tag.extract()
                self.sanitized_body = unicode(soup)

                # trim for snippet
                for tag in soup.findAll(['style', 'head', 'title']):
                    tag.extract()
                self.snippet = soup.get_text(' ')[:191]

            except RuntimeError as exc:
                err_prefix = 'maximum recursion depth exceeded'
                # e.message is deprecated in Python 3
                if exc.args[0].startswith(err_prefix):
                    full_traceback = 'Ignoring error: {}\nOuter stack:\n{}{}'\
                        .format(exc, ''.join(traceback.format_stack()[:-2]),
                                traceback.format_exc(exc))

                    # Note that python doesn't support tail call recursion
                    # optimizations
                    # http://neopythonic.blogspot.com/2009/04/tail-recursion-elimination.html
                    full_traceback = 'Error in BeautifulSoup.' + \
                        'System recursion limit: {0}'.format(
                            sys.getrecursionlimit()) + \
                        '\n\n\n' + \
                        full_traceback

                    # TODO have a better logging service for storing these
                    errdir = os.path.join(config['LOGDIR'],
                                          'bs_parsing_errors', )
                    errfile = os.path.join(errdir, str(self.data_sha256))
                    mkdirp(errdir)

                    with open("{0}_traceback".format(errfile), 'w') as fh:
                        fh.write(full_traceback)
                    # Write the file in binary mode, since it might also have
                    # decoding errors.
                    with open("{0}_data".format(errfile), 'wb') as fh:
                        fh.write(html_part.encode("utf-8"))

                    log.error("BeautifulSoup parsing error. Data logged to\
                              {0}_data and {0}_traceback".format(errfile))
                    self.decode_error = True

                    # Not sanitized, but will still work
                    self.sanitized_body = html_part
                    self.snippet = soup.get_text(' ')[:191]

                else:
                    log.error("Unknown BeautifulSoup exception: {0}".format(
                        exc))
                    raise exc

        elif plain_part is None:
            self.sanitized_body = u''
            self.snippet = u''
        else:
            stripped = strip_plaintext_quote(plain_part.strip())
            self.sanitized_body = plaintext2html(stripped)
            self.snippet = stripped[:snippet_length]

    @property
    def body(self):
        """ Returns (plaintext, html) body for the message, decoded. """
        assert self.parts, \
            "Can't calculate body before parts have been parsed"

        plain_data = None
        html_data = None

        for part in self.parts:
            if part.content_type == 'text/html':
                html_data = part.get_data().decode('utf-8')
                break
        for part in self.parts:
            if part.content_type == 'text/plain':
                plain_data = part.get_data().decode('utf-8')
                break

        return plain_data, html_data

    def trimmed_subject(self):
        s = self.subject
        if s[:4] == u'RE: ' or s[:4] == u'Re: ':
            s = s[4:]
        return s

    @property
    def prettified_body(self):
        html_data = self.sanitized_body

        prettified = None
        if 'font:' in html_data or 'font-face:' \
                in html_data or 'font-family:' in html_data:
            prettified = html_data
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "message_template.html")
            with open(path, 'r') as f:
                # template has %s in it. can't do format because python
                # misinterprets css
                prettified = f.read() % html_data

        return prettified

    def cereal(self):
        # TODO serialize more here for client API
        d = {}
        d['from'] = self.from_addr
        d['to'] = self.to_addr
        d['date'] = self.received_date
        d['subject'] = self.subject
        d['id'] = self.id
        d['thread_id'] = self.thread_id
        d['snippet'] = self.snippet
        d['mailing_list_info'] = self.mailing_list_headers
        return d

    @property
    def mailing_list_info(self):
        return self.mailing_list_headers

    @property
    def headers(self):
        """ Returns headers for the message, decoded. """
        assert self.parts, \
            "Can't provide headers before parts have been parsed"

        headers = self.parts[0].get_data()
        json_headers = json.JSONDecoder().decode(headers)

        return json_headers

    discriminator = Column('type', String(16))
    __mapper_args__ = {'polymorphic_on': discriminator,
                       'polymorphic_identity': 'message'}


class SpoolMessage(Message):
    id = Column(Integer, ForeignKey('message.id', ondelete='CASCADE'),
                primary_key=True)

    created_date = Column(DateTime)
    is_sent = Column(Boolean, server_default=false(), nullable=False)

    # Null till reconciled.
    resolved_message_id = Column(Integer,
                                 ForeignKey('message.id', ondelete='CASCADE'),
                                 nullable=True)
    resolved_message = relationship(
        'Message',
        primaryjoin='SpoolMessage.resolved_message_id==Message.id',
        backref=backref('spooled_message', uselist=False))

    __mapper_args__ = {'polymorphic_identity': 'spoolmessage',
                       'inherit_condition': id == Message.id}


# These are the top 15 most common Content-Type headers
# in my personal mail archive. --mg
common_content_types = ['text/plain',
                        'text/html',
                        'multipart/alternative',
                        'multipart/mixed',
                        'image/jpeg',
                        'multipart/related',
                        'application/pdf',
                        'image/png',
                        'image/gif',
                        'application/octet-stream',
                        'multipart/signed',
                        'application/msword',
                        'application/pkcs7-signature',
                        'message/rfc822',
                        'image/jpg']


class Block(JSONSerializable, Blob, Base, HasRevisions):
    """ Metadata for message parts stored in s3 """
    message_id = Column(Integer, ForeignKey('message.id', ondelete='CASCADE'),
                        nullable=False)
    message = relationship('Message',
                           backref=backref(
                               "parts", cascade="all, delete, delete-orphan"))

    walk_index = Column(Integer)
    # Save some space with common content types
    _content_type_common = Column(Enum(*common_content_types))
    _content_type_other = Column(String(255))
    filename = Column(String(255))

    content_disposition = Column(Enum('inline', 'attachment'))
    content_id = Column(String(255))  # For attachments
    misc_keyval = Column(JSON)

    is_inboxapp_attachment = Column(Boolean, server_default=false())

    # TODO: create a constructor that allows the 'content_type' keyword

    __table_args__ = (UniqueConstraint('message_id', 'walk_index',
                      'data_sha256'),)

    def __init__(self, *args, **kwargs):
        self.content_type = None
        self.size = 0
        Base.__init__(self, *args, **kwargs)

    def __repr__(self):
        return 'Block: %s' % self.__dict__

    def client_json(self):
        d = {}
        d['g_id'] = self.message.g_msgid
        d['g_index'] = self.walk_index
        d['content_type'] = self.content_type
        d['content_disposition'] = self.content_disposition
        d['size'] = self.size
        d['filename'] = self.filename
        return d

    @reconstructor
    def init_on_load(self):
        if self._content_type_common:
            self.content_type = self._content_type_common
        else:
            self.content_type = self._content_type_other

    @property
    def namespace(self):
        return self.message.namespace


@event.listens_for(Block, 'before_insert', propagate=True)
def serialize_before_insert(mapper, connection, target):
    if target.content_type in common_content_types:
        target._content_type_common = target.content_type
        target._content_type_other = None
    else:
        target._content_type_common = None
        target._content_type_other = target.content_type


class FolderItem(JSONSerializable, Base, HasRevisions):
    """ Maps threads to folders.

    Threads in this table are the _Inbox_ datastore abstraction, which may
    be different from folder names in the actual account backends.
    """
    thread_id = Column(Integer, ForeignKey('thread.id', ondelete='CASCADE'),
                       nullable=False)
    # thread relationship is on Thread to make delete-orphan cascade work

    folder_name = Column(String(191), index=True)

    @property
    def namespace(self):
        return self.thread.namespace

    __table_args__ = (UniqueConstraint('folder_name', 'thread_id'),)


class Thread(JSONSerializable, Base):
    """ Threads are a first-class object in Inbox. This thread aggregates
        the relevant thread metadata from elsewhere so that clients can only
        query on threads.

        A thread can be a member of an arbitrary number of folders.

        If you're attempting to display _all_ messages a la Gmail's All Mail,
        don't query based on folder!
    """
    subject = Column(Text, nullable=True)
    subjectdate = Column(DateTime, nullable=False)
    recentdate = Column(DateTime, nullable=False)

    folders = relationship('FolderItem', backref="thread", single_parent=True,
                           order_by="FolderItem.folder_name",
                           cascade='all, delete, delete-orphan')

    namespace_id = Column(ForeignKey('namespace.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    namespace = relationship('Namespace', backref='threads')

    mailing_list_headers = Column(JSON, nullable=True)

    def update_from_message(self, message):
        if message.received_date > self.recentdate:
            self.recentdate = message.received_date
        # subject is subject of original message in the thread
        if message.received_date < self.recentdate:
            self.subject = message.subject
            self.subjectdate = message.received_date

        if len(message.mailing_list_headers) > len(self.mailing_list_headers):
            self.mailing_list_headers = message.mailing_list_headers
        return self

    @property
    def mailing_list_info(self):
        return self.mailing_list_headers

    def is_mailing_list_thread(self):
        for v in self.mailing_list_headers.itervalues():
            if (v is not None):
                return True
        return False

    def cereal(self):
        """ Threads are serialized with full message data. """
        d = {}
        d['id'] = self.id
        d['messages'] = [m.cereal() for m in self.messages]
        d['subject'] = self.subject
        d['recentdate'] = self.recentdate
        return d

    discriminator = Column('type', String(16))
    __mapper_args__ = {'polymorphic_on': discriminator}
