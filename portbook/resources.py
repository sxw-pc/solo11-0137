import hashlib
import json
from itertools import combinations
from datetime import datetime
from .source import record
from .store import log

DELEGATION_KINDS = ('emergency', 'throughput')

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

def _candidate(db,a,exclude_id=None,exclude_ids=None):
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
    excluded=set(exclude_ids or ())
    if exclude_id: excluded.add(exclude_id)
    placeholders=','.join('?' for _ in excluded) if excluded else "''"
    occupied=db.execute(f"SELECT * FROM allocations WHERE state='confirmed' AND id NOT IN ({placeholders})",tuple(excluded)).fetchall()
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

# ---------------------------------------------------------------------------
# One-shot group dispatch under an authorized delegation contract.
#
# Two distinct contracts share no sorting switch:
#   emergency  -> honour the single highest-grade (priority) eligible ship;
#   throughput -> honour as many voyages as the safety boundary permits,
#                 with a deterministic, explainable tie-break.
# Hard constraints are exactly those already recorded by the notice and
# waterway ledgers plus the physical occupancy boundary; no confirmed plan is
# ever moved and no expired window is treated as usable.
# ---------------------------------------------------------------------------

def _canonical(rows):
    return sorted((dict(r) for r in rows), key=lambda a:(-a['priority'], a['start'], a['id']))

def _resolve_candidates(db, request):
    ids=request.get('candidate_ids')
    if ids:
        rows=[db.execute('SELECT * FROM allocations WHERE id=?',(i,)).fetchone() for i in ids]
        missing=[i for i,r in zip(ids,rows) if r is None]
        if missing: raise ValueError('unknown candidate allocation: '+','.join(missing))
        return _canonical(rows)
    rows=db.execute("SELECT * FROM allocations WHERE state='waiting'").fetchall()
    if request.get('start'): rows=[r for r in rows if r['start']>=request['start']]
    if request.get('end'):   rows=[r for r in rows if r['end']<=request['end']]
    if request.get('berth'): rows=[r for r in rows if r['berth']==request['berth']]
    if request.get('tug'):   rows=[r for r in rows if r['tug']==request['tug']]
    rows=_canonical(rows)
    if not rows: raise ValueError('delegation candidate set is empty')
    return rows

def _pairwise_conflict(a, b):
    if not overlaps(a['start'],a['end'],b['start'],b['end']): return None
    if a['berth']==b['berth']: return 'berth occupied'
    if a['tug']==b['tug']: return 'tug occupied'
    return None

def _eligible_with_reasons(db, candidates, exclude_ids):
    """Apply the ledgered hard constraints to every candidate once."""
    eligible, reasons = [], {}
    for a in candidates:
        reason, nv, wv = _candidate(db, a, exclude_ids=exclude_ids)
        if reason is None:
            a = dict(a); a['_notice_version']=nv; a['_window_version']=wv
            eligible.append(a)
        else:
            reasons[a['id']] = reason
    return eligible, reasons

def _throughput_packs(eligible):
    """Feasible packs in canonical enumeration order: larger sets first,
    then higher aggregate priority, then lexicographic allocation ids."""
    packs=[]
    for size in range(len(eligible), 0, -1):
        for combo in combinations(eligible, size):
            clash=None
            for x,y in combinations(combo, 2):
                clash=_pairwise_conflict(x,y)
                if clash: break
            if clash is None:
                packs.append(list(combo))
        if packs: break
    if not packs: return []
    best_priority=max(sum(a['priority'] for a in p) for p in packs)
    packs=[p for p in packs if sum(a['priority'] for a in p)==best_priority]
    packs.sort(key=lambda p: tuple(sorted(a['id'] for a in p)))
    return packs

def _adjudicate(db, kind, candidates, exclude_ids):
    eligible, hard_reasons = _eligible_with_reasons(db, candidates, exclude_ids)
    selected, why = [], {}
    if kind == 'emergency':
        if eligible:
            chosen = eligible[0]  # canonical order: highest priority, then start, id
            selected = [chosen]
            why[chosen['id']] = (
                f"emergency delegation: highest priority {chosen['priority']} among eligible candidates")
    elif kind == 'throughput':
        packs = _throughput_packs(eligible)
        if packs:
            selected = packs[0]
            for a in selected:
                why[a['id']] = (
                    f"throughput delegation: member of maximum feasible pack of {len(selected)} voyages; "
                    f"tie-break kept highest aggregate priority then smallest allocation id")
    else:
        raise ValueError(f'unknown delegation kind {kind!r}; expected one of {DELEGATION_KINDS}')
    selected_ids = {a['id'] for a in selected}
    for a in eligible:
        if a['id'] in selected_ids: continue
        clash = next((_pairwise_conflict(a, s) for s in selected if _pairwise_conflict(a, s)), None)
        if clash:
            why[a['id']] = 'not selected: ' + clash + ' by a chosen voyage'
        elif kind == 'emergency':
            why[a['id']] = f"not selected: emergency delegation honours one ship and {selected[0]['id']} outranks it"
        else:
            why[a['id']] = 'not selected: excluded by maximum-voyage pack tie-break'
    for a in candidates:
        if a['id'] in selected_ids or a['id'] in why: continue
        clash = next((_pairwise_conflict(a, s) for s in selected if _pairwise_conflict(a, s)), None)
        why[a['id']] = 'not selected: ' + (clash if clash else 'lower ranked feasible alternative')
    return selected, hard_reasons, why

