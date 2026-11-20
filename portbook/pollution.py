"""港区污染采样：值班预警与实验室正式签发双轨台账。

两条轨道服务不同目的，各自持久化、互不覆盖：

* ``pollution_cases`` 保存值班预警：把最新原始 reading 直接与签发 ceiling 比较，
  不做任何实验室质量修正，保证现场读数预警不被后续签发动作抹掉。
* ``pollution_verdicts`` 保存实验室正式签发结论：正式读数必须从同批标准液的
  实际测量反推，校准曲线只允许在通过质控的单调解释中选取，空白、加标回收、
  平行样和复测共同界定真实浓度的可能范围。旧签发全部进入
  ``pollution_verdict_history``，源事件保留在 ``evidence`` 中可追查。

质控范围不是把每一项误差各自取极值再拼成物理上不可能的最坏值：空白同时作用
于样本与加标测量，回收率约束的是加标增量，二者通过同一个空白响应耦合，必须
在联合可行域上取浓度极值。
"""
import itertools
import json

from .source import record
from .store import log

DEFAULT_UNIT = 'mg/L'
DEFAULT_TOLERANCE = 0.02

# 正式结论状态
PENDING = 'pending'            # 质控证据不足，不能签发
REVIEW = 'review_required'     # 两套同优校准解释给出相反必然结论，待复核
EXCEEDED = 'exceeded'          # 浓度下界高于限值，可签发超限
WITHIN = 'within_limit'        # 浓度上界不高于限值，可签发合格
INDETERMINATE = 'indeterminate'  # 单一解释下范围跨越限值，不能签发必然结论


