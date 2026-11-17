from datetime import datetime
from .source import record
from .store import log

def current(db, ship_class, start, end):
    return db.execute('SELECT * FROM windows WHERE ship_class=? AND base_start=? AND base_end=? AND start=? AND end=?',(ship_class,start,end,start,end)).fetchone()

def import_data(db,data):
    results=[]
    with db:
        for g in data.get('gauges',[]):
            _,new=record(db,g['event_id'],'gauge',g)
            if new:
                db.execute('INSERT INTO gauges(event_id,station,at,datum,depth,version) VALUES(?,?,?,?,?,?)',
                           (g['event_id'],g['station'],g['at'],g['datum'],g['depth'],g['version']))
        for r in data.get('restrictions',[]):
            _,new=record(db,r['event_id'],'restriction',r)
            if new:
                db.execute('INSERT INTO restrictions(event_id,order_id,version,start,end,signer,revoked) VALUES(?,?,?,?,?,?,?)',
                           (r['event_id'],r['order_id'],r.get('version',1),r['start'],r['end'],r['signer'],int(r.get('revoked',False))))
        for template in data.get('templates',[]):
            results.append(decide(db,template))
    return results

def decide(db,template):
    ship=template['ship_class']; start=template['start']; end=template['end']
    points=db.execute('SELECT * FROM gauges WHERE station=? AND at>=? AND at<=? ORDER BY at,version DESC',
                      (template['station'],start,end)).fetchall()
    latest={}
    for p in points:
        latest.setdefault(p['at'],p)
    samples=sorted(latest.values(),key=lambda x:x['at'])
    required=template.get('required_samples',3)
    max_gap=template.get('max_gap_minutes',120)
    if len(samples)<required or not samples or samples[0]['at']!=start or samples[-1]['at']!=end:
        state,reason,min_depth='pending','missing continuous gauge samples',None
    elif any((datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds()>max_gap*60 for a,b in zip(samples,samples[1:])):
        state,reason,min_depth='pending','gauge sample gap exceeds limit',None
    elif len({p['datum'] for p in samples})!=1:
        state,reason,min_depth='pending','gauge datum conflict',None
    else:
        min_depth=min(p['depth'] for p in samples)
        all_orders=db.execute('SELECT * FROM restrictions ORDER BY version DESC,event_id').fetchall()
        newest={}
        for r in all_orders:
            newest.setdefault(r['order_id'],[])
            if not newest[r['order_id']] or newest[r['order_id']][0]['version']==r['version']:
                newest[r['order_id']].append(r)
        active=[r for group in newest.values() for r in group if r['start']<end and r['end']>start]
        conflict=any(len({(r['start'],r['end'],r['revoked']) for r in group})>1 for group in newest.values())
        closed=[r for r in active if not r['revoked']]
        if conflict:
            state,reason='pending','restriction version conflict'
        elif any(r['signer']!='harbor' for r in active):
            state,reason='pending','unauthorized restriction'
        elif min_depth < template['draft']+template['margin']:
            state,reason='unsafe','minimum under keel clearance failed'
        elif closed:
            state,reason='partially_safe','valid closure splits passage window'
        else:
            state,reason='safe','continuous safe passage'
    old=current(db,ship,start,end)
    if old and state!='partially_safe' and old['state']==state and old['reason']==reason and old['start']==start and old['end']==end and old['min_depth']==min_depth:
        return dict(old)
    version=old['version']+1 if old else 1
    if old:
        db.execute('INSERT INTO window_history(ship_class,base_start,base_end,state,reason,version) VALUES(?,?,?,?,?,?)',
                   (ship,start,end,old['state'],old['reason'],old['version']))
    db.execute('DELETE FROM windows WHERE ship_class=? AND base_start=? AND base_end=?',(ship,start,end))
    db.execute('INSERT INTO windows(ship_class,start,end,base_start,base_end,state,min_depth,reason,version) VALUES(?,?,?,?,?,?,?,?,?)',
               (ship,start,end,start,end,state,min_depth,reason,version))
    if state=='partially_safe':
        cursor=start
        for r in sorted(closed,key=lambda x:x['start']):
            cut_start=max(start,r['start']);cut_end=min(end,r['end'])
            if cursor<cut_start:
                db.execute('INSERT INTO windows(ship_class,start,end,base_start,base_end,state,min_depth,reason,version) VALUES(?,?,?,?,?,?,?,?,?)',
                           (ship,cursor,cut_start,start,end,'safe',min_depth,'open segment outside closure',version))
            cursor=max(cursor,cut_end)
        if cursor<end:
            db.execute('INSERT INTO windows(ship_class,start,end,base_start,base_end,state,min_depth,reason,version) VALUES(?,?,?,?,?,?,?,?,?)',
                       (ship,cursor,end,start,end,'safe',min_depth,'open segment outside closure',version))
    log(db,'waterway',ship,state,reason)
    from .resources import invalidate_window
    if old:
        invalidate_window(db,ship,start,end,'waterway version changed')
    return dict(current(db,ship,start,end))
