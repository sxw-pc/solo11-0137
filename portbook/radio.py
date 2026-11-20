"""Independent radio order ledger.

The legacy simple status (``radio_case``) is preserved untouched for old
reports: it matches acknowledgements only by command_id and can mistake a
stale receipt for a new revision.  The formal effectiveness certification
(``radio_cert``) and the after-the-fact personal receipt certification
(``radio_personal``) are causal: they order events only through explicit
parent links, receipt-to-order bindings and per-station local sequence
numbers, never through wall-clock timestamps or import order.
"""
import json
from .source import record
from .store import log

ACTIONS = {'hold', 'proceed', 'cancel'}
PHASES = {'prepare', 'commit', 'revoke'}
ROLES = ('pilot', 'tug')
RESPONSES = {'heard', 'confirm', 'refuse'}


def _check_directive(item):
    parents = item.get('parents', [])
    if (not isinstance(item['revision'], int) or item['revision'] < 1
            or item['phase'] not in PHASES or item['action'] not in ACTIONS
            or not item['digest'] or not item['scope'] or not item['issuer']
            or not isinstance(parents, list)
            or not all(isinstance(p, str) for p in parents)):
        raise ValueError('invalid radio directive')


def check_receipt(item):
    parents = item.get('parents', [])
    seqs_ok = all(isinstance(item[k], int) and item[k] >= 1
                  for k in ('sender_seq', 'receiver_seq'))
    if (item['response'] not in RESPONSES or item['role'] not in ROLES
            or not seqs_ok or not item['digest'] or not item['order_event']
            or not isinstance(parents, list)
            or not all(isinstance(p, str) for p in parents)):
        raise ValueError('invalid radio receipt')


def import_data(db, data):
    order_affected = set()
    cert_affected = set()
    imported = 0
    with db:
        for item in data.get('orders', []):
            if item['revision'] < 1 or item['action'] not in ACTIONS:
                raise ValueError('invalid radio order')
            _, fresh = record(db, item['event_id'], 'radio-order', item)
            if fresh:
                db.execute('INSERT INTO radio_order VALUES (?,?,?,?,?,?,?)',
                           (item['event_id'], item['command_id'], item['voyage'],
                            item['revision'], item['action'], item['issuer'],
                            item['issued_at']))
                imported += 1
                order_affected.add(item['voyage'])
        for auth in data.get('authorities', []):
            db.execute('INSERT INTO radio_authority VALUES (?,?,?,?) '
                       'ON CONFLICT(issuer,scope) DO UPDATE SET '
                       'valid_from=excluded.valid_from,valid_to=excluded.valid_to',
                       (auth['issuer'], auth['scope'],
                        auth['valid_from'], auth['valid_to']))
        for item in data.get('directives', []):
            _check_directive(item)
            _, fresh = record(db, item['event_id'], 'radio-directive', item)
            if fresh:
                db.execute('INSERT INTO radio_directive VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (item['event_id'], item['command_id'], item['voyage'],
                            item['revision'], item['phase'], item['action'],
                            item['digest'], item['issuer'], item['scope'],
                            json.dumps(sorted(item.get('parents', []))),
                            item['issued_at']))
                imported += 1
                cert_affected.add(item['voyage'])
        cases = [decide(db, voyage) for voyage in sorted(order_affected)]
        voyages = None if data.get('authorities') else cert_affected | order_affected
        formal = certify_voyages(db, voyages)
    return {'imported': imported, 'cases': cases, **formal}


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


def _authorized(db, issuer, scope, at):
    return bool(db.execute('SELECT 1 FROM radio_authority WHERE issuer=? AND scope=? '
                           'AND valid_from<=? AND valid_to>=?',
                           (issuer, scope, at, at)).fetchone())


