from datetime import datetime, timedelta
from .source import record
from .store import log
from .resources import invalidate_voyage, promote, dispatch_request, preview_group

def import_data(db,data):
    out=[]
    with db:
        for w in data.get('witnesses',[]):
            _,new=record(db,w['event_id'],'witness',w)
            if new:
                db.execute('INSERT INTO witnesses(event_id,voyage,actor,role,at) VALUES(?,?,?,?,?)',
                           (w['event_id'],w['voyage'],w['actor'],w['role'],w['at']))
        for o in data.get('occupancies',[]):
            _,new=record(db,o['event_id'],'occupancy',o)
            if new:
                db.execute('INSERT INTO occupancy(event_id,voyage,berth,start,end) VALUES(?,?,?,?,?)',
                           (o['event_id'],o['voyage'],o['berth'],o['start'],o.get('end')))
        for voyage in sorted({w['voyage'] for w in data.get('witnesses',[])} | {o['voyage'] for o in data.get('occupancies',[])}):
            out.append(decide(db,voyage,data.get('grace_minutes',30)))
        # A release produced by this batch may be followed by a one-shot
        # group dispatch; the delegation verdict is committed in the same
        # transaction and is itself idempotent on re-import.
        for request in data.get('delegations',[]):
            out.append(dispatch_request(db,request))
    return out

def preview_dispatch(db,request):
    """Read-only view of the overall feasible group plan before confirmation."""
    return preview_group(db,request)

def decide(db,voyage,grace_minutes=30):
    rows=db.execute('SELECT * FROM witnesses WHERE voyage=? ORDER BY at,event_id',(voyage,)).fetchall()
    agent=next((r for r in rows if r['role']=='agent'),None)
    ports=[r for r in rows if r['role']=='port' and (not agent or r['actor']!=agent['actor'])]
    port=ports[0] if ports else None
    actual=db.execute('SELECT * FROM occupancy WHERE voyage=? ORDER BY start DESC',(voyage,)).fetchone()
    allocation=db.execute("SELECT * FROM allocations WHERE voyage=? AND state='confirmed' ORDER BY start LIMIT 1",(voyage,)).fetchone()
    if not allocation:
        state,reason='disputed','physical occupancy without valid plan' if actual else 'no valid allocation'
    elif not port or not agent:
        state,reason='pending','independent agent and port witness required'
    elif len(ports)>1 and (datetime.fromisoformat(ports[-1]['at'])-datetime.fromisoformat(ports[0]['at'])).total_seconds()>300:
        state,reason='disputed','conflicting independent port witness times'
    else:
        deadline=datetime.fromisoformat(allocation['start'])+timedelta(minutes=grace_minutes)
        if datetime.fromisoformat(port['at'])>deadline:
            if actual:
                state,reason='disputed','late witnessed arrival but physical occupancy exists'
            else:
                state,reason='forfeited','late independent port witness'
                invalidate_voyage(db,voyage,'late arrival forfeiture')
                promote(db)
        else:
            state,reason='fulfilled','independent timely port witness'
    old=db.execute('SELECT state,reason FROM fulfilments WHERE voyage=?',(voyage,)).fetchone()
    if not old or old['state']!=state or old['reason']!=reason:
        db.execute('INSERT INTO fulfilments(voyage,state,reason,allocation_id,witness_id) VALUES(?,?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET state=excluded.state,reason=excluded.reason,allocation_id=excluded.allocation_id,witness_id=excluded.witness_id,updated_at=CURRENT_TIMESTAMP',
                   (voyage,state,reason,allocation['id'] if allocation else None,port['event_id'] if port else None))
        log(db,'arrival',voyage,state,reason,port['event_id'] if port else None)
    return {'voyage':voyage,'state':state,'reason':reason}