def import_data(db, data):
    affected = set()
    imported = 0
    with db:
        for entry in data.get('limits', []):
            if entry['version'] < 1 or entry['ceiling'] < 0:
                raise ValueError('invalid pollution limit')
            _, new = record(db, entry['event_id'], 'pollution-limit', entry)
            if new:
                db.execute('INSERT INTO pollution_limits VALUES (?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['station'], entry['marker'],
                            entry['version'], entry['ceiling'],
                            entry.get('unit', DEFAULT_UNIT), entry['signer']))
                imported += 1
                affected.add((entry['station'], entry['marker']))
        for entry in data.get('samples', []):
            if entry['reading'] < 0:
                raise ValueError('invalid sample reading')
            _, new = record(db, entry['event_id'], 'pollution-sample', entry)
            if new:
                db.execute('INSERT INTO pollution_samples VALUES '
                           '(?,?,?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['station'], entry['marker'],
                            entry.get('batch_id', 'unbatched'), entry['sampled_at'],
                            entry['reading'], entry.get('unit', DEFAULT_UNIT),
                            entry.get('instrument'), entry['laboratory'],
                            entry.get('det_lower'), entry.get('det_upper')))
                imported += 1
                affected.add((entry['station'], entry['marker']))
        for entry in data.get('calibration_windows', []):
            _, new = record(db, entry['event_id'], 'calibration-window', entry)
            if new:
                db.execute('INSERT INTO pollution_calibration_windows VALUES '
                           '(?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['instrument'], entry['batch_id'],
                            entry['marker'], entry.get('unit', DEFAULT_UNIT),
                            entry['signer'], entry['start_at'], entry['end_at']))
                imported += 1
                _mark_batch(db, affected, entry['batch_id'], entry['marker'])
        for entry in data.get('standards', []):
            _, new = record(db, entry['event_id'], 'calibration-standard', entry)
            if new:
                db.execute('INSERT INTO pollution_standards VALUES '
                           '(?,?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['batch_id'], entry['marker'],
                            entry.get('unit', DEFAULT_UNIT), entry['instrument'],
                            entry['signer'], entry['measured_at'],
                            entry['known_conc'], entry['response'],
                            entry.get('tolerance', DEFAULT_TOLERANCE)))
                imported += 1
                _mark_batch(db, affected, entry['batch_id'], entry['marker'])
        for entry in data.get('standard_retests', []):
            _, new = record(db, entry['event_id'], 'standard-retest', entry)
            if new:
                std = db.execute('SELECT batch_id,marker FROM pollution_standards '
                                 'WHERE event_id=?', (entry['standard_event'],)).fetchone()
                if not std:
                    raise ValueError('retest target standard missing')
                db.execute('INSERT INTO pollution_standard_retests VALUES '
                           '(?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['standard_event'],
                            entry['batch_id'], entry['marker'], entry['instrument'],
                            entry['signer'], entry['measured_at'],
                            entry['response'], entry['reason']))
                imported += 1
                _mark_batch(db, affected, entry['batch_id'], entry['marker'])
        for entry in data.get('blanks', []):
            _, new = record(db, entry['event_id'], 'pollution-blank', entry)
            if new:
                db.execute('INSERT INTO pollution_blanks VALUES (?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['batch_id'], entry['marker'],
                            entry.get('unit', DEFAULT_UNIT), entry['instrument'],
                            entry['signer'], entry['measured_at'],
                            entry['response_lo'], entry['response_hi']))
                imported += 1
                _mark_batch(db, affected, entry['batch_id'], entry['marker'])
        for entry in data.get('spikes', []):
            _, new = record(db, entry['event_id'], 'pollution-spike', entry)
            if new:
                db.execute('INSERT INTO pollution_spikes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['batch_id'], entry['marker'],
                            entry.get('unit', DEFAULT_UNIT), entry['instrument'],
                            entry['signer'], entry['measured_at'],
                            entry['added_conc'], entry['response_lo'],
                            entry['response_hi'], entry['recovery_min'],
                            entry['recovery_max']))
                imported += 1
                _mark_batch(db, affected, entry['batch_id'], entry['marker'])
        for entry in data.get('parallels', []):
            _, new = record(db, entry['event_id'], 'pollution-parallel', entry)
            if new:
                sample = db.execute('SELECT station,marker,batch_id FROM pollution_samples '
                                    'WHERE event_id=?', (entry['sample_event'],)).fetchone()
                if not sample:
                    raise ValueError('parallel target sample missing')
                db.execute('INSERT INTO pollution_parallels VALUES (?,?,?,?,?,?,?,?,?)',
                           (entry['event_id'], entry['sample_event'], entry['batch_id'],
                            entry['marker'], entry['instrument'], entry['signer'],
                            entry['measured_at'], entry['response_lo'],
                            entry['response_hi']))
                imported += 1
                affected.add((sample['station'], sample['marker']))
        for entry in data.get('withdrawals', []):
            # 撤销必须指向已登记事件；旧证据保留，只影响后续重算
            target = db.execute('SELECT event_id FROM evidence WHERE event_id=?',
                                (entry['target_event_id'],)).fetchone()
            if not target:
                raise ValueError('withdrawal target missing')
            _, new = record(db, entry['event_id'], 'pollution-withdrawal', entry)
            if new:
                db.execute('INSERT INTO pollution_withdrawals VALUES (?,?,?,?,?,?)',
                           (entry['event_id'], entry['target_event_id'],
                            entry.get('batch_id'), entry.get('marker'),
                            entry['signer'], entry['reason']))
                imported += 1
                if entry.get('batch_id'):
                    _mark_batch(db, affected, entry['batch_id'], entry.get('marker'))
                else:
                    # 只给目标事件时，从其承载的批次反推受影响范围
                    for row in db.execute(
                            'SELECT DISTINCT batch_id,marker FROM pollution_standards '
                            'WHERE event_id=?', (entry['target_event_id'],)).fetchall():
                        _mark_batch(db, affected, row['batch_id'], row['marker'])
                    sample = db.execute('SELECT station,marker FROM pollution_samples '
                                        'WHERE event_id=?',
                                        (entry['target_event_id'],)).fetchone()
                    if sample:
                        affected.add((sample['station'], sample['marker']))
        cases = [decide(db, station, marker)
                 for station, marker in sorted(affected)]
    return {'imported': imported, 'cases': cases}


def _mark_batch(db, affected, batch_id, marker=None):
    rows = db.execute('SELECT station,marker FROM pollution_samples WHERE batch_id=?',
                      (batch_id,)).fetchall()
    for row in rows:
        if marker is None or row['marker'] == marker:
            affected.add((row['station'], row['marker']))


def _withdrawn(db, event_id):
    return db.execute('SELECT 1 FROM pollution_withdrawals WHERE target_event_id=?',
                      (event_id,)).fetchone() is not None


# ---------------------------------------------------------------- 值班预警