def _graph(db, voyage):
    """Causal graph over a voyage's directive and receipt events.

    Edges come only from explicit parents, receipt-to-order bindings and the
    sender's local sequence chain; unknown references are kept as missing
    edges so conclusions can stay pending instead of guessing an order.
    """
    directives = [dict(r) for r in db.execute(
        'SELECT * FROM radio_directive WHERE voyage=? ORDER BY event_id', (voyage,))]
    receipts = [dict(r) for r in db.execute(
        'SELECT * FROM radio_receipt WHERE voyage=? ORDER BY event_id', (voyage,))]
    nodes = {}
    for d in directives:
        d['parents'] = json.loads(d['parents'])
        nodes[d['event_id']] = ('directive', d)
    for r in receipts:
        r['parents'] = json.loads(r['parents'])
        nodes[r['event_id']] = ('receipt', r)
    edges = {e: set() for e in nodes}
    missing = {e: set() for e in nodes}

    def link(child, parent):
        (edges if parent in nodes else missing)[child].add(parent)

    for e, (kind, ev) in nodes.items():
        for p in ev['parents']:
            link(e, p)
        if kind == 'receipt':
            link(e, ev['order_event'])
    by_actor = {}
    for r in receipts:
        by_actor.setdefault(r['actor'], []).append(r)
    for rs in by_actor.values():
        rs.sort(key=lambda r: (r['sender_seq'], r['event_id']))
        for prev, cur in zip(rs, rs[1:]):
            edges[cur['event_id']].add(prev['event_id'])
    return nodes, edges, missing


def _ancestors(edges):
    anc = {}
    for e in edges:
        seen = set()
        stack = list(edges[e])
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(edges.get(x, ()))
        anc[e] = seen
    cycles = sorted(e for e in edges if e in anc[e])
    return anc, cycles


def _evaluate(db, command_id, nodes, anc, note):
    """Poset semantics for one command: valid commits plus competing vetoes
    and revokes.  A definite conclusion requires a unique maximal decisive
    event, i.e. every causal interpretation agrees on it."""
    dirs = sorted((ev for _, (k, ev) in nodes.items()
                   if k == 'directive' and ev['command_id'] == command_id),
                  key=lambda d: d['event_id'])
    recs = sorted((ev for _, (k, ev) in nodes.items()
                   if k == 'receipt' and ev['command_id'] == command_id),
                  key=lambda r: r['event_id'])
    preps = {}
    for d in dirs:
        if d['phase'] == 'prepare':
            preps.setdefault(d['revision'], {})[d['digest']] = d
    if not preps:
        note('no prepare')
    conflicts = sorted(r for r, m in preps.items() if len(m) > 1)
    for r in conflicts:
        note(f'parallel revision body conflict at revision {r}')
    status = {}
    for rev in sorted(preps):
        if rev in conflicts:
            continue
        for dg, prep in sorted(preps[rev].items()):
            pid = prep['event_id']
            if not _authorized(db, prep['issuer'], prep['scope'], prep['issued_at']):
                note(f"prepare {pid} by unauthorized issuer {prep['issuer']}")
                continue
            confirms = {}
            for r in recs:
                if r['order_event'] != pid or r['response'] != 'confirm':
                    continue
                if r['digest'] != dg:
                    note(f"stale digest on receipt {r['event_id']}")
                    continue
                confirms.setdefault(r['role'], {}).setdefault(
                    r['actor'], []).append(r['event_id'])
            role_ok = True
            conf_events = {}
            for role in ROLES:
                actors = confirms.get(role, {})
                if not actors:
                    note(f'missing confirmation from {role} for prepare {pid}')
                    role_ok = False
                elif len(actors) > 1:
                    note(f"duplicate role {role} confirmed by {','.join(sorted(actors))}")
                    role_ok = False
                else:
                    conf_events[role] = actors[next(iter(actors))]
            status[pid] = (role_ok, conf_events)
    valid = []
    commits = [d for d in dirs if d['phase'] == 'commit']
    if not commits:
        note('no commit')
    for c in commits:
        cid = c['event_id']
        if not _authorized(db, c['issuer'], c['scope'], c['issued_at']):
            note(f"commit {cid} by unauthorized issuer {c['issuer']}")
            continue
        if c['revision'] in conflicts:
            note(f'commit {cid} targets conflicted revision {c["revision"]}')
            continue
        prep = preps.get(c['revision'], {}).get(c['digest'])
        if prep is None:
            note(f'commit {cid} without matching prepare')
            continue
        st = status.get(prep['event_id'])
        if st is None:
            continue
        role_ok, conf_events = st
        if not role_ok:
            continue
        causal = True
        for role in ROLES:
            if not any(e in anc[cid] for e in conf_events[role]):
                note(f'commit {cid} not causally after {role} confirmation')
                causal = False
        if causal:
            valid.append(c)
    if not valid:
        return 'possible', None, None, None
    committed = {(c['revision'], c['digest']) for c in valid}
    decisive = {c['event_id']: ('commit', c) for c in valid}
    for r in recs:
        if r['response'] != 'refuse':
            continue
        target = nodes.get(r['order_event'])
        if target and (target[1]['revision'], target[1]['digest']) in committed:
            decisive[r['event_id']] = ('veto', r)
    for d in dirs:
        if d['phase'] != 'revoke':
            continue
        if not _authorized(db, d['issuer'], d['scope'], d['issued_at']):
            note(f"revoke {d['event_id']} by unauthorized issuer {d['issuer']}")
            continue
        if (d['revision'], d['digest']) in committed:
            decisive[d['event_id']] = ('revoke', d)
    maximal = [e for e in decisive
               if not any(e != f and e in anc[f] for f in decisive)]
    max_commits = [e for e in maximal if decisive[e][0] == 'commit']
    if len(maximal) == len(max_commits):
        contents = {(decisive[e][1]['revision'], decisive[e][1]['digest'])
                    for e in maximal}
        if len(contents) == 1:
            chosen = decisive[sorted(maximal)[-1]][1]
            note('authorized commit causally after pilot and tug confirmations')
            return 'effective', chosen['revision'], chosen['digest'], chosen['event_id']
        note('concurrent commits with different content: ' + ','.join(sorted(maximal)))
        return 'possible', None, None, None
    if max_commits:
        for e in sorted(maximal):
            kind, ev = decisive[e]
            if kind == 'veto':
                note(f"concurrent commit and safety veto by {ev['actor']} ({e})")
            elif kind == 'revoke':
                note(f'concurrent commit and revoke ({e})')
        return 'possible', None, None, None
    for e in sorted(maximal):
        kind, ev = decisive[e]
        if kind == 'veto':
            note(f"safety veto by {ev['actor']} ({e}) causally after commit")
        else:
            note(f'revoke ({e}) causally after commit')
    last = max(valid, key=lambda c: (c['revision'], c['event_id']))
    return 'vetoed', last['revision'], last['digest'], last['event_id']


