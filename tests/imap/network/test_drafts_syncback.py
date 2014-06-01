import uuid
from datetime import datetime

import pytest

from tests.util.crispin import crispin_client

ACCOUNT_ID = 1
NAMESPACE_ID = 1
THREAD_ID = 2

# These tests use a real Gmail test account and idempotently put the account
# back to the state it started in when the test is done.


@pytest.fixture(scope='function')
def message(db, config):
    from inbox.server.models.tables.imap import ImapAccount

    account = db.session.query(ImapAccount).get(ACCOUNT_ID)
    to = u'"\u2605The red-haired mermaid\u2605" <{0}>'.\
        format(account.email_address)
    subject = 'Draft test: ' + str(uuid.uuid4().hex)
    body = '<html><body><h2>Sea, birds, yoga and sand.</h2></body></html>'

    return (to, subject, body)


def test_remote_save_draft(db, config, message):
    """
    Tests the remote_save_draft function called by
    create_draft(), update_draft().

    """
    from inbox.server.actions.gmail import remote_save_draft
    from inbox.server.sendmail.base import all_recipients
    from inbox.server.sendmail.message import create_email, SenderInfo
    from inbox.server.models.tables.imap import ImapAccount

    account = db.session.query(ImapAccount).get(ACCOUNT_ID)
    sender_info = SenderInfo(name=account.full_name,
                             email=account.email_address)
    to, subject, body = message
    email = create_email(sender_info, all_recipients(to), subject, body, None)
    flags = [u'\\Draft']
    date = datetime.utcnow()

    remote_save_draft(ACCOUNT_ID, account.drafts_folder.name,
                      email.to_string(), flags=flags, date=date)

    with crispin_client(account.id, account.provider) as c:
        criteria = ['NOT DELETED', 'SUBJECT "{0}"'.format(subject)]

        c.conn.select_folder(account.drafts_folder.name, readonly=False)

        inbox_uids = c.conn.search(criteria)
        assert inbox_uids, 'Message missing from Drafts folder'

        c.conn.delete_messages(inbox_uids)
        c.conn.expunge()


def test_remote_delete_draft(db, config, message):
    """
    Tests the remote_delete_draft function called by
    update_draft(), delete_draft(), send_draft().

    """
    from inbox.server.actions.gmail import (remote_save_draft,
                                            remote_delete_draft)
    from inbox.server.sendmail.base import all_recipients
    from inbox.server.sendmail.message import create_email, SenderInfo
    from inbox.server.models.tables.imap import ImapAccount

    # Save on remote
    account = db.session.query(ImapAccount).get(ACCOUNT_ID)
    sender_info = SenderInfo(name=account.full_name,
                             email=account.email_address)
    to, subject, body = message
    email = create_email(sender_info, all_recipients(to), subject, body, None)
    flags = [u'\\Draft']
    date = datetime.utcnow()

    inbox_uid = email.headers['X-INBOX-ID']

    remote_save_draft(ACCOUNT_ID, account.drafts_folder.name,
                      email.to_string(), flags=flags, date=date)

    with crispin_client(account.id, account.provider) as c:
        criteria = ['DRAFT', 'NOT DELETED',
                    'HEADER X-INBOX-ID {0}'.format(inbox_uid)]

        c.conn.select_folder(account.drafts_folder.name, readonly=False)
        uids = c.conn.search(criteria)
        assert uids, 'Message missing from Drafts folder'

        # Delete on remote
        remote_delete_draft(account.id, inbox_uid, account.drafts_folder.name)

        c.conn.select_folder(account.drafts_folder.name, readonly=False)
        uids = c.conn.search(criteria)
        assert not uids, 'Message still in Drafts folder'