def decide(db, station, marker):
    """值班预警：最新原始 reading 对当前签发 ceiling，独立于正式签发。"""
    limits = db.execute('SELECT * FROM pollution_limits WHERE station=? AND marker=? '
                        'ORDER BY version DESC,event_id DESC',
                        (station, marker)).fetchall()
    samples = db.execute('SELECT * FROM pollution_samples WHERE station=? AND marker=? '
                         'ORDER BY sampled_at DESC,event_id DESC',
                         (station, marker)).fetchall()
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
        log(db, 'pollution-warning', f'{station}:{marker}', state,
            'latest raw sample against current signed limit', fields[1])
    warning = {'station': station, 'marker': marker, 'state': state,
               'sample_event': fields[1], 'limit_event': fields[2], 'excess': excess}
    formal = decide_formal(db, station, marker)
    warning['formal_state'] = formal['formal_state']
    return warning


# ------------------------------------------------------------ 正式签发引擎

def _active_samples(db, station, marker):
    return [s for s in db.execute(
        'SELECT * FROM pollution_samples WHERE station=? AND marker=? '
        'ORDER BY sampled_at DESC,event_id DESC', (station, marker)).fetchall()
        if not _withdrawn(db, s['event_id'])]


def decide_formal(db, station, marker):
    """依据整批校准、空白、加标、平行/复测的可核查质量状态给出正式结论。"""
    sample_rows = _active_samples(db, station, marker)
    sample = sample_rows[0] if sample_rows else None
    limit = db.execute('SELECT * FROM pollution_limits WHERE station=? AND marker=? '
                       'ORDER BY version DESC,event_id DESC',
                       (station, marker)).fetchone()
    if not sample or not limit:
        return _persist_formal(db, station, marker, None, PENDING, None, None,
                               None, sample, limit,
                               ['no active sample or no signed limit'])

    evidence = {'sample_event': sample['event_id'], 'limit_event': limit['event_id'],
                'batch_id': sample['batch_id'], 'standards': [], 'blanks': [],
                'spikes': [], 'parallels': [], 'windows': [], 'retests': [],
                'rejected_interpretations': [], 'interpretations': []}
    reasons = []

    if sample['unit'] != limit['unit']:
        reasons.append('sample unit does not match signed limit unit')
    batch_id = sample['batch_id']

    # 授权时段：校准样本与正式样本跨仪器、跨签发批次或超出授权时段不混用
    windows = [w for w in db.execute(
        'SELECT * FROM pollution_calibration_windows WHERE batch_id=? AND marker=?',
        (batch_id, marker)).fetchall() if not _withdrawn(db, w['event_id'])]
    evidence['windows'] = [w['event_id'] for w in windows]
    window = next((w for w in windows if w['instrument'] == sample['instrument']
                   and w['unit'] == sample['unit']
                   and w['start_at'] <= sample['sampled_at'] <= w['end_at']), None)
    if sample['instrument'] is None or window is None:
        reasons.append('sample outside authorized calibration window/instrument')

    standards = [s for s in db.execute(
        'SELECT * FROM pollution_standards WHERE batch_id=? AND marker=?',
        (batch_id, marker)).fetchall() if not _withdrawn(db, s['event_id'])]
    evidence['standards'] = [s['event_id'] for s in standards]
    if sample['instrument'] is not None:
        standards = [s for s in standards if s['instrument'] == sample['instrument']]
    if any(s['unit'] != sample['unit'] for s in standards):
        reasons.append('standard unit does not match sample unit')

    retests = [r for r in db.execute(
        'SELECT * FROM pollution_standard_retests WHERE batch_id=? AND marker=?',
        (batch_id, marker)).fetchall() if not _withdrawn(db, r['event_id'])]
    if sample['instrument'] is not None:
        retests = [r for r in retests if r['instrument'] == sample['instrument']]
    evidence['retests'] = [r['event_id'] for r in retests]
    retest_by_std = {}
    for r in retests:
        retest_by_std.setdefault(r['standard_event'], []).append(r)

    blanks = [b for b in db.execute(
        'SELECT * FROM pollution_blanks WHERE batch_id=? AND marker=?',
        (batch_id, marker)).fetchall() if not _withdrawn(db, b['event_id'])]
    if sample['instrument'] is not None:
        blanks = [b for b in blanks if b['instrument'] == sample['instrument']]
    evidence['blanks'] = [b['event_id'] for b in blanks]
    spikes = [p for p in db.execute(
        'SELECT * FROM pollution_spikes WHERE batch_id=? AND marker=?',
        (batch_id, marker)).fetchall() if not _withdrawn(db, p['event_id'])]
    if sample['instrument'] is not None:
        spikes = [p for p in spikes if p['instrument'] == sample['instrument']]
    evidence['spikes'] = [p['event_id'] for p in spikes]
    parallels = [p for p in db.execute(
        'SELECT * FROM pollution_parallels WHERE sample_event=?',
        (sample['event_id'],)).fetchall() if not _withdrawn(db, p['event_id'])]
    if sample['instrument'] is not None:
        parallels = [p for p in parallels if p['instrument'] == sample['instrument']]
    evidence['parallels'] = [p['event_id'] for p in parallels]

    if not standards:
        reasons.append('no active calibration standards for this batch/instrument')
    if not blanks:
        reasons.append('batch blank evidence required')
    if not spikes:
        reasons.append('spike recovery evidence required')
    if not parallels:
        reasons.append('parallel sample evidence required')
    if reasons:
        return _persist_formal(db, station, marker, batch_id, PENDING, None, None,
                               sample['unit'], sample, limit, reasons, evidence)

    # 样本实测响应区间：原始 reading 与平行样共同收紧，检出界硬截断
    resp_lo = min([sample['reading']] + [p['response_lo'] for p in parallels])
    resp_hi = max([sample['reading']] + [p['response_hi'] for p in parallels])
    if sample['det_lower'] is not None and resp_lo < sample['det_lower']:
        resp_lo = sample['det_lower']
        reasons.append('response clipped at lower detection bound')
    if sample['det_upper'] is not None and resp_hi > sample['det_upper']:
        resp_hi = sample['det_upper']
        reasons.append('response clipped at upper detection bound')

    blank_lo = min(b['response_lo'] for b in blanks)
    blank_hi = max(b['response_hi'] for b in blanks)
    if blank_lo < 0 or blank_hi < blank_lo:
        reasons.append('invalid blank interval')

    # 枚举所有满足质控要求的单调校准解释。
    # 异常标准点只有在存在独立复测证据时才允许隔离，不得为凑结论随意丢点。
    interpretations = _calibration_interpretations(standards, retest_by_std, evidence)
    if not interpretations:
        reasons.append('no monotone calibration interpretation satisfies QC')
        return _persist_formal(db, station, marker, batch_id, PENDING, None, None,
                               sample['unit'], sample, limit, reasons, evidence)

    # 最大可保留证据量：丢点最少的解释优先
    best_score = max(item['score'] for item in interpretations)
    maximal = [item for item in interpretations if item['score'] == best_score]

    verdicts = []
    for item in maximal:
        lo, hi, spike_ok, calc_reasons = _concentration_range(
            item['line'], resp_lo, resp_hi, blank_lo, blank_hi, spikes)
        item['conc_lo'], item['conc_hi'] = lo, hi
        item['spike_recovery_ok'] = spike_ok
        evidence['interpretations'].append({
            'retained_standards': item['retained_standard_events'],
            'retained_evidence': item['retained'],
            'isolated_standards': item['isolated'],
            'slope': item['line'][0], 'intercept': item['line'][1],
            'concentration_lo': lo, 'concentration_hi': hi,
            'spike_recovery_ok': spike_ok})
        if not spike_ok:
            reasons.extend(calc_reasons)
            continue
        if lo is None:
            reasons.extend(calc_reasons)
            continue
        if hi <= limit['ceiling']:
            verdicts.append(WITHIN)
        elif lo > limit['ceiling']:
            verdicts.append(EXCEEDED)
        else:
            verdicts.append(INDETERMINATE)

    if not verdicts:
        return _persist_formal(db, station, marker, batch_id, PENDING, None, None,
                               sample['unit'], sample, limit, reasons, evidence)

    # 两套同优解释、一套必然超限而另一套允许不超限：必须待复核并展示两套证据，
    # 不能按文件顺序择一
    if EXCEEDED in verdicts and WITHIN in verdicts:
        return _persist_formal(db, station, marker, batch_id, REVIEW, None, None,
                               sample['unit'], sample, limit,
                               ['equally supported calibration interpretations split '
                                'the formal conclusion'], evidence)

    # 任一最大证据量解释无法定论，就不能签发必然结论
    if INDETERMINATE in verdicts or len(set(verdicts)) > 1:
        lo = min(item['conc_lo'] for item in maximal if item['conc_lo'] is not None)
        hi = max(item['conc_hi'] for item in maximal if item['conc_hi'] is not None)
        return _persist_formal(db, station, marker, batch_id, INDETERMINATE, lo, hi,
                               sample['unit'], sample, limit,
                               ['concentration range crosses the signed ceiling'],
                               evidence)

    state = verdicts[0]
    lo = min(item['conc_lo'] for item in maximal)
    hi = max(item['conc_hi'] for item in maximal)
    return _persist_formal(db, station, marker, batch_id, state, lo, hi,
                           sample['unit'], sample, limit, reasons, evidence)


