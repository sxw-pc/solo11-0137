from datetime import datetime, timedelta
from .source import record
from .store import log
from .resources import invalidate_voyage, promote

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
    return out

def decide(db,voyage,grace_minutes=30):
    rows=db.execute('SELECT * FROM witnesses WHERE voyage=? ORDER BY at,event_id',(voyage,)).fetchall()
    agent=next((r for r in rows if r['role']=='agent'),None)
    port=next((r for r in rows if r['role']=='port' and (not agent or r['actor']!=agent['actor'])),None)
    actual=db.execute('SELECT * FROM occupancy WHERE voyage=? ORDER BY start DESC',(voyage,)).fetchone()
    allocation=db.execute("SELECT * FROM allocations WHERE voyage=? AND state='confirmed' ORDER BY start LIMIT 1",(voyage,)).fetchone()
    if not allocation:
        state,reason='disputed','physical occupancy without valid plan' if actual else 'no valid allocation'
    elif not port or not agent:
        state,reason='pending','independent agent and port witness required'
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
