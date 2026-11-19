import hashlib
import json
from pathlib import Path

def read(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), raw

def record_file(db, path, command):
    raw=Path(path).read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    with db:
        db.execute('INSERT OR IGNORE INTO evidence(event_id,kind,sha,raw) VALUES(?,?,?,?)',
                   ('file:'+digest,'source-file',digest,raw))
        db.execute('INSERT INTO journal(command,sha,raw) VALUES(?,?,?)',(command,digest,raw))
    return digest

def record(db, event_id, kind, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    digest = hashlib.sha256(raw).hexdigest()
    old = db.execute('SELECT sha FROM evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        if old['sha'] != digest:
            raise ValueError(f'event id {event_id} changed bytes')
        return digest, False
    db.execute('INSERT INTO evidence(event_id,kind,sha,raw) VALUES(?,?,?,?)',
               (event_id, kind, digest, raw))
    return digest, True
