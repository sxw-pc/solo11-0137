"""Independent environmental sampling intake and threshold ledger."""
from .source import record
from .store import log


def import_data(db, data):
    affected = set()
    imported = 0
    with db:
        for entry in data.get('limits', []):
            if entry['version'] < 1 or entry['ceiling'] < 0:
                raise ValueError('invalid pollution limit')
            _, new = record(db, entry['event_id'], 'pollution-limit', entry)
            if new:
                db.execute('INSERT INTO pollution_limits VALUES (?,?,?,?,?,?)',
                           (entry['event_id'], entry['station'], entry['marker'],
                            entry['version'], entry['ceiling'], entry['signer']))
                imported += 1
                affected.add((entry['station'], entry['marker']))
        for entry in data.get('samples', []):
            if entry['reading'] < 0:
                raise ValueError('invalid sample reading')
            _, new = record(db, entry['event_id'], 'pollution-sample', entry)
            if new:
                db.execute('INSERT INTO pollution_samples VALUES (?,?,?,?,?,?)',
                           (entry['event_id'], entry['station'], entry['marker'],
                            entry['sampled_at'], entry['reading'], entry['laboratory']))
                imported += 1
                affected.add((entry['station'], entry['marker']))
        cases = [decide(db, station, marker) for station, marker in sorted(affected)]
    return {'imported': imported, 'cases': cases}


def decide(db, station, marker):
    limits = db.execute('SELECT * FROM pollution_limits WHERE station=? AND marker=? '
                        'ORDER BY version DESC,event_id DESC', (station, marker)).fetchall()
    samples = db.execute('SELECT * FROM pollution_samples WHERE station=? AND marker=? '
                         'ORDER BY sampled_at DESC,event_id DESC', (station, marker)).fetchall()
    sample = samples[0] if samples else None
    limit = limits[0] if limits else None
    if not sample or not limit:
        state, excess = 'pending', None
    elif any(item['ceiling'] != limit['ceiling'] for item in limits
             if item['version'] == limit['version']):
        state, excess = 'disputed', None
    else:
        excess = sample['reading'] - limit['ceiling']
        state = 'exceeded' if excess > 0 else 'within_limit'
    old = db.execute('SELECT state,sample_event,limit_event,excess FROM pollution_cases '
                     'WHERE station=? AND marker=?', (station, marker)).fetchone()
    fields = (state, sample['event_id'] if sample else None,
              limit['event_id'] if limit else None, excess)
    if not old or tuple(old) != fields:
        db.execute('INSERT INTO pollution_cases VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(station,marker) DO UPDATE SET state=excluded.state,'
                   'sample_event=excluded.sample_event,limit_event=excluded.limit_event,'
                   'excess=excluded.excess', (station, marker, *fields))
        log(db, 'pollution', f'{station}:{marker}', state,
            'latest sample against current signed limit', fields[1])
    return {'station': station, 'marker': marker, 'state': state,
            'sample_event': fields[1], 'limit_event': fields[2], 'excess': excess}
