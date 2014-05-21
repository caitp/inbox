from datetime import datetime

from sqlalchemy import asc, desc
from sqlalchemy.orm.exc import NoResultFound

from inbox.server.models.tables.base import Transaction


def create_event(transaction):
    if transaction.command == 'delete':
        event_type = 'delete'
    elif (transaction.delta is not None and transaction.delta.get('deleted_at')
          is not None):
        # Object was soft-deleted
        event_type = 'delete'
    elif transaction.command == 'insert':
        event_type = 'create'
    elif transaction.command == 'update':
        event_type = 'modify'

    # TODO(emfree) is there a less hacky way to handle this polymorphism?
    if transaction.table_name in ['thread', 'imapthread']:
        object_type = 'thread'
    elif transaction.table_name == 'message':
        object_type = 'message'
    else:
        raise TypeError('Attempting to create a sync event for an unsupported '
                        'object type')

    if transaction.object_public_id is None:
        raise ValueError('object_public_id is not set')

    # STOPSHIP(emfree): on updates, only return the delta, not the full
    # transaction.
    snapshot = transaction.additional_data
    return {
        'id': transaction.object_public_id,
        'event': event_type,
        'type': object_type,
        'attributes': snapshot
    }


def get_public_id_from_ts(namespace_id, timestamp, db_session):
    dt = datetime.utcfromtimestamp(timestamp)
    transaction = db_session.query(Transaction). \
        order_by(desc(Transaction.id)). \
        filter(Transaction.created_at <= dt,
               Transaction.namespace_id == namespace_id).first()
    if transaction is None:
        transaction = db_session.query(Transaction). \
            order_by(asc(Transaction.id)). \
            filter(Transaction.created_at > dt,
                   Transaction.namespace_id == namespace_id).first()
    if transaction is None:
        return None
    return transaction.public_id


def get_entries_after_public_id(namespace_id, events_start,
                                db_session, result_limit):
    # TODO(emfree) should maybe apply result_limit *after* filtering by type.
    try:
        internal_start_id, = db_session.query(Transaction.id). \
            filter(Transaction.public_id == events_start).one()
    except NoResultFound:
        raise ValueError('Invalid first_public_id parameter: {}'.
                         format(events_start))
    query = db_session.query(Transaction). \
        order_by(asc(Transaction.id)). \
        filter(Transaction.namespace_id == namespace_id,
               Transaction.id > internal_start_id)
    events = []
    events_end = events_start
    for transaction in query.yield_per(result_limit):
        try:
            events.append(create_event(transaction))
            events_end = transaction.public_id
            if len(events) == result_limit:
                break
        except (TypeError, ValueError):
            pass

    return {
        'events_start': events_start,
        'events_end': events_end,
        'events': events
    }
