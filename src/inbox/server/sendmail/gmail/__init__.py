from inbox.server.sendmail.gmail.gmail import GmailSMTPClient
from inbox.server.sendmail.gmail.drafts import (new, reply, update, delete,
                                                send)

# delete_draft, get_draft, get_all_drafts are provider agnostic
__all__ = ['GmailSMTPClient', 'new', 'reply', 'update', 'delete', 'send']

PROVIDER = 'Gmail'
SENDMAIL_CLS = 'GmailSMTPClient'
