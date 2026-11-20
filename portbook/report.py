import json

TABLES=['evidence','notice_decisions','windows','window_history','allocations','fulfilments','occupancy','decision_log',
        'pollution_limits','pollution_samples','pollution_cases','pollution_verdicts','pollution_verdict_history',
        'pollution_calibration_windows','pollution_standards','pollution_standard_retests','pollution_blanks',
        'pollution_spikes','pollution_parallels','pollution_withdrawals']

# 含时间戳默认值的表，重放比较时只比对业务列，避免 CURRENT_TIMESTAMP 造成伪差异
_COMPARE_COLUMNS={
    'pollution_verdicts':('station','marker','batch_id','formal_state','conc_lo','conc_hi',
                          'unit','sample_event','limit_event','evidence'),
    'pollution_verdict_history':('station','marker','batch_id','formal_state','conc_lo','conc_hi',
                                 'unit','sample_event','limit_event','evidence'),
}

def pollution_overview(db):
    """港区总览：值班预警与实验室正式签发逐点对照。

    relation 取值：
      same       预警与正式签发结论一致（同为超限或同为合格）
      different  两者必然结论相反
      unresolved 正式结论待证（质控不足/跨限/待复核）或预警本身待证、争议
    每行指向两条轨道分别使用的限值、样本、标准液与质控事件。
    """
    rows=[]
    cases=db.execute('SELECT * FROM pollution_cases ORDER BY station,marker').fetchall()
    for case in cases:
        verdict=db.execute('SELECT * FROM pollution_verdicts WHERE station=? AND marker=?',
                           (case['station'],case['marker'])).fetchone()
        formal=verdict['formal_state'] if verdict else 'pending'
        warning=case['state']
        if warning in ('pending','disputed') or formal in ('pending','indeterminate','review_required'):
            relation='unresolved'
        elif warning==formal:
            relation='same'
        else:
            relation='different'
        refs={'limit_event':case['limit_event'],'sample_event':case['sample_event'],
              'standards':[],'blanks':[],'spikes':[],'parallels':[],
              'windows':[],'retests':[],'interpretations':[]}
        conc_lo=conc_hi=unit=batch_id=None
        if verdict:
            conc_lo,conc_hi,unit,batch_id=(verdict['conc_lo'],verdict['conc_hi'],
                                           verdict['unit'],verdict['batch_id'])
            try:
                detail=json.loads(verdict['evidence'])
                for key in ('standards','blanks','spikes','parallels','windows','retests'):
                    refs[key]=detail.get(key,[])
                refs['interpretations']=detail.get('interpretations',[])
            except (TypeError,json.JSONDecodeError):
                pass
        rows.append({'station':case['station'],'marker':case['marker'],'batch_id':batch_id,
                     'warning_state':warning,'formal_state':formal,'relation':relation,
                     'warning_limit_event':case['limit_event'],
                     'warning_sample_event':case['sample_event'],
                     'formal_limit_event':verdict['limit_event'] if verdict else None,
                     'formal_sample_event':verdict['sample_event'] if verdict else None,
                     'concentration_lo':conc_lo,'concentration_hi':conc_hi,'unit':unit,
                     'evidence_refs':refs})
    return rows

def build(db):
    out={name:[dict(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in TABLES}
    for item in out['evidence']:
        item['raw']=item['raw'].decode('utf8')
    out['pollution_overview']=pollution_overview(db)
    return out
def render(db):
    return json.dumps(build(db),ensure_ascii=False,indent=2,default=lambda x:x.decode('utf8') if isinstance(x,bytes) else str(x))

def audit(db):
    from .store import connect
    from . import notice, waterway, resources, arrival, pollution
    handlers={'notice-import':notice.import_data,'waterway-import':waterway.import_data,
              'resource-plan':resources.import_data,'arrival-import':arrival.import_data,
              'pollution-import':pollution.import_data}
    journal=db.execute('SELECT * FROM journal ORDER BY id').fetchall()
    if not journal: return {'journal_count':0,'replay_ok':False,'reason':'no CLI source files to replay'}
    fresh=connect(':memory:')
    try:
        import hashlib
        for entry in journal:
            if hashlib.sha256(entry['raw']).hexdigest()!=entry['sha']:
                return {'journal_count':len(journal),'replay_ok':False,'reason':'raw source file hash mismatch'}
            handlers[entry['command']](fresh,json.loads(entry['raw']))
        tables=['notice_decisions','windows','allocations','fulfilments','occupancy',
                'pollution_limits','pollution_samples','pollution_cases','pollution_verdicts',
                'pollution_verdict_history','pollution_calibration_windows','pollution_standards',
                'pollution_standard_retests','pollution_blanks','pollution_spikes',
                'pollution_parallels','pollution_withdrawals']
        mismatches=[]
        for name in tables:
            columns=_COMPARE_COLUMNS.get(name)
            select=','.join(columns) if columns else ('voyage,state,reason,allocation_id,witness_id' if name=='fulfilments' else '*')
            left=[tuple(row) for row in db.execute(f'SELECT {select} FROM {name} ORDER BY 1')]
            right=[tuple(row) for row in fresh.execute(f'SELECT {select} FROM {name} ORDER BY 1')]
            if left!=right: mismatches.append(name)
        return {'journal_count':len(journal),'replay_ok':not mismatches,'mismatches':mismatches}
    finally:
        fresh.close()
