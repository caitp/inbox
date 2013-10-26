import os

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Enum, Text
from sqlalchemy import ForeignKey, Table, Index, func
from sqlalchemy.types import PickleType

from sqlalchemy.ext.declarative import declarative_base
Base = declarative_base()

from sqlalchemy import event
from sqlalchemy.orm import reconstructor, relationship, backref
from sqlalchemy.schema import UniqueConstraint

from hashlib import sha256
from os import environ
from .util.file import mkdirp, remove_file, Lock
from .util.itert import chunk

import logging as log
from sqlalchemy.dialects import mysql
from boto.s3.connection import S3Connection
from boto.s3.key import Key

### Roles

class JSONSerializable(object):
    def client_json(self):
        """ Override this and return a string of the object serialized for
            the web client.
        """
        pass

STORE_MSG_ON_S3 = True
DB_CHUNK_SIZE = 100

class Blob(object):
    """ A blob of data that can be saved to local or remote (S3) disk. """
    size = Column(Integer, default=0)
    data_sha256 = Column(String(255))
    def save(self, data):
        assert data is not None, \
                "Blob can't have NoneType data (can be zero-length, though!)"
        assert type(data) is not unicode, "Blob bytes must be encoded"
        self.size = len(data)
        self.data_sha256 = sha256(data).hexdigest()
        if self.size > 0:
            if STORE_MSG_ON_S3:
                self._save_to_s3(data)
            else:
                self._save_to_disk(data)
        else:
            log.warning("Not saving 0-length {1} {0}".format(
                self.id, self.__class__.__name__))

    def get_data(self):
        if self.size == 0:
            # NOTE: This is a placeholder for "empty bytes". If this doesn't
            # work as intended, it will trigger the hash assertion later.
            data = ""
        elif STORE_MSG_ON_S3:
            data = self._get_from_s3()
        else:
            data = self._get_from_disk()
        assert self.data_sha256 == sha256(data).hexdigest(), \
                "Returned data doesn't match stored hash!"
        return data

    def delete_data(self):
        if self.size == 0:
            # nothing to do here
            return
        if STORE_MSG_ON_S3:
            self._delete_from_s3()
        else:
            self._delete_from_disk()
        # TODO should we clear these fields?
        # self.size = None
        # self.data_sha256 = None

    def _save_to_s3(self, data):
        assert len(data) > 0, "Need data to save!"
        # TODO: store AWS credentials in a better way.
        assert 'AWS_ACCESS_KEY_ID' in environ, "Need AWS key!"
        assert 'AWS_SECRET_ACCESS_KEY' in environ, "Need AWS secret!"
        assert 'MESSAGE_STORE_BUCKET_NAME' in environ, "Need bucket name to store message data!"
        # Boto pools connections at the class level
        conn = S3Connection(environ.get('AWS_ACCESS_KEY_ID'),
                            environ.get('AWS_SECRET_ACCESS_KEY'))
        bucket = conn.get_bucket(environ.get('MESSAGE_STORE_BUCKET_NAME'))

        # See if it alreays exists and has the same hash
        data_obj = bucket.get_key(self.data_sha256)
        if data_obj:
            assert data_obj.get_metadata('data_sha256') == self.data_sha256, \
                "BlockMeta hash doesn't match what we previously stored on s3!"
            # log.info("BlockMeta already exists on S3.")
            return

        data_obj = Key(bucket)
        # if metadata:
        #     assert type(metadata) is dict
        #     for k, v in metadata.iteritems():
        #         data_obj.set_metadata(k, v)
        data_obj.set_metadata('data_sha256', self.data_sha256)
        # data_obj.content_type = self.content_type  # Experimental
        data_obj.key = self.data_sha256
        # log.info("Writing data to S3 with hash {0}".format(self.data_sha256))
        # def progress(done, total):
        #     log.info("%.2f%% done" % (done/total * 100) )
        # data_obj.set_contents_from_string(data, cb=progress)
        data_obj.set_contents_from_string(data)

    def _get_from_s3(self):
        assert self.data_sha256, "Can't get data with no hash!"
        # Boto pools connections at the class level
        conn = S3Connection(environ.get('AWS_ACCESS_KEY_ID'),
                            environ.get('AWS_SECRET_ACCESS_KEY'))
        bucket = conn.get_bucket(environ.get('MESSAGE_STORE_BUCKET_NAME'))
        data_obj = bucket.get_key(self.data_sha256)
        assert data_obj, "No data returned!"
        return data_obj.get_contents_as_string()

    def _delete_from_s3(self):
        # TODO
        pass

    # Helpers
    @property
    def _data_file_directory(self):
        assert self.data_sha256
        # Nest it 6 items deep so we don't have folders with too many files.
        h = self.data_sha256
        return os.path.join('/mnt', 'parts', h[0], h[1], h[2], h[3], h[4], h[5])

    @property
    def _data_file_path(self):
        return os.path.join(self._data_file_directory, self.data_sha256)

    def _save_to_disk(self, data):
        mkdirp(self._data_file_directory)
        with open(self._data_file_path, 'wb') as f:
            f.write(data)

    def _get_from_disk(self):
        try:
            with open(self._data_file_path, 'rb') as f:
                return f.read()
        except Exception:
            log.error("No data for hash {0}".format(self.data_sha256))
            # XXX should this instead be empty bytes?
            return None

    def _delete_from_disk(self):
        remove_file(self._data_file_path)

