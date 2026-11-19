from datetime import datetime
from .source import record
from .store import log

def overlaps(a,b,c,d):
    return a<d and c<b

def invalidate_voyage(db,voyage,reason,promote_after=True):
    for row in db.execute("SELECT id FROM allocations WHERE voyage=? AND state='confirmed'",(voyage,)).fetchall():
        actual=db.execute('SELECT event_id FROM occupancy WHERE voyage=?',(voyage,)).fetchone()
        state='disputed' if actual else 'released'
        db.execute("UPDATE allocations SET state=?,reason=? WHERE id=?",(state,reason,row['id']))
        log(db,'resources',row['id'],state,reason)
    if promote_after: promote(db)

def invalidate_window(db,ship_class,start,end,reason):
    for row in db.execute("SELECT a.id FROM allocations a JOIN berths b ON b.id=a.berth WHERE b.ship_class=? AND a.state='confirmed' AND a.start<? AND a.end>?",(ship_class,end,start)).fetchall():
        db.execute("UPDATE allocations SET state='pending',reason=? WHERE id=?",(reason,row['id']))
        log(db,'resources',row['id'],'pending',reason)

def _candidate(db,a,exclude_id=None):
    berth=db.execute('SELECT * FROM berths WHERE id=?',(a['berth'],)).fetchone()
    tug=db.execute('SELECT * FROM tugs WHERE id=?',(a['tug'],)).fetchone()
    if not berth or not tug: return 'unknown berth or tug',None,None
    if a['draft']+a.get('margin',0.5)>berth['depth']: return 'berth depth insufficient',None,None
    if not tug['qualified'] or tug['power']<a.get('required_power',1): return 'tug qualification or power insufficient',None,None
    if a['start']<tug['start'] or a['end']>tug['end']: return 'tug shift unavailable',None,None
    notice_version=window_version=None
    if a.get('formal',True):
        n=db.execute('SELECT * FROM notice_decisions WHERE voyage=?',(a['voyage'],)).fetchone()
        w=db.execute('SELECT * FROM windows WHERE ship_class=? AND start<=? AND end>=? AND state=? ORDER BY version DESC LIMIT 1',(berth['ship_class'],a['start'],a['end'],'safe')).fetchone()
        if not n or n['state']!='accepted': return 'notice not accepted',None,None
        rev=db.execute('SELECT eta,draft FROM revisions WHERE event_id=?',(n['event_id'],)).fetchone()
        if rev and (abs((datetime.fromisoformat(a['start'])-datetime.fromisoformat(rev['eta'])).total_seconds())>3600 or a['draft']!=rev['draft']):
            return 'allocation differs from authorized voyage',None,None
        if not w:
            return 'safe passage window unavailable',None,None
        notice_version=n['version']; window_version=w['version']
    occupied=db.execute("SELECT * FROM allocations WHERE state='confirmed' AND id<>? AND id<>?",(a['id'],exclude_id or '')).fetchall()
    occupied+=db.execute('SELECT * FROM occupancy WHERE end IS NULL OR end>?',(a['start'],)).fetchall()
    for o in occupied:
        if overlaps(a['start'],a['end'],o['start'],o['end'] or '9999'):
            if o['berth']==a['berth']: return 'berth occupied',notice_version,window_version
            if 'tug' in o.keys() and o['tug']==a['tug']: return 'tug occupied',notice_version,window_version
    return None,notice_version,window_version

def promote(db):
    for a in db.execute("SELECT * FROM allocations WHERE state='waiting' ORDER BY priority DESC,start,id").fetchall():
        reason,nv,wv=_candidate(db,dict(a))
        if reason is None:
            db.execute("UPDATE allocations SET state='confirmed',reason='promoted',notice_version=?,window_version=? WHERE id=?",(nv,wv,a['id']))
            log(db,'resources',a['id'],'confirmed','promoted')

def import_data(db,data):
    out=[]
    with db:
        for b in data.get('berths',[]):
            db.execute('INSERT INTO berths(id,depth,ship_class) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET depth=excluded.depth,ship_class=excluded.ship_class',(b['id'],b['depth'],b['ship_class']))
        for t in data.get('tugs',[]):
            db.execute('INSERT INTO tugs(id,power,qualified,start,end) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET power=excluded.power,qualified=excluded.qualified,start=excluded.start,end=excluded.end',(t['id'],t['power'],int(t['qualified']),t['start'],t['end']))
        for a in sorted(data.get('applications',[]),key=lambda x:(-x['priority'],x['start'],x['id'])):
            _,new=record(db,a['id'],'application',a)
            if not new:
                out.append({'id':a['id'],'state':'idempotent'}); continue
            old=db.execute("SELECT id FROM allocations WHERE voyage=? AND state='confirmed'",(a['voyage'],)).fetchone()
            reason,nv,wv=_candidate(db,a,old['id'] if old else None)
            if old and reason is None:
                invalidate_voyage(db,a['voyage'],'replaced by revised plan',False)
            state='confirmed' if reason is None else 'waiting'
            db.execute('INSERT INTO allocations(id,voyage,berth,tug,start,end,priority,draft,margin,required_power,formal,state,reason,notice_version,window_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (a['id'],a['voyage'],a['berth'],a['tug'],a['start'],a['end'],a['priority'],a['draft'],a.get('margin',0.5),a.get('required_power',1),int(a.get('formal',True)),state,reason or 'all constraints met',nv,wv))
            log(db,'resources',a['id'],state,reason or 'all constraints met',a['id'])
            out.append({'id':a['id'],'state':state,'reason':reason})
        promote(db)
    return out
