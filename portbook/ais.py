"""Independent offline AIS ping intake and basic voyage-track summary."""
from .source import record
from .store import log


def import_data(db, data):
    affected = set()
    with db:
        for item in data.get("pings", []):
            if not -90 <= item["lat"] <= 90 or not -180 <= item["lon"] <= 180:
                raise ValueError("invalid AIS position")
            _, new = record(db, item["event_id"], "ais-ping", item)
            if not new:
                continue
            db.execute(
                "INSERT INTO ais_ping VALUES (?,?,?,?,?,?,?)",
                (item["event_id"], item["mmsi"], item["voyage"], item["at"],
                 item["lat"], item["lon"], item["receiver"]),
            )
            affected.add(item["voyage"])
        return [decide(db, voyage) for voyage in sorted(affected)]


def decide(db, voyage):
    pings = db.execute(
        "SELECT * FROM ais_ping WHERE voyage=? ORDER BY at,event_id", (voyage,)
    ).fetchall()
    if not pings:
        return {"voyage": voyage, "state": "missing"}
    mmsis = {ping["mmsi"] for ping in pings}
    if len(mmsis) != 1:
        state, reason = "disputed", "multiple reported MMSI values"
    elif len(pings) < 2:
        state, reason = "pending", "need more than one AIS position"
    else:
        state, reason = "reported", "basic track available"
    last_at = pings[-1]["at"]
    old = db.execute("SELECT * FROM ais_track WHERE voyage=?", (voyage,)).fetchone()
    if not old or (old["pings"], old["last_at"], old["state"], old["reason"]) != (
        len(pings), last_at, state, reason
    ):
        db.execute(
            "INSERT INTO ais_track VALUES (?,?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET "
            "pings=excluded.pings,last_at=excluded.last_at,state=excluded.state,reason=excluded.reason",
            (voyage, len(pings), last_at, state, reason),
        )
        log(db, "ais", voyage, state, reason, pings[-1]["event_id"])
    return {"voyage": voyage, "pings": len(pings), "last_at": last_at, "state": state, "reason": reason}

