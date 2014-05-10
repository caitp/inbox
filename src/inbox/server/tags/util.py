# STOPSHIP(emfree) restructure this as needed

PROVIDER_NAME_MAPPING = {
    'Gmail': 'gmail',
    'Outlook': 'outlook',
    'Yahoo': 'yahoo',
    'EAS': 'exchange',
    'Inbox': 'inbox'
}


RESERVED_PROVIDER_NAMES = [
    'gmail', 'outlook', 'yahoo', 'exchange', 'inbox', 'icloud', 'aol'
]

GMAIL_SYSTEM_NAME_MAPPING = {
    'Inbox': 'inbox',
    'Draft': 'drafts',
    '[Gmail]/Drafts': 'drafts',
    'Sent': 'sent',
    '[Gmail]/Sent Mail': 'sent',
    'Starred': 'starred',
    '[Gmail]/Starred': 'starred',
    '[Gmail]/Trash': 'trash',
    '[Gmail]/Spam': 'spam',
    '[Gmail]/All Mail': None
}


def exposed_name(folder, name):
    """Return the representation of a folder name that we expose as a tag.
    For folders corresponding to Inbox system tags, return the tag name.
    Otherwise, return the '<provider>-<folder_name>' (all lowercased).
    E.g., for the folder corresponding to the Gmail label 'jobslist',
    return 'gmail-jobslist'."""
    provider = PROVIDER_NAME_MAPPING[folder.account.provider]
    if provider == 'gmail':
        if name in GMAIL_SYSTEM_NAME_MAPPING:
            return GMAIL_SYSTEM_NAME_MAPPING[name]
    return '-'.join((provider, name.lower()))


def name_available(name, namespace_id, db_session):
    from inbox.server.models.tables.base import InternalTag
    """Returns True if and only if the given name can be used as a new internal
    tag for the given namespace."""
    name = name.lower()
    if any(name.startswith(provider) for provider in RESERVED_PROVIDER_NAMES):
        return False

    if name in InternalTag.SYSTEM_NAMES:
        return False

    if (name,) in db_session.query(InternalTag.name). \
            filter(InternalTag.namespace_id == namespace_id):
        return False

    return True
