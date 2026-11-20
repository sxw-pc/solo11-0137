"""Offline shore-power metering.

Two ledgers are kept strictly separate:

* the ship's own meter (``power-import`` ``readings``) is the only evidence a
  dedicated-meter contract can be settled on;
* a shared transformer meter is segmented against the actual occupied berth
  intervals (from :mod:`portbook.arrival`) and only apportioned when every
  contractual evidence obligation (load cap, share weight, readings, connection
  interval) is satisfied.

Raw meter readings are sealed in ``evidence`` and never rewritten; every
allocation is recorded per segment so the two ledgers always reconcile to the
trusted transformer increment.
"""
import hashlib
import json
from datetime import datetime
from fractions import Fraction

from .source import record
from .store import log


def _did(*parts):
    """Deterministic row id so replay audits reproduce identical ledgers."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf8")).hexdigest()


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

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
        for item in data.get("contracts", []):
            if item["kind"] not in ("dedicated", "shared"):
                raise ValueError("contract kind must be dedicated or shared")
            _, new = record(db, item["event_id"], "power-contract", item)
            if new:
                db.execute(
                    "INSERT INTO power_contract VALUES (?,?,?,?,?,?,?)",
                    (item["event_id"], item["voyage"], item["kind"], item.get("berth"),
                     item.get("weight"), item.get("load_cap"), item["signed_at"]),
                )
                affected.add(item["voyage"])
        for item in data.get("transformers", []):
            _, new = record(db, item["event_id"], "power-transformer", item)
            if new:
                berths = json.dumps(sorted(item["berths"]), separators=(",", ":"))
                db.execute(
                    "INSERT INTO power_transformer VALUES (?,?,?)",
                    (item["event_id"], item["meter_id"], berths),
                )
                affected.update(_voyages_for_meter(db, item["meter_id"]))
        for item in data.get("transformer_readings", []):
            if item["kwh_total"] < 0:
                raise ValueError("meter total must be nonnegative")
            _, new = record(db, item["event_id"], "transformer-reading", item)
            if new:
                db.execute(
                    "INSERT INTO transformer_reading VALUES (?,?,?,?)",
                    (item["event_id"], item["meter_id"], item["at"], item["kwh_total"]),
                )
                affected.update(_voyages_for_meter(db, item["meter_id"]))
        for item in data.get("outages", []):
            _, new = record(db, item["event_id"], "power-outage", item)
            if new:
                db.execute(
                    "INSERT INTO power_outage(event_id,meter_id,start,end,resumed_by) "
                    "VALUES (?,?,?,?,?)",
                    (item["event_id"], item["meter_id"], item["start"], item["end"],
                     item.get("resumed_by")),
                )
                affected.update(_voyages_for_meter(db, item["meter_id"]))
        recomputed = set()
        out = []
        for voyage in sorted(affected):
            decision = decide(db, voyage, _recomputed=recomputed)
            if decision is not None:
                out.append(decision)
        return out


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

def decide(db, voyage, _recomputed=None):
    """Recompute the power account for one voyage.

    Dedicated-meter contracts (and voyages without a shared contract) keep the
    original single-meter cumulative-reading ledger.  Shared contracts are
    settled through the transformer segmentation ledger, which reads actual
    occupancy evidence written by :mod:`portbook.arrival`.
    """
    if _recomputed is None:
        _recomputed = set()
    contract = _latest_contract(db, voyage)
    if contract and contract["kind"] == "shared":
        meter_id = _meter_for_voyage(db, voyage)
        if meter_id and meter_id not in _recomputed:
            _recompute_meter(db, meter_id)
            _recomputed.add(meter_id)
        return _refresh_account(db, voyage)
    return _dedicated_account(db, voyage)


# ---------------------------------------------------------------------------
# dedicated / single-meter ledger (original behaviour)
# ---------------------------------------------------------------------------

def _dedicated_account(db, voyage):
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
    basis = "dedicated ship meter" if kwh is not None else "pending: dedicated ship meter"
    _write_account(db, voyage, kwh, state, reason, kwh if kwh is not None else None,
                   kwh if kwh is not None else None, basis)
    return {"voyage": voyage, "kwh": kwh, "state": state, "reason": reason}


# ---------------------------------------------------------------------------
# shared transformer ledger
# ---------------------------------------------------------------------------

def _recompute_meter(db, meter_id):
    """Rebuild every segment/attribution row for one transformer meter."""
    transformer = db.execute(
        "SELECT * FROM power_transformer WHERE meter_id=?", (meter_id,)
    ).fetchone()
    if transformer is None:
        return
    berths = set(json.loads(transformer["berths"]))

    # Deterministic latest occupancy window per voyage on the served berths:
    # the most recent on-site evidence wins, never a lexical event-id accident.
    windows = {}
    if berths:
        for occ in db.execute(
            "SELECT * FROM occupancy WHERE berth IN (%s) "
            "ORDER BY start, COALESCE(end, ''), event_id"
            % ",".join("?" for _ in berths), tuple(berths)
        ).fetchall():
            windows[occ["voyage"]] = (occ["start"], occ["end"], occ["event_id"])

    contracts = {v: _latest_contract(db, v) for v in windows}
    readings = db.execute(
        "SELECT * FROM transformer_reading WHERE meter_id=? ORDER BY at,event_id",
        (meter_id,),
    ).fetchall()

    # Rebuild deterministically; raw transformer_reading rows stay sealed.
    db.execute("DELETE FROM power_attribution WHERE segment_id IN "
               "(SELECT id FROM power_segment WHERE meter_id=?)", (meter_id,))
    db.execute("DELETE FROM power_segment WHERE meter_id=?", (meter_id,))

    if len(readings) < 2:
        _segment_uncovered_occupancy(db, meter_id, windows, readings,
                                     "need opening and closing transformer readings")
        return

    last_ts = readings[-1]["at"]
    outages = db.execute(
        "SELECT * FROM power_outage WHERE meter_id=? ORDER BY start,event_id", (meter_id,)
    ).fetchall()

    for a, b in zip(readings, readings[1:]):
        increment = b["kwh_total"] - a["kwh_total"]
        trusted, reason = _interval_trust(db, a, b, readings, outages)
        seg_id = _did("segment", meter_id, a["at"], b["at"])
        if not trusted:
            db.execute(
                "INSERT INTO power_segment VALUES (?,?,?,?,?,?,?)",
                (seg_id, meter_id, a["at"], b["at"], None, 0, reason),
            )
            for voyage, (start, end, ev) in sorted(windows.items()):
                end_eff = end or last_ts
                if _overlap(a["at"], b["at"], start, end_eff):
                    _flush(db, seg_id, a["at"], b["at"], None,
                           [(voyage, _vessel(voyage), f"pending: {reason}",
                             Fraction(0), Fraction(0), ev)])
            continue
        _build_pieces(db, seg_id, meter_id, a["at"], b["at"], Fraction(increment),
                      last_ts, windows, contracts)

    _segment_uncovered_occupancy(db, meter_id, windows, readings,
                                 "connection interval outside trusted transformer readings")


def _interval_trust(db, a, b, readings, outages):
    """Trust only the affected interval; verified earlier consumption survives."""
    if b["kwh_total"] < a["kwh_total"]:
        return False, "meter total moved backwards"
    # Same-timestamp conflicting readings poison only the touching intervals.
    for r in readings:
        if r["event_id"] != b["event_id"] and r["at"] == b["at"] \
                and r["kwh_total"] != b["kwh_total"]:
            return False, "conflicting readings at same timestamp"
    overlapping = [out for out in outages
                   if _overlap(a["at"], b["at"], out["start"], out["end"])]
    if overlapping:
        # A re-metering reading inside the interval restores trust for the
        # whole outage window, regardless of which outage record links it.
        resumed = False
        for out in overlapping:
            resume = db.execute(
                "SELECT at FROM transformer_reading WHERE event_id=?",
                (out["resumed_by"],)).fetchone() if out["resumed_by"] else None
            if resume is not None and a["at"] < resume["at"] <= b["at"]:
                resumed = True
                break
        if not resumed:
            return False, "outage without re-metering evidence"
    return True, "trusted transformer increment"


def _build_pieces(db, seg_id, meter_id, seg_start, seg_end, increment, last_ts,
                  windows, contracts):
    db.execute(
        "INSERT INTO power_segment VALUES (?,?,?,?,?,?,?)",
        (seg_id, meter_id, seg_start, seg_end, float(increment), 1,
         "trusted transformer increment"),
    )
    # Cut at every connection boundary inside the meter interval.
    cuts = {seg_start, seg_end}
    for start, end, _ in windows.values():
        end_eff = end or last_ts
        if seg_start < start < seg_end:
            cuts.add(start)
        if seg_start < end_eff < seg_end:
            cuts.add(end_eff)
    cuts = sorted(cuts)
    for piece_start, piece_end in zip(cuts, cuts[1:]):
        duration = _seconds(piece_start, piece_end)
        if duration <= 0:
            continue
        seg_duration = _seconds(seg_start, seg_end)
        piece_increment = increment * Fraction(duration, seg_duration)
        _allot_piece(db, seg_id, piece_start, piece_end, piece_increment,
                     last_ts, windows, contracts)


def _allot_piece(db, seg_id, start, end, piece_increment, last_ts, windows, contracts):
    connected = []
    for voyage, (w_start, w_end, ev) in windows.items():
        w_end_eff = w_end or last_ts
        if w_start < end and start < w_end_eff:
            connected.append((voyage, ev))
    if not connected:
        _flush(db, seg_id, start, end, piece_increment,
               [(None, "dock", "dock base load", piece_increment, piece_increment, None)])
        return

    # Dedicated ships are only ever billed on their own meter evidence.
    exclusive, shared_here = [], []
    for voyage, ev in connected:
        c = contracts.get(voyage)
        if c is None or c["kind"] == "dedicated":
            exclusive.append((voyage, ev, c))
        else:
            shared_here.append((voyage, ev, c))

    remaining = piece_increment
    rows = []
    for voyage, ev, contract in exclusive:
        measured = _prorated_own_meter(db, voyage, start, end)
        if measured is None:
            rows.append((voyage, _vessel(voyage),
                         "pending: dedicated contract awaiting ship meter evidence",
                         Fraction(0), Fraction(0), ev))
        else:
            used = min(measured, remaining)
            remaining -= used
            rows.append((voyage, _vessel(voyage),
                         "dedicated ship meter measurement", used, used, ev))

    if not shared_here:
        if remaining > 0:
            rows.append((None, "dock", "dock base load", remaining, remaining, None))
        _flush(db, seg_id, start, end, piece_increment, rows)
        return

    # A shared contract may only be apportioned when every ship in the piece
    # carries an issued weight and load cap; otherwise ranges only.
    incomplete = any(c is None or c["weight"] is None or c["load_cap"] is None
                     for _, _, c in shared_here)
    if incomplete:
        total_weight = sum(Fraction(str(c["weight"])) for _, _, c in shared_here
                           if c is not None and c["weight"] is not None) or Fraction(1)
        for voyage, ev, c in shared_here:
            w = Fraction(str(c["weight"])) if c is not None and c["weight"] is not None \
                else Fraction(1)
            upper = remaining * w / total_weight
            rows.append((voyage, _vessel(voyage),
                         "pending: no issued apportionment basis for connected ships",
                         Fraction(0), upper, ev))
        if remaining > 0:
            rows.append((None, "dock", "dock base load pending attribution",
                         remaining, remaining, None))
        _flush(db, seg_id, start, end, piece_increment, rows)
        return

    # Fully evidenced apportionment by signed share weights.
    total_weight = sum(Fraction(str(c["weight"])) for _, _, c in shared_here)
    desired = {v: remaining * Fraction(str(c["weight"])) / total_weight
               for v, _, c in shared_here}
    caps = {v: _piece_cap(c, start, end) for v, _, c in shared_here}
    firm, clipped = {}, set()
    for voyage, _, _ in shared_here:
        if desired[voyage] > caps[voyage]:
            firm[voyage] = caps[voyage]
            clipped.add(voyage)
        else:
            firm[voyage] = desired[voyage]
    if clipped:
        # Load caps bind: the overflow cannot be silently reassigned.  Give the
        # range each ship may still take, leave the rest unallocated.
        for voyage, ev, _ in shared_here:
            upper = min(desired[voyage], caps[voyage])
            rows.append((voyage, _vessel(voyage),
                         "pending: contract load cap exceeded; overflow unallocated",
                         firm[voyage], upper, ev))
        overflow = remaining - sum(firm.values(), Fraction(0))
        if overflow > 0:
            rows.append((None, "unallocated", "pending: load-cap overflow",
                         overflow, overflow, None))
        _flush(db, seg_id, start, end, piece_increment, rows)
        return

    for voyage, ev, _ in shared_here:
        rows.append((voyage, _vessel(voyage),
                     "shared transformer apportionment by signed weights",
                     firm[voyage], firm[voyage], ev))
    leftover = remaining - sum(firm.values(), Fraction(0))
    if leftover > 0:
        rows.append((None, "dock", "dock base load", leftover, leftover, None))
    _flush(db, seg_id, start, end, piece_increment, rows)


def _flush(db, seg_id, start, end, total, rows):
    """Insert piece rows; stored floats reconcile exactly to the piece total."""
    if total is None:
        # Untrusted segment: no measurable increment to distribute.
        for idx, (voyage, recipient, basis, amount, upper, evidence_id) in enumerate(rows):
            db.execute(
                "INSERT INTO power_attribution(id,segment_id,voyage,recipient,start,end,"
                "kwh,kwh_max,basis,evidence_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_did("attribution", seg_id, start, end, voyage or recipient, idx),
                 seg_id, voyage, recipient, start, end, 0.0, 0.0,
                 basis, evidence_id),
            )
        return
    used = Fraction(0)
    for i, (voyage, recipient, basis, amount, upper, evidence_id) in enumerate(rows):
        if i == len(rows) - 1:
            kwh = float(total - used)
            kwh_max = kwh if recipient in ("dock", "unallocated") else float(min(total - used, upper))
        else:
            kwh = float(amount)
            kwh_max = float(upper)
            used += amount
        db.execute(
            "INSERT INTO power_attribution(id,segment_id,voyage,recipient,start,end,"
            "kwh,kwh_max,basis,evidence_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_did("attribution", seg_id, start, end, voyage or recipient, i),
             seg_id, voyage, recipient, start, end, kwh, kwh_max,
             basis, evidence_id),
        )


def _piece_cap(contract, start, end):
    """Signed load cap (kWh per hour) prorated to the piece duration."""
    hours = Fraction(_seconds(start, end), 3600)
    return Fraction(str(contract["load_cap"])) * hours


def _prorated_own_meter(db, voyage, start, end):
    readings = db.execute(
        "SELECT * FROM power_reading WHERE voyage=? ORDER BY at,event_id", (voyage,)
    ).fetchall()
    if len(readings) < 2 or len({r["meter_id"] for r in readings}) != 1:
        return None
    if any(b["kwh_total"] < a["kwh_total"] for a, b in zip(readings, readings[1:])):
        return None
    total = readings[-1]["kwh_total"] - readings[0]["kwh_total"]
    span = _seconds(readings[0]["at"], readings[-1]["at"])
    if span <= 0:
        return None
    return Fraction(total) * Fraction(_seconds(start, end), span)


def _segment_uncovered_occupancy(db, meter_id, windows, readings, reason):
    """Connection intervals with no trusted meter evidence around them."""
    if not windows or not readings:
        seg_id = _did("segment", meter_id, "uncovered", reason)
        db.execute(
            "INSERT INTO power_segment VALUES (?,?,?,?,?,?,?)",
            (seg_id, meter_id,
             readings[0]["at"] if readings else "",
             readings[-1]["at"] if readings else "", None, 0, reason),
        )
        for voyage, (start, end, ev) in sorted(windows.items()):
            end_eff = end or (readings[-1]["at"] if readings else start)
            _flush(db, seg_id, start, end_eff, None,
                   [(voyage, _vessel(voyage), f"pending: {reason}",
                     Fraction(0), Fraction(0), ev)])
        return
    first_ts, last_ts = readings[0]["at"], readings[-1]["at"]
    uncovered = []
    for voyage, (start, end, ev) in sorted(windows.items()):
        end_eff = end or last_ts
        if start < first_ts or end_eff > last_ts:
            uncovered.append((voyage, max(start, first_ts), min(end_eff, last_ts), ev))
    if not uncovered:
        return
    seg_id = _did("segment", meter_id, "uncovered", reason)
    db.execute(
        "INSERT INTO power_segment VALUES (?,?,?,?,?,?,?)",
        (seg_id, meter_id, first_ts, last_ts, None, 0, reason),
    )
    for voyage, start, end_eff, ev in uncovered:
        _flush(db, seg_id, start, end_eff, None,
               [(voyage, _vessel(voyage), f"pending: {reason}",
                 Fraction(0), Fraction(0), ev)])


def _refresh_account(db, voyage):
    rows = db.execute(
        "SELECT * FROM power_attribution WHERE voyage=? ORDER BY start,end,id",
        (voyage,),
    ).fetchall()
    contract = _latest_contract(db, voyage)
    if not rows:
        if contract is None:
            return None
        _write_account(db, voyage, None, "pending", "no connection or meter evidence",
                       None, None, "shared contract")
        return {"voyage": voyage, "kwh": None, "state": "pending",
                "reason": "no connection or meter evidence",
                "kwh_min": None, "kwh_max": None, "basis": "shared contract"}
    firm = sum(Fraction(str(r["kwh"])) for r in rows)
    upper = sum(Fraction(str(r["kwh_max"])) for r in rows)
    contested = [r for r in rows if r["basis"].startswith("pending")]
    kmin, kmax = float(firm), float(upper)
    if contested:
        reason = contested[0]["basis"].removeprefix("pending: ")
        if any(flag in reason for flag in ("backwards", "conflicting", "outage")):
            state = "disputed"
        else:
            state = "pending"
        kwh, basis = None, "shared transformer range pending evidence"
    else:
        kwh = kmin
        state, reason = "apportioned", "shared transformer apportionment"
        basis = "shared transformer apportionment by signed weights"
    _write_account(db, voyage, kwh, state, reason, kmin, kmax, basis)
    return {"voyage": voyage, "kwh": kwh, "state": state, "reason": reason,
            "kwh_min": kmin, "kwh_max": kmax, "basis": basis}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_account(db, voyage, kwh, state, reason, kwh_min, kwh_max, basis):
    old = db.execute("SELECT * FROM power_account WHERE voyage=?", (voyage,)).fetchone()
    new = (kwh, state, reason, kwh_min, kwh_max, basis)
    if old and (old["kwh"], old["state"], old["reason"],
                old["kwh_min"], old["kwh_max"], old["basis"]) == new:
        return
    db.execute(
        "INSERT INTO power_account(voyage,kwh,state,reason,kwh_min,kwh_max,basis) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(voyage) DO UPDATE SET "
        "kwh=excluded.kwh,state=excluded.state,reason=excluded.reason,"
        "kwh_min=excluded.kwh_min,kwh_max=excluded.kwh_max,basis=excluded.basis",
        (voyage, kwh, state, reason, kwh_min, kwh_max, basis),
    )
    last = db.execute(
        "SELECT event_id FROM (SELECT pa.evidence_id AS event_id FROM power_attribution pa "
        "WHERE pa.voyage=? ORDER BY start DESC,id) LIMIT 1", (voyage,)).fetchone()
    log(db, "power", voyage, state, reason, last["event_id"] if last else None)


def _latest_contract(db, voyage):
    return db.execute(
        "SELECT * FROM power_contract WHERE voyage=? ORDER BY signed_at DESC,event_id DESC "
        "LIMIT 1", (voyage,)).fetchone()


def _meter_for_voyage(db, voyage):
    occ = db.execute(
        "SELECT berth FROM occupancy WHERE voyage=? "
        "ORDER BY start DESC, COALESCE(end, '') DESC, event_id LIMIT 1",
        (voyage,)).fetchone()
    if occ is None:
        return None
    for row in db.execute("SELECT meter_id,berths FROM power_transformer").fetchall():
        if occ["berth"] in json.loads(row["berths"]):
            return row["meter_id"]
    return None


def _voyages_for_meter(db, meter_id):
    row = db.execute("SELECT berths FROM power_transformer WHERE meter_id=?",
                     (meter_id,)).fetchone()
    if row is None:
        return set()
    berths = json.loads(row["berths"])
    if not berths:
        return set()
    rows = db.execute(
        "SELECT DISTINCT voyage FROM occupancy WHERE berth IN (%s)"
        % ",".join("?" for _ in berths), tuple(berths)).fetchall()
    return {r["voyage"] for r in rows}


def _vessel(voyage):
    return f"vessel:{voyage}"


def _overlap(start, end, other_start, other_end):
    return start < other_end and other_start < end


def _seconds(start, end):
    return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