### Column Types

class MediumPickle(PickleType):
    impl = mysql.MEDIUMBLOB

### Tables

# global

class IMAPAccount(Base):
    __tablename__ = 'imapaccount'
    id = Column(Integer, primary_key=True, autoincrement=True)
    # user_id refers to Inbox's user id
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    user = relationship("User", backref="accounts")

    email_address = Column(String(254), nullable=True, index=True)
    provider = Enum('Gmail', 'Outlook', 'Yahoo', 'Inbox')

    # local flags & data
    initial_sync_done = Column(Boolean)
    sync_active = Column(Boolean, default=False)
    save_raw_messages = Column(Boolean, default=True)
    sync_host = Column(String(255), nullable=True)

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

    @property
    def _sync_lockfile_name(self):
        return "/var/lock/inbox_sync/{0}.lock".format(self.id)

    @property
    def _sync_lock(self):
        return Lock(self._sync_lockfile_name, block=False)

    def sync_lock(self):
        self._sync_lock.acquire()

    def sync_unlock(self):
        self._sync_lock.release()

    def total_stored_data(self):
        """ Computes the total size of the block data of emails in your
            account's IMAP folders
        """
        subq = db_session.query(BlockMeta) \
                .join(BlockMeta.messagemeta, MessageMeta.foldermetas) \
                .filter(FolderMeta.imapaccount_id==self.id) \
                .group_by(MessageMeta.id, BlockMeta.id)
        return db_session.query(func.sum(subq.subquery().columns.size)).scalar()

    def total_stored_messages(self):
        """ Computes the number of emails in your account's IMAP folders """
        return db_session.query(MessageMeta) \
                .join(MessageMeta.foldermetas) \
                .filter(FolderMeta.imapaccount_id==self.id) \
                .group_by(MessageMeta.id).count()


class UserSession(Base):
    """ Inbox-specific sessions. """
    __tablename__ = 'user_session'

    id = Column(Integer, primary_key=True, autoincrement=True)

    token = Column(String(40))
    # sessions have a many-to-one relationship with users
    user_id = Column(Integer, ForeignKey('user.id'),
            nullable=False)
    user = relationship('User', backref='sessions')

class Namespace(Base):
    """ A way to do grouping / permissions, basically. """
    __tablename__ = 'namespace'
    id = Column(Integer, primary_key=True, autoincrement=True)
    # NOTE: only root namespaces have IMAP accounts
    # http://stackoverflow.com/questions/386294/what-is-the-maximum-length-of-a-valid-email-address
    imapaccount_id = Column(Integer, ForeignKey('imapaccount.id'),
            nullable=True)
    imapaccount = relationship('IMAPAccount', backref=backref('namespace',
        uselist=False))

    @property
    def email_address(self):
        if self.imapaccount is not None:
            return self.imapaccount.email_address

    @property
    def is_root(self):
        return self.imapaccount_id is not None

    def get_messages(self, folder_name):
        """ Returns all messages in a given folder.

            Note that this may be more messages than included in the IMAP
            folder, since we fetch the full thread if one of the messages is in
            the requested folder.

            NOTE: this assumes that the namespace is a "root"
            namespace and not a shared folder.
        """
    
        assert self.is_root, "get_messages is only defined on root namespaces"

        # Get all thread IDs for all messages in this folder.
        all_msgids = db_session.query(FolderMeta.messagemeta_id)\
              .filter(FolderMeta.folder_name == folder_name,
                      FolderMeta.imapaccount_id == self.imapaccount_id)
        all_thrids = set()
        for thrid, in db_session.query(MessageMeta.g_thrid).filter(
                MessageMeta.namespace_id == self.id,
                MessageMeta.id.in_(all_msgids)):
            all_thrids.add(thrid)

        # Get all messages for those thread IDs
        all_msgs = []
        for g_thrids in chunk(list(all_thrids), DB_CHUNK_SIZE):
            all_msgs_query = db_session.query(MessageMeta).filter(
                    MessageMeta.namespace_id == self.id,
                    MessageMeta.g_thrid.in_(g_thrids))
            all_msgs += all_msgs_query.all()

        return all_msgs