def _ols_line(points):
    n = len(points)
    mean_c = sum(c for c, _ in points) / n
    mean_r = sum(r for _, r in points) / n
    denom = sum((c - mean_c) ** 2 for c, _ in points)
    if denom == 0:
        return None
    slope = sum((c - mean_c) * (r - mean_r) for c, r in points) / denom
    intercept = mean_r - slope * mean_c
    return slope, intercept


def _calibration_interpretations(standards, retest_by_std, evidence):
    """枚举通过质控的单调校准解释。

    每个标准点有三类候选：

    * 保留原始实测响应；
    * 存在独立复测时，用复测响应替代污染/错标的原始响应（证据仍保留）；
    * 存在独立复测时，隔离该点。

    没有复测支持的异常点既不能替代也不能隔离，只能保留并接受质控否决。
    保留（含复测替代）的点必须按已知浓度严格单调，最小二乘直线斜率为正且
    残差在该批允差内。分数等于保留的证据点数，隔离才丢证据，杜绝为凑结论
    随意丢点。
    """
    ordered = sorted(standards, key=lambda s: (s['known_conc'], s['event_id']))
    candidates = []
    for std in ordered:
        choices = [('keep', std['event_id'], std['response'], std['event_id'])]
        for retest in retest_by_std.get(std['event_id'], []):
            choices.append(('replace', std['event_id'], retest['response'],
                            retest['event_id']))
            choices.append(('isolate', std['event_id'], None, retest['event_id']))
        candidates.append(choices)

    interpretations = []
    seen = set()
    for combo in itertools.product(*candidates):
        retained = [(event, resp, source)
                    for action, event, resp, source in combo
                    if action in ('keep', 'replace')]
        isolated = [event for action, event, _, _ in combo if action == 'isolate']
        points = [(next(s['known_conc'] for s in ordered if s['event_id'] == event),
                   resp) for event, resp, _ in retained]
        if len(points) < 2:
            evidence['rejected_interpretations'].append(
                {'isolated': isolated, 'reason': 'fewer than two retained standards'})
            continue
        if any(b[0] <= a[0] for a, b in zip(points, points[1:])) or \
                any(b[1] <= a[1] for a, b in zip(points, points[1:])):
            evidence['rejected_interpretations'].append(
                {'isolated': isolated, 'reason': 'retained standards not monotone'})
            continue
        line = _ols_line(points)
        if line is None or line[0] <= 0:
            evidence['rejected_interpretations'].append(
                {'isolated': isolated, 'reason': 'non-positive calibration slope'})
            continue
        tolerance = max(s['tolerance'] for s in ordered
                        if s['event_id'] in {e for e, _, _ in retained})
        if any(abs(r - (line[0] * c + line[1])) > tolerance + 1e-12
               for c, r in points):
            evidence['rejected_interpretations'].append(
                {'isolated': isolated, 'reason': 'residual exceeds batch tolerance'})
            continue
        key = tuple(sorted(source for _, _, source in retained))
        if key in seen:
            continue
        seen.add(key)
        interpretations.append({
            'retained': sorted(source for _, _, source in retained),
            'retained_standard_events': sorted(event for event, _, _ in retained),
            'isolated': sorted(isolated),
            'score': len(retained), 'line': line})
    return interpretations


