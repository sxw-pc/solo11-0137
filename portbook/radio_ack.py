"""Independent radio acknowledgement collection."""
from .source import record
from . import radio


def import_data(db, data):
    affected = set()
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
                affected.add(item['voyage'])
        cases = [radio.decide(db, voyage) for voyage in sorted(affected)]
    return {'imported': imported, 'cases': cases}
