"""Independent offline shore-power meter intake."""
from .source import record
from .store import log


def import_data(db, data):
    affected = set()
    with db:
        for item in data.get("readings", []):
            if item["kwh_total"] < 0:
                raise ValueError("meter total must be nonnegative")
            _, new = record(db, item["event_id"], "power-reading", item)
            if not new:
                continue
            db.execute(
                "INSERT INTO power_reading VALUES (?,?,?,?,?)",
                (item["event_id"], item["meter_id"], item["voyage"], item["at"], item["kwh_total"]),
            )
            affected.add(item["voyage"])
        return [decide(db, voyage) for voyage in sorted(affected)]


def decide(db, voyage):
    readings = db.execute(
        "SELECT * FROM power_reading WHERE voyage=? ORDER BY at,event_id", (voyage,)
    ).fetchall()
    if len(readings) < 2:
        kwh, state, reason = None, "pending", "need opening and closing meter readings"
    elif len({r["meter_id"] for r in readings}) != 1:
        kwh, state, reason = None, "disputed", "mixed meter IDs"
    elif any(b["kwh_total"] < a["kwh_total"] for a, b in zip(readings, readings[1:])):
        kwh, state, reason = None, "disputed", "meter total moved backwards"
    else:
        kwh = readings[-1]["kwh_total"] - readings[0]["kwh_total"]
        state, reason = "measured", "monotonic meter readings"
    old = db.execute("SELECT * FROM power_account WHERE voyage=?", (voyage,)).fetchone()
    if not old or (old["kwh"], old["state"], old["reason"]) != (kwh, state, reason):
        db.execute(
            "INSERT INTO power_account VALUES (?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET "
            "kwh=excluded.kwh,state=excluded.state,reason=excluded.reason",
            (voyage, kwh, state, reason),
        )
        log(db, "power", voyage, state, reason, readings[-1]["event_id"] if readings else None)
    return {"voyage": voyage, "kwh": kwh, "state": state, "reason": reason}