def _concentration_range(line, resp_lo, resp_hi, blank_lo, blank_hi, spikes):
    """在空白与加标回收的联合可行域上求真实浓度范围。

    空白响应 b 同时从样本响应和加标响应中扣除；回收率约束的是加标增量
    (r_sp - r_s) 反推的浓度增量。因此 b 与回收率通过同一批测量耦合：
    对每个 (r_s, b) 组合，加标回收带直接限定可接受的 Δ 区间，不能先把
    各项误差独立取极值再拼成物理上不可能的最坏值。
    """
    slope, intercept = line
    reasons = []
    feasible = []

    delta_bands = []  # 每个加标事件允许的浓度增量区间
    for spike in spikes:
        band_lo = spike['added_conc'] / spike['recovery_max']
        band_hi = spike['added_conc'] / spike['recovery_min']
        if band_lo <= 0 or band_hi < band_lo:
            reasons.append('invalid spike recovery band')
            return None, None, False, reasons
        delta_bands.append((spike, band_lo, band_hi))

    # 浓度 = (r_s - b - intercept) / slope，对 r_s、b 均为线性，
    # 联合可行域是带线性约束的盒式多面体，极值落在端点枚举上。
    for r_s, b in itertools.product((resp_lo, resp_hi), (blank_lo, blank_hi)):
        c_s = (r_s - b - intercept) / slope
        for spike, band_lo, band_hi in delta_bands:
            # 该 (r_s) 下实测加标差值能达到的 Δ 范围
            delta_meas_lo = (spike['response_lo'] - r_s) / slope
            delta_meas_hi = (spike['response_hi'] - r_s) / slope
            overlap_lo = max(band_lo, delta_meas_lo)
            overlap_hi = min(band_hi, delta_meas_hi)
            if overlap_hi < overlap_lo - 1e-12:
                c_s = None  # 该联合点不满足回收约束
                break
        if c_s is None:
            continue
        feasible.append(max(0.0, c_s))

    if not feasible:
        reasons.append('no joint blank/recovery feasible region')
        return None, None, False, reasons

    # 检出界是仪器响应单位的界，已在调用处对响应区间硬截断；
    # 此处不再对反推浓度二次截断
    lo, hi = min(feasible), max(feasible)
    if lo > hi + 1e-12:
        reasons.append('joint QC constraints produced an empty interval')
        return None, None, False, reasons
    return lo, hi, True, reasons


