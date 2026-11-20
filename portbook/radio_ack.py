"""Independent radio acknowledgement collection.

Legacy ``acks`` keep feeding the simple status in radio.decide.  New-style
``receipts`` bind a concrete directive event and body digest, carry sender
and receiver local sequence numbers, and drive the formal effectiveness and
personal receipt certifications in the radio ledger.
"""
import json
from .source import record
from . import radio


def import_data(db, data):
    ack_affected = set()
    cert_affected = set()
    imported = 0
    with db:
        for item in data.get('acks', []):
            if item['response'] not in {'received', 'refused'}:
                raise ValueError('invalid acknowledgement')
            _, fresh = record(db, item['event_id'], 'radio-ack', item)
            if fresh:
                db.execute('INSERT INTO radio_ack VALUES (?,?,?,?,?,?)',
                           (item['event_id'], item['command_id'], item['voyage'],
                            item['actor'], item['response'], item['received_at']))
                imported += 1
                ack_affected.add(item['voyage'])
        for item in data.get('receipts', []):
            radio.check_receipt(item)
            _, fresh = record(db, item['event_id'], 'radio-receipt', item)
            if fresh:
                db.execute('INSERT INTO radio_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (item['event_id'], item['command_id'], item['voyage'],
                            item['order_event'], item['digest'], item['actor'],
                            item['role'], item['response'], item['sender_seq'],
                            item['receiver_seq'],
                            json.dumps(sorted(item.get('parents', []))),
                            item['received_at']))
                imported += 1
                cert_affected.add(item['voyage'])
        cases = [radio.decide(db, voyage) for voyage in sorted(ack_affected)]
        formal = radio.certify_voyages(db, cert_affected | ack_affected)
    return {'imported': imported, 'cases': cases, **formal}
