import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, command TEXT NOT NULL, sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(signer TEXT PRIMARY KEY, source TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), voyage TEXT NOT NULL, revision INTEGER NOT NULL, eta TEXT NOT NULL, draft REAL NOT NULL, cancelled INTEGER NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS withdrawals(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), target_event_id TEXT NOT NULL REFERENCES revisions(event_id), reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_decisions(voyage TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER, event_id TEXT, reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS gauges(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), station TEXT NOT NULL, at TEXT NOT NULL, datum TEXT NOT NULL, depth REAL NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS restrictions(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), order_id TEXT NOT NULL, version INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, signer TEXT NOT NULL, revoked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS windows(ship_class TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, base_start TEXT NOT NULL, base_end TEXT NOT NULL, state TEXT NOT NULL, min_depth REAL, reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(ship_class,start,end));
CREATE TABLE IF NOT EXISTS window_history(id INTEGER PRIMARY KEY, ship_class TEXT NOT NULL, base_start TEXT NOT NULL, base_end TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS berths(id TEXT PRIMARY KEY, depth REAL NOT NULL, ship_class TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tugs(id TEXT PRIMARY KEY, power REAL NOT NULL, qualified INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY, voyage TEXT NOT NULL, berth TEXT NOT NULL, tug TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, priority INTEGER NOT NULL, draft REAL NOT NULL, margin REAL NOT NULL, required_power REAL NOT NULL, formal INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, notice_version INTEGER, window_version INTEGER);
CREATE TABLE IF NOT EXISTS witnesses(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), voyage TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS occupancy(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), voyage TEXT NOT NULL, berth TEXT NOT NULL, start TEXT NOT NULL, end TEXT);
CREATE TABLE IF NOT EXISTS fulfilments(voyage TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, allocation_id TEXT, witness_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY, subsystem TEXT NOT NULL, object_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, evidence_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS pollution_limits(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), station TEXT NOT NULL, marker TEXT NOT NULL, version INTEGER NOT NULL, ceiling REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'mg/L', signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_samples(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), station TEXT NOT NULL, marker TEXT NOT NULL, batch_id TEXT NOT NULL DEFAULT 'unbatched', sampled_at TEXT NOT NULL, reading REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'mg/L', instrument TEXT, laboratory TEXT NOT NULL, det_lower REAL, det_upper REAL);
-- 值班预警：只拿原始 reading 与签发 ceiling 比较，不经过实验室质控
CREATE TABLE IF NOT EXISTS pollution_cases(station TEXT NOT NULL, marker TEXT NOT NULL, state TEXT NOT NULL, sample_event TEXT, limit_event TEXT, excess REAL, PRIMARY KEY(station,marker));
-- 实验室离线质控采录：同批标准液实测值、复测、空白、加标回收、平行样、校准授权时段
CREATE TABLE IF NOT EXISTS pollution_calibration_windows(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), instrument TEXT NOT NULL, batch_id TEXT NOT NULL, marker TEXT NOT NULL, unit TEXT NOT NULL, signer TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_standards(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), batch_id TEXT NOT NULL, marker TEXT NOT NULL, unit TEXT NOT NULL, instrument TEXT NOT NULL, signer TEXT NOT NULL, measured_at TEXT NOT NULL, known_conc REAL NOT NULL, response REAL NOT NULL, tolerance REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_standard_retests(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), standard_event TEXT NOT NULL REFERENCES pollution_standards(event_id), batch_id TEXT NOT NULL, marker TEXT NOT NULL, instrument TEXT NOT NULL, signer TEXT NOT NULL, measured_at TEXT NOT NULL, response REAL NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_blanks(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), batch_id TEXT NOT NULL, marker TEXT NOT NULL, unit TEXT NOT NULL, instrument TEXT NOT NULL, signer TEXT NOT NULL, measured_at TEXT NOT NULL, response_lo REAL NOT NULL, response_hi REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_spikes(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), batch_id TEXT NOT NULL, marker TEXT NOT NULL, unit TEXT NOT NULL, instrument TEXT NOT NULL, signer TEXT NOT NULL, measured_at TEXT NOT NULL, added_conc REAL NOT NULL, response_lo REAL NOT NULL, response_hi REAL NOT NULL, recovery_min REAL NOT NULL, recovery_max REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_parallels(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), sample_event TEXT NOT NULL REFERENCES pollution_samples(event_id), batch_id TEXT NOT NULL, marker TEXT NOT NULL, instrument TEXT NOT NULL, signer TEXT NOT NULL, measured_at TEXT NOT NULL, response_lo REAL NOT NULL, response_hi REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pollution_withdrawals(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), target_event_id TEXT NOT NULL, batch_id TEXT, marker TEXT, signer TEXT NOT NULL, reason TEXT NOT NULL);
-- 正式签发结论：与预警分开存储，任何时候不得互相覆盖；history 保留全部旧签发
CREATE TABLE IF NOT EXISTS pollution_verdicts(station TEXT NOT NULL, marker TEXT NOT NULL, batch_id TEXT, formal_state TEXT NOT NULL, conc_lo REAL, conc_hi REAL, unit TEXT, sample_event TEXT, limit_event TEXT, evidence TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(station,marker));
CREATE TABLE IF NOT EXISTS pollution_verdict_history(id INTEGER PRIMARY KEY, station TEXT NOT NULL, marker TEXT NOT NULL, batch_id TEXT, formal_state TEXT NOT NULL, conc_lo REAL, conc_hi REAL, unit TEXT, sample_event TEXT, limit_event TEXT, evidence TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db

def log(db, subsystem, object_id, state, reason, evidence_id=None):
    db.execute("INSERT INTO decision_log(subsystem,object_id,state,reason,evidence_id) VALUES(?,?,?,?,?)",
               (subsystem, object_id, state, reason, evidence_id))

def upsert_decision(db, table, key, value, state, reason, evidence_id=None):
    old = db.execute(f"SELECT version,state,reason FROM {table} WHERE {key}=?", (value,)).fetchone()
    if old and old['state'] == state and old['reason'] == reason:
        return old['version']
    version = (old['version'] + 1) if old else 1
    if table == 'notice_decisions':
        db.execute("INSERT INTO notice_decisions(voyage,state,reason,event_id,version) VALUES(?,?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET state=excluded.state,reason=excluded.reason,event_id=excluded.event_id,version=excluded.version",
                   (value, state, reason, evidence_id, version))
    else:
        raise ValueError(table)
    log(db, table, value, state, reason, evidence_id)
    return version
