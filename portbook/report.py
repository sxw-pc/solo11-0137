import json

TABLES=['evidence','notice_decisions','windows','window_history','allocations','fulfilments','occupancy','power_reading','power_account','decision_log']
def build(db):
    out={name:[dict(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in TABLES}
    for item in out['evidence']:
        item['raw']=item['raw'].decode('utf8')
    return out
def render(db):
    return json.dumps(build(db),ensure_ascii=False,indent=2,default=lambda x:x.decode('utf8') if isinstance(x,bytes) else str(x))

def audit(db):
    from .store import connect
    from . import notice, waterway, resources, arrival, power
    handlers={'notice-import':notice.import_data,'waterway-import':waterway.import_data,
              'resource-plan':resources.import_data,'arrival-import':arrival.import_data,
              'power-import':power.import_data}
    journal=db.execute('SELECT * FROM journal ORDER BY id').fetchall()
    if not journal: return {'journal_count':0,'replay_ok':False,'reason':'no CLI source files to replay'}
    fresh=connect(':memory:')
    try:
        import hashlib
        for entry in journal:
            if hashlib.sha256(entry['raw']).hexdigest()!=entry['sha']:
                return {'journal_count':len(journal),'replay_ok':False,'reason':'raw source file hash mismatch'}
            handlers[entry['command']](fresh,json.loads(entry['raw']))
        tables=['notice_decisions','windows','allocations','fulfilments','occupancy','power_account']
        mismatches=[]
        for name in tables:
            columns='voyage,state,reason,allocation_id,witness_id' if name=='fulfilments' else '*'
            left=[tuple(row) for row in db.execute(f'SELECT {columns} FROM {name} ORDER BY 1')]
            right=[tuple(row) for row in fresh.execute(f'SELECT {columns} FROM {name} ORDER BY 1')]
            if left!=right: mismatches.append(name)
        return {'journal_count':len(journal),'replay_ok':not mismatches,'mismatches':mismatches}
    finally:
        fresh.close()