def preview_group(db, request):
    """Dry-run the whole feasible plan without writing any ledger row."""
    kind = request['kind']
    if kind not in DELEGATION_KINDS:
        raise ValueError(f'unknown delegation kind {kind!r}; expected one of {DELEGATION_KINDS}')
    if 'id' not in request: raise ValueError('delegation id is required')
    candidates = _resolve_candidates(db, request)
    candidate_ids = [a['id'] for a in candidates]
    exclude_ids = set(candidate_ids)  # the group is decided as one transaction
    selected, hard_reasons, why = _adjudicate(db, kind, candidates, exclude_ids)
    return {
        'id': request['id'], 'kind': kind,
        'candidate_ids': candidate_ids,
        'selected_ids': [a['id'] for a in selected],
        'state': 'selected' if selected else 'no feasible candidate',
        'reason': f'{kind} delegation over {len(candidate_ids)} candidates',
        'items': _item_rows(db, candidates, selected, hard_reasons, why),
    }

def _item_rows(db, candidates, selected, hard_reasons, why):
    selected_map = {a['id']: a for a in selected}
    rows = []
    for a in candidates:
        if a['id'] in selected_map:
            s = selected_map[a['id']]
            rows.append({'id': a['id'], 'state': 'selected', 'reason': why[a['id']],
                         'notice_version': s['_notice_version'], 'window_version': s['_window_version']})
        else:
            rows.append({'id': a['id'], 'state': 'rejected',
                         'reason': hard_reasons.get(a['id']) or why.get(a['id'], 'not selected'),
                         'notice_version': None, 'window_version': None})
    return rows

def dispatch_request(db, request):
    """Core one-shot adjudication. Runs inside the caller's transaction so
    the arrival ledger can commit evidence and the group verdict together."""
    existing = db.execute('SELECT * FROM delegations WHERE id=?', (request['id'],)).fetchone()
    if existing:
        stored_sha = existing['sha']
        if stored_sha != _request_sha(request):
            raise ValueError(f"delegation id {request['id']!r} already records different evidence")
        items = db.execute('SELECT * FROM delegation_items WHERE delegation_id=? ORDER BY allocation_id',
                           (request['id'],)).fetchall()
        return {'id': request['id'], 'kind': existing['kind'],
                'candidate_ids': json.loads(existing['candidate_ids']),
                'selected_ids': json.loads(existing['selected_ids']),
                'state': 'idempotent', 'reason': existing['reason'],
                'items': [dict(i) for i in items]}
    plan = preview_group(db, request)
    evidence_id, _ = record(db, 'delegation:' + request['id'], 'delegation', request)
    selected_ids = plan['selected_ids']
    state = 'selected' if selected_ids else 'no feasible candidate'
    db.execute('INSERT INTO delegations(id,kind,sha,state,reason,candidate_ids,selected_ids,evidence_id) VALUES(?,?,?,?,?,?,?,?)',
               (request['id'], request['kind'], _request_sha(request), state, plan['reason'],
                json.dumps(plan['candidate_ids']), json.dumps(selected_ids), evidence_id))
    for item in plan['items']:
        db.execute('INSERT INTO delegation_items(delegation_id,allocation_id,state,reason,notice_version,window_version) VALUES(?,?,?,?,?,?)',
                   (request['id'], item['id'], item['state'], item['reason'],
                    item['notice_version'], item['window_version']))
        if item['state'] == 'selected':
            db.execute("UPDATE allocations SET state='confirmed',reason=?,notice_version=?,window_version=? WHERE id=?",
                       (f"confirmed by {request['kind']} delegation {request['id']}",
                        item['notice_version'], item['window_version'], item['id']))
        log(db, 'delegation', f"{request['id']}:{item['id']}", item['state'], item['reason'], evidence_id)
    log(db, 'delegation', request['id'], state, plan['reason'], evidence_id)
    plan['state'] = state
    return plan

def _request_sha(request):
    raw = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    return hashlib.sha256(raw).hexdigest()

def group_dispatch(db, request):
    """Confirm one whole group atomically; the same evidence never confirms twice."""
    with db:
        return dispatch_request(db, request)

def import_delegations(db, data):
    """Batch entry used by the CLI and by the audit replay journal."""
    requests = data.get('delegations') or ([data] if data.get('kind') else [])
    out = []
    with db:
        for request in requests:
            out.append(dispatch_request(db, request))
    return out

def recompute_group(db, delegation_id):
    """Re-run the recorded delegation against the *current* ledgers without
    touching the stored confirmation, so drift after a notice/window change
    is visible but the original verdict can never be silently rewritten."""
    stored = db.execute('SELECT * FROM delegations WHERE id=?', (delegation_id,)).fetchone()
    if not stored: raise ValueError(f'unknown delegation {delegation_id!r}')
    ids = json.loads(stored['candidate_ids'])
    candidates = _canonical(
        r for r in (db.execute('SELECT * FROM allocations WHERE id=?', (i,)).fetchone() for i in ids) if r)
    selected, hard_reasons, why = _adjudicate(db, stored['kind'], candidates, set(ids))
    return {
        'id': delegation_id, 'kind': stored['kind'],
        'candidate_ids': ids,
        'stored_selected_ids': json.loads(stored['selected_ids']),
        'selected_ids': [a['id'] for a in selected],
        'changed': [a['id'] for a in selected] != json.loads(stored['selected_ids']),
        'state': 'selected' if selected else 'no feasible candidate',
        'items': _item_rows(db, candidates, selected, hard_reasons, why),
    }
