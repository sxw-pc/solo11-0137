"""Independent radio order collection and simple acknowledgement status."""
from .source import record
from .store import log


def import_data(db, data):
    affected = set()
    imported = 0
    with db:
        for item in data.get('orders', []):
            if item['revision'] < 1 or item['action'] not in {'hold', 'proceed', 'cancel'}:
                raise ValueError('invalid radio order')
            _, fresh = record(db, item['event_id'], 'radio-order', item)
            if fresh:
                db.execute('INSERT INTO radio_order VALUES (?,?,?,?,?,?,?)',
                           (item['event_id'], item['command_id'], item['voyage'],
                            item['revision'], item['action'], item['issuer'],
                            item['issued_at']))
                imported += 1
                affected.add(item['voyage'])
        cases = [decide(db, voyage) for voyage in sorted(affected)]
    return {'imported': imported, 'cases': cases}


def decide(db, voyage):
    order = db.execute('SELECT * FROM radio_order WHERE voyage=? '
                       'ORDER BY revision DESC,issued_at DESC,event_id DESC LIMIT 1',
                       (voyage,)).fetchone()
    ack = db.execute('SELECT * FROM radio_ack WHERE voyage=? AND command_id=? '
                     'ORDER BY received_at DESC,event_id DESC LIMIT 1',
                     (voyage, order['command_id'])).fetchone() if order else None
    state = 'missing' if not order else 'unconfirmed' if not ack else (
        'acknowledged' if ack['response'] == 'received' else 'refused')
    old = db.execute('SELECT command_id,order_event,ack_event,state FROM radio_case '
                     'WHERE voyage=?', (voyage,)).fetchone()
    fields = (order['command_id'] if order else None,
              order['event_id'] if order else None,
              ack['event_id'] if ack else None, state)
    if not old or tuple(old) != fields:
        db.execute('INSERT INTO radio_case VALUES (?,?,?,?,?) ON CONFLICT(voyage) '
                   'DO UPDATE SET command_id=excluded.command_id,order_event=excluded.order_event,'
                   'ack_event=excluded.ack_event,state=excluded.state', (voyage, *fields))
        log(db, 'radio', voyage, state, 'latest order and matching acknowledgement', fields[2])
    return {'voyage': voyage, 'command_id': fields[0], 'state': state,
            'order_event': fields[1], 'ack_event': fields[2]}