def _persist_formal(db, station, marker, batch_id, state, conc_lo, conc_hi, unit,
                    sample, limit, reasons, evidence=None):
    evidence = dict(evidence or {})
    evidence['reasons'] = reasons
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
    sample_event = sample['event_id'] if sample else None
    limit_event = limit['event_id'] if limit else None
    new_fields = (state, conc_lo, conc_hi, unit, sample_event, limit_event, payload)
    old = db.execute('SELECT formal_state,conc_lo,conc_hi,unit,sample_event,'
                     'limit_event,evidence FROM pollution_verdicts WHERE station=? '
                     'AND marker=?', (station, marker)).fetchone()
    if not old or tuple(old) != new_fields:
        # 旧签发原样进 history，源事件仍在 evidence 中可追查
        if old:
            db.execute('INSERT INTO pollution_verdict_history'
                       '(station,marker,batch_id,formal_state,conc_lo,conc_hi,unit,'
                       'sample_event,limit_event,evidence) '
                       'SELECT station,marker,batch_id,formal_state,conc_lo,conc_hi,'
                       'unit,sample_event,limit_event,evidence FROM pollution_verdicts '
                       'WHERE station=? AND marker=?', (station, marker))
        db.execute('INSERT INTO pollution_verdicts'
                   '(station,marker,batch_id,formal_state,conc_lo,conc_hi,unit,'
                   'sample_event,limit_event,evidence) VALUES (?,?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(station,marker) DO UPDATE SET batch_id=excluded.batch_id,'
                   'formal_state=excluded.formal_state,conc_lo=excluded.conc_lo,'
                   'conc_hi=excluded.conc_hi,unit=excluded.unit,'
                   'sample_event=excluded.sample_event,limit_event=excluded.limit_event,'
                   'evidence=excluded.evidence,updated_at=CURRENT_TIMESTAMP',
                   (station, marker, batch_id, *new_fields))
        log(db, 'pollution-formal', f'{station}:{marker}', state,
            '; '.join(reasons) or 'formal lab verdict', sample_event)
    return {'station': station, 'marker': marker, 'formal_state': state,
            'batch_id': batch_id, 'conc_lo': conc_lo, 'conc_hi': conc_hi,
            'unit': unit, 'sample_event': sample_event,
            'limit_event': limit_event, 'reasons': reasons, 'evidence': evidence}