def certify(db, voyage, command_id):
    nodes, edges, missing = _graph(db, voyage)
    anc, cycles = _ancestors(edges)
    own = {e for e, (_, ev) in nodes.items() if ev['command_id'] == command_id}
    relevant = set(own)
    for e in own:
        relevant |= anc[e]
    reasons = []

    def note(text):
        if text not in reasons:
            reasons.append(text)

    state = revision = digest = commit_event = None
    cyc = [e for e in cycles if e in relevant]
    if cyc:
        note('causal cycle involving ' + ','.join(cyc))
        state = 'possible'
    else:
        state, revision, digest, commit_event = _evaluate(
            db, command_id, nodes, anc, note)
    missed = sorted({p for e in relevant for p in missing.get(e, ())})
    if missed:
        note('missing causal edge to ' + ','.join(missed))
        state = 'possible'
    reason = '; '.join(reasons) if reasons else 'insufficient evidence'
    edge_json = json.dumps(sorted([e, p] for e in own for p in edges[e]))
    fields = (state, reason, revision, digest, commit_event, edge_json)
    old = db.execute('SELECT * FROM radio_cert WHERE voyage=? AND command_id=?',
                     (voyage, command_id)).fetchone()
    if old and (old['state'], old['reason'], old['revision'], old['digest'],
                old['commit_event'], old['edges']) == fields:
        return dict(old)
    version = old['version'] + 1 if old else 1
    db.execute('INSERT INTO radio_cert(voyage,command_id,state,reason,revision,digest,'
               'commit_event,edges,version) VALUES (?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(voyage,command_id) DO UPDATE SET state=excluded.state,'
               'reason=excluded.reason,revision=excluded.revision,digest=excluded.digest,'
               'commit_event=excluded.commit_event,edges=excluded.edges,'
               'version=excluded.version',
               (voyage, command_id, state, reason, revision, digest, commit_event,
                edge_json, version))
    log(db, 'radio-cert', voyage + ':' + command_id, state, reason, commit_event)
    return dict(db.execute('SELECT * FROM radio_cert WHERE voyage=? AND command_id=?',
                           (voyage, command_id)).fetchone())