class SharedFolderNSMeta(Base):
    __tablename__ = 'sharedfoldernsmeta'

    id = Column(Integer, primary_key=True, autoincrement=True)

    namespace = relationship('Namespace', backref='sharedfoldernsmetas')
    namespace_id = Column(Integer, ForeignKey('namespace.id'), nullable=False)
    user = relationship('User', backref='sharedfoldernsmetas')
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)

    display_name = Column(String(40))

class User(Base):
    __tablename__ = 'user'

    id = Column(Integer, primary_key=True, autoincrement=True)

    name = Column(String(255))

# sharded

class MessageMeta(JSONSerializable, Base):
    __tablename__ = 'messagemeta'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # XXX clean this up a lot - make a better constructor, maybe taking
    # a mailbase as an argument to prefill a lot of attributes

    # TODO Figure out how this cross-shard foreign key works with
    # SQLAlchemy's sharding support.
    namespace_id = Column(ForeignKey('namespace.id'), nullable=False)
    namespace = relationship("Namespace", backref="messagemetas")
    # TODO probably want to store some of these headers in a better
    # non-pickled way to provide indexing
    from_addr = Column(MediumPickle)
    sender_addr = Column(MediumPickle)
    reply_to = Column(MediumPickle)
    to_addr = Column(MediumPickle)
    cc_addr = Column(MediumPickle)
    bcc_addr = Column(MediumPickle)
    in_reply_to = Column(MediumPickle)
    message_id = Column(String(255))
    subject = Column(Text(collation='utf8_unicode_ci'))
    internaldate = Column(DateTime)
    size = Column(Integer, default=0)
    data_sha256 = Column(String(255))

    # TODO genericize these
    g_msgid = Column(String(255))
    g_thrid = Column(String(255))

    def trimmed_subject(self):
        s = self.subject
        if s[:4] == u'RE: ' or s[:4] == u'Re: ' :
            s = s[4:]
        return s

    def client_json(self):
        # TODO serialize more here for client API
        d = {}
        d['from'] = self.from_addr
        d['to'] = self.to_addr
        d['date'] = self.internaldate
        d['subject'] = self.subject
        d['g_id'] = self.g_msgid
        d['g_thrid'] = self.g_thrid
        d['namespace_id'] = self.namespace_id
        return d

# make pulling up all messages in a given thread fast
Index('messagemeta_namespace_id_g_thrid', MessageMeta.namespace_id,
        MessageMeta.g_thrid)

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

class RawMessage(JSONSerializable, Blob, Base):
    __tablename__ = 'rawmessage'
    """ Metadata for raw messages (used for allowing the mail ingester
        to change over time without having to re-download all messages).

        This table should not depend on any table other than the User table.
        (No other foreign keys!)
    """
    id = Column(Integer, primary_key=True, autoincrement=True)
    namespace_id = Column(ForeignKey('namespace.id'), nullable=False)
    namespace = relationship("Namespace")

    # Save data other than BODY[] that we query for when downloading messages
    # from the IMAP account.
    internaldate = Column(DateTime)
    g_msgid = Column(String(255))
    g_thrid = Column(String(255))

    folder_name = Column(String(255))
    msg_uid = Column(String(255))

    flags = Column(MediumPickle)

class BlockMeta(JSONSerializable, Blob, Base):
    __tablename__ = 'blockmeta'
    """ Metadata for message parts stored in s3 """

    id = Column(Integer, primary_key=True, autoincrement=True)
    messagemeta_id = Column(Integer, ForeignKey('messagemeta.id'), nullable=False)
    messagemeta = relationship('MessageMeta', backref="parts")

    walk_index = Column(Integer)
    # Save some space with common content types
    _content_type_common = Column(Enum(*common_content_types))
    _content_type_other = Column(String(255))
    filename = Column(String(255))

    content_disposition = Column(Enum('inline', 'attachment'))
    content_id = Column(String(255))  # For attachments
    misc_keyval = Column(MediumPickle)

    is_inboxapp_attachment = Column(Boolean, default=False)
    collection_id = Column(Integer, ForeignKey("collections.id"), nullable=True)
    collection = relationship('Collection', backref="parts")

    # TODO: create a constructor that allows the 'content_type' keyword

    __table_args__ = (UniqueConstraint('messagemeta_id', 'walk_index',
        'data_sha256', name='_blockmeta_uc'),)

    def __init__(self, *args, **kwargs):
        self.content_type = None
        self.size = 0
        Base.__init__(self, *args, **kwargs)

    def __repr__(self):
        return 'BlockMeta: %s' % self.__dict__

    def client_json(self):
        d = {}
        d['g_id'] = self.messagemeta.g_msgid
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

