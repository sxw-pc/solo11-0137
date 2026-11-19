"""Independent offline container-handoff intake."""
from .source import record
from .store import log


def import_data(db, data):
    affected = set()
    with db:
        for event in data.get("handoffs", []):
            if event["from_party"] == event["to_party"]:
                raise ValueError("handoff needs a new custodian")
            _, new = record(db, event["event_id"], "handoff", event)
            if not new:
                continue
            db.execute(
                "INSERT INTO handoff_event VALUES (?,?,?,?,?,?,?)",
                (
                    event["event_id"],
                    event["container_id"],
                    event["from_party"],
                    event["to_party"],
                    event["terminal"],
                    event["at"],
                    event["seal"],
                ),
            )
            affected.add(event["container_id"])
        return [decide(db, container_id) for container_id in sorted(affected)]


def decide(db, container_id):
    events = db.execute(
        "SELECT * FROM handoff_event WHERE container_id=? ORDER BY at,event_id",
        (container_id,),
    ).fetchall()
    if not events:
        return {"container_id": container_id, "state": "missing"}
    custodian = events[0]["from_party"]
    seal = events[0]["seal"]
    state, reason = "confirmed", "continuous handoff chain"
    for event in events:
        if event["from_party"] != custodian or event["seal"] != seal:
            state, reason = "disputed", "custodian or seal continuity broken"
            break
        custodian = event["to_party"]
    if state == "disputed":
        custodian = None
    old = db.execute("SELECT * FROM custody WHERE container_id=?", (container_id,)).fetchone()
    last = events[-1]["event_id"]
    if not old or (old["custodian"], old["state"], old["reason"], old["last_event_id"]) != (
        custodian,
        state,
        reason,
        last,
    ):
        db.execute(
            "INSERT INTO custody VALUES (?,?,?,?,?) ON CONFLICT(container_id) DO UPDATE "
            "SET custodian=excluded.custodian,state=excluded.state,reason=excluded.reason,last_event_id=excluded.last_event_id",
            (container_id, custodian, state, reason, last),
        )
        log(db, "handoff", container_id, state, reason, last)
    return {"container_id": container_id, "custodian": custodian, "state": state, "reason": reason}