def certify_personal(db, voyage, actor):
    """After-the-fact check of one executor's own receipt chain: which
    directives the actor actually acknowledged, in their local order."""
    recs = [dict(r) for r in db.execute(
        'SELECT * FROM radio_receipt WHERE voyage=? AND actor=? '
        'ORDER BY sender_seq,event_id', (voyage, actor))]
    reasons = []

    def note(text):
        if text not in reasons:
            reasons.append(text)

    by_seq = {}
    for r in recs:
        by_seq.setdefault(r['sender_seq'], []).append(r['event_id'])
    for seq, ids in sorted(by_seq.items()):
        if len(ids) > 1:
            note(f'forked sender sequence {seq}')
    if by_seq:
        for n in range(1, max(by_seq) + 1):
            if n not in by_seq:
                note(f'gap in sender sequence {n}')
    received = []
    for r in recs:
        d = db.execute('SELECT revision,digest,phase,action FROM radio_directive '
                       'WHERE event_id=?', (r['order_event'],)).fetchone()
        entry = {'receipt': r['event_id'], 'order_event': r['order_event'],
                 'response': r['response'], 'sender_seq': r['sender_seq']}
        if d is None:
            note(f"receipt {r['event_id']} binds unknown order event {r['order_event']}")
            entry['known'] = False
        else:
            entry.update(dict(d))
            entry['known'] = True
            if d['digest'] != r['digest']:
                note(f"stale digest on receipt {r['event_id']}")
        received.append(entry)
    state = 'complete' if not reasons else 'incomplete'
    reason = '; '.join(reasons) if reasons else 'consistent local receipt chain'
    last = recs[-1] if recs else None
    fields = (state, reason, last['order_event'] if last else None,
              last['response'] if last else None, last['sender_seq'] if last else None)
    old = db.execute('SELECT * FROM radio_personal WHERE voyage=? AND actor=?',
                     (voyage, actor)).fetchone()
    if old and (old['state'], old['reason'], old['last_order_event'],
                old['last_response'], old['last_seq']) == fields:
        out = dict(old)
    else:
        version = old['version'] + 1 if old else 1
        db.execute('INSERT INTO radio_personal(voyage,actor,state,reason,last_order_event,'
                   'last_response,last_seq,version) VALUES (?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(voyage,actor) DO UPDATE SET state=excluded.state,'
                   'reason=excluded.reason,last_order_event=excluded.last_order_event,'
                   'last_response=excluded.last_response,last_seq=excluded.last_seq,'
                   'version=excluded.version', (voyage, actor, *fields, version))
        log(db, 'radio-personal', voyage + ':' + actor, state, reason,
            last['order_event'] if last else None)
        out = dict(db.execute('SELECT * FROM radio_personal WHERE voyage=? AND actor=?',
                              (voyage, actor)).fetchone())
    out['received'] = received
    return out


def certify_voyages(db, voyages=None):
    if voyages is None:
        voyages = [r[0] for r in db.execute(
            'SELECT voyage FROM radio_directive UNION SELECT voyage FROM radio_receipt '
            'ORDER BY 1')]
    certs = []
    personal = []
    for voyage in sorted(voyages):
        commands = [r[0] for r in db.execute(
            'SELECT command_id FROM radio_directive WHERE voyage=? UNION '
            'SELECT command_id FROM radio_receipt WHERE voyage=? ORDER BY 1',
            (voyage, voyage))]
        certs.extend(certify(db, voyage, c) for c in commands)
        actors = [r[0] for r in db.execute(
            'SELECT DISTINCT actor FROM radio_receipt WHERE voyage=? ORDER BY 1',
            (voyage,))]
        personal.extend(certify_personal(db, voyage, a) for a in actors)
    return {'certs': certs, 'personal': personal}