@event.listens_for(BlockMeta, 'before_insert', propagate = True)
def serialize_before_insert(mapper, connection, target):
    if target.content_type in common_content_types:
        target._content_type_common = target.content_type
        target._content_type_other = None
    else:
        target._content_type_common = None
        target._content_type_other = target.content_type

class FolderMeta(JSONSerializable, Base):
    __tablename__ = 'foldermeta'
    """ This maps folder names to UIDs """

    id = Column(Integer, primary_key=True, autoincrement=True)

    imapaccount_id = Column(ForeignKey('imapaccount.id'), nullable=False)
    imapaccount = relationship("IMAPAccount")
    messagemeta_id = Column(Integer, ForeignKey('messagemeta.id'), nullable=False)
    messagemeta = relationship('MessageMeta')
    msg_uid = Column(Integer, nullable=False)
    folder_name = Column(String(255), nullable=False)  # All Mail, Inbox, etc. (i.e. Labels)
    flags = Column(MediumPickle)

    __table_args__ = (UniqueConstraint('folder_name', 'msg_uid', 'imapaccount_id',
        name='_folder_msg_user_uc'),)

# make pulling up all messages in a given folder fast
Index('foldermeta_imapaccount_id_folder_name', FolderMeta.imapaccount_id,
        FolderMeta.folder_name)

class UIDValidity(JSONSerializable, Base):
    __tablename__ = 'uidvalidity'
    """ UIDValidity has a per-folder value. If it changes, we need to
        re-map g_msgid to UID for that folder.
    """

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(ForeignKey('imapaccount.id'), nullable=False)
    account = relationship("IMAPAccount")
    folder_name = Column(String(255))
    uid_validity = Column(Integer)
    highestmodseq = Column(Integer)

    __table_args__ = (UniqueConstraint('account_id', 'folder_name',
        name='_folder_account_uc'),)

class Collection(Base):
    __tablename__ = 'collections'
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(32), nullable=True)

## Make the tables
from sqlalchemy import create_engine
DB_URI = "mysql://{username}:{password}@{host}:{port}/{database}?charset=utf8mb4"

if 'RDS_HOSTNAME' in environ:
    # Amazon RDS settings for production
    engine = create_engine(DB_URI.format(
        username = environ.get('RDS_USER'),
        password = environ.get('RDS_PASSWORD'),
        host = environ.get('RDS_HOSTNAME'),
        port = environ.get('RDS_PORT'),
        database = environ.get('RDS_DB_NAME')
    ))

else:

    if os.environ['MYSQL_USER'] == 'XXXXXXX':
        log.error("Go setup MySQL settings in config file!")
        raise Exception()

    engine = create_engine(DB_URI.format(
        username = environ.get('MYSQL_USER'),
        password = environ.get('MYSQL_PASSWORD'),
        host = environ.get('MYSQL_HOSTNAME'),
        port = environ.get('MYSQL_PORT'),
        database = environ.get('MYSQL_DATABASE')
    ))


# ## Make the tables
# from sqlalchemy import create_engine
# DB_URI = "mysql+pymysql://{username}:{password}@{host}:{port}/{database}"


# if 'RDS_HOSTNAME' in environ:
#     # Amazon RDS settings for production
#     engine = create_engine(DB_URI.format(
#         username = environ.get('RDS_USER'),
#         password = environ.get('RDS_PASSWORD'),
#         host = environ.get('RDS_HOSTNAME'),
#         port = environ.get('RDS_PORT'),
#         database = environ.get('RDS_DB_NAME')
#     ), connect_args = {'charset': 'utf8mb4'} )

# else:

#     if os.environ['MYSQL_USER'] == 'XXXXXXX':
#         log.error("Go setup MySQL settings in config file!")
#         raise Exception()

#     engine = create_engine(DB_URI.format(
#         username = environ.get('MYSQL_USER'),
#         password = environ.get('MYSQL_PASSWORD'),
#         host = environ.get('MYSQL_HOSTNAME'),
#         port = environ.get('MYSQL_PORT'),
#         database = environ.get('MYSQL_DATABASE')
#     ), connect_args = {'charset': 'utf8mb4'} )

def init_db():
    Base.metadata.create_all(engine)

from sqlalchemy.orm import sessionmaker
Session = sessionmaker()
Session.configure(bind=engine)

# A single global database session per Inbox instance is good enough for now.
db_session = Session()
