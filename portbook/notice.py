import hashlib
import json
from .source import record
from .store import log

def current(db, voyage):
    return db.execute('SELECT * FROM notice_decisions WHERE voyage=?', (voyage,)).fetchone()

def decide(db, voyage):
    rows = db.execute('SELECT r.*,e.raw FROM revisions r JOIN evidence e ON e.event_id=r.event_id WHERE voyage=? AND r.event_id NOT IN (SELECT target_event_id FROM withdrawals) ORDER BY revision DESC,event_id', (voyage,)).fetchall()
    if not rows:
        return None
    highest = rows[0]['revision']
    top = [r for r in rows if r['revision'] == highest]
    if len({r['digest'] for r in top}) > 1:
        state, reason, chosen = 'conflict', 'parallel revision digest conflict', None
    else:
        chosen = top[0]
        state, reason = ('cancelled', 'signed cancellation') if chosen['cancelled'] else ('accepted', 'authorized revision')
    old = current(db, voyage)
    if old and old['state'] == state and old['reason'] == reason and old['event_id'] == (chosen['event_id'] if chosen else None):
        return dict(old)
    version = old['version'] + 1 if old else 1
    db.execute('INSERT INTO notice_decisions(voyage,state,revision,event_id,reason,version) VALUES(?,?,?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET state=excluded.state,revision=excluded.revision,event_id=excluded.event_id,reason=excluded.reason,version=excluded.version',
               (voyage,state,highest,chosen['event_id'] if chosen else None,reason,version))
    log(db,'notice',voyage,state,reason,chosen['event_id'] if chosen else None)
    from .resources import invalidate_voyage
    if old and (old['version'] != version):
        invalidate_voyage(db,voyage,'notice version changed')
    return dict(current(db,voyage))

def import_data(db, data):
    results=[]
    with db:
        for c in data.get('credentials',[]):
            db.execute('INSERT INTO credentials(signer,source,valid_from,valid_to) VALUES(?,?,?,?) ON CONFLICT(signer) DO UPDATE SET source=excluded.source,valid_from=excluded.valid_from,valid_to=excluded.valid_to',
                       (c['signer'],c['source'],c['valid_from'],c['valid_to']))
        for item in data.get('notices',[]):
            event_id=item['event_id']
            digest,new=record(db,event_id,'notice',item)
            if not new:
                results.append({'event_id':event_id,'state':'idempotent'})
                continue
            c=db.execute('SELECT * FROM credentials WHERE signer=?',(item['signer'],)).fetchone()
            valid=bool(c and c['source']==item['source'] and c['valid_from']<=item['issued_at']<=c['valid_to'] and item['source']=='harbor')
            if not valid:
                log(db,'notice',item['voyage'],'untrusted','invalid signer or source',event_id)
                results.append({'event_id':event_id,'state':'untrusted'})
                continue
            semantic={k:v for k,v in item.items() if k not in ('event_id','issued_at')}
            semantic_digest=hashlib.sha256(json.dumps(semantic,sort_keys=True).encode()).hexdigest()
            db.execute('INSERT INTO revisions(event_id,voyage,revision,eta,draft,cancelled,digest) VALUES(?,?,?,?,?,?,?)',
                       (event_id,item['voyage'],item['revision'],item['eta'],item['draft'],int(item.get('cancelled',False)),semantic_digest))
            results.append(decide(db,item['voyage']))
        for item in data.get('withdrawals',[]):
            target=db.execute('SELECT voyage FROM revisions WHERE event_id=?',(item['target_event_id'],)).fetchone()
            if not target: raise ValueError('withdrawal target missing')
            c=db.execute('SELECT * FROM credentials WHERE signer=?',(item['signer'],)).fetchone()
            if not c or c['source']!='harbor' or not(c['valid_from']<=item['issued_at']<=c['valid_to']):
                raise ValueError('withdrawal not authorized')
            _,new=record(db,item['event_id'],'withdrawal',item)
            if new:
                db.execute('INSERT INTO withdrawals(event_id,target_event_id,reason) VALUES(?,?,?)',
                           (item['event_id'],item['target_event_id'],item['reason']))
                results.append(decide(db,target['voyage']))
    return results
