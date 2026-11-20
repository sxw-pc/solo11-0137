import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portbook import power, arrival, report
from portbook.store import connect

T09, T0930, T10, T1030, T11 = (
    "2026-11-17T09:00:00", "2026-11-17T09:30:00", "2026-11-17T10:00:00",
    "2026-11-17T10:30:00", "2026-11-17T11:00:00",
)


def contracts(kind, voyages=("V1", "V2"), weights=None, caps=None):
    weights = weights or {"V1": 2, "V2": 1}
    caps = caps or {"V1": 100.0, "V2": 100.0}
    berth = {"V1": "B1", "V2": "B2", "V3": "B3"}
    out = []
    for i, v in enumerate(voyages):
        item = {"event_id": f"contract-{v}", "voyage": v, "kind": kind,
                "berth": berth[v], "signed_at": "2026-11-17T08:00:00"}
        if kind == "shared":
            item["weight"] = weights[v]
            item["load_cap"] = caps[v]
        out.append(item)
    return out


def transformer(berths=("B1", "B2")):
    return [{"event_id": "tr-1", "meter_id": "TM1", "berths": list(berths)}]


def readings(points):
    return [{"event_id": f"rd-{i}", "meter_id": "TM1", "at": at, "kwh_total": kwh}
            for i, (at, kwh) in enumerate(points)]


def occupancy(voyage, berth, start, end, suffix=""):
    return {"event_id": f"occ-{voyage}{suffix}", "voyage": voyage, "berth": berth,
            "start": start, "end": end}


def both_present_09_10():
    return {"occupancies": [occupancy("V1", "B1", T09, T10),
                            occupancy("V2", "B2", T09, T10)]}


class SharedPowerCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed(self, kind, **kw):
        power.import_data(self.db, {
            "transformers": transformer(kw.pop("berths", ("B1", "B2"))),
            "contracts": contracts(kind, **kw),
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        })

    def _segments_conserve(self):
        """Every trusted segment's attributions sum to its meter increment."""
        for seg in self.db.execute("SELECT * FROM power_segment WHERE trusted=1"):
            total = self.db.execute(
                "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution WHERE segment_id=?",
                (seg["id"],)).fetchone()["s"]
            self.assertAlmostEqual(total, seg["increment"], places=9,
                                   msg=f"segment {seg['id']} not conserved")

    def test_same_input_different_bills_by_contract(self):
        # Shared contract: 60 kWh, weights 2:1 -> 40 / 20.
        self._seed("shared")
        arrival.import_data(self.db, both_present_09_10())
        v1, v2 = power.decide(self.db, "V1"), power.decide(self.db, "V2")
        self.assertEqual((v1["state"], v1["kwh"]), ("apportioned", 40.0))
        self.assertEqual((v2["state"], v2["kwh"]), ("apportioned", 20.0))

        # Dedicated contracts on the identical evidence: neither may be billed
        # off the shared meter; both wait on their own ship meters.
        other = connect(":memory:")
        self.addCleanup(other.close)
        power.import_data(other, {
            "transformers": transformer(),
            "contracts": contracts("dedicated"),
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        })
        arrival.import_data(other, both_present_09_10())
        d1, d2 = power.decide(other, "V1"), power.decide(other, "V2")
        self.assertEqual(d1["state"], "pending")
        self.assertEqual(d2["state"], "pending")
        self.assertIn("meter", d1["reason"])
        # The transformer energy is not silently split under dedicated terms.
        self.assertEqual(
            other.execute("SELECT COUNT(*) FROM power_attribution WHERE basis LIKE "
                          "'shared transformer apportionment%'").fetchone()[0], 0)

    def test_misaligned_connection_and_dock_base_load_conserve(self):
        self._seed("shared")
        # V1 connects only 09:00-09:30; V2 the whole hour.
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T0930),
            occupancy("V2", "B2", T09, T10),
        ]})
        v1, v2 = power.decide(self.db, "V1"), power.decide(self.db, "V2")
        # First half: 30 kWh split 2:1 -> 20 / 10. Second half: V2 alone 30.
        self.assertAlmostEqual(v1["kwh"], 20.0, places=9)
        self.assertAlmostEqual(v2["kwh"], 40.0, places=9)
        self._segments_conserve()
        self.assertAlmostEqual(
            self.db.execute("SELECT SUM(kwh) FROM power_attribution").fetchone()[0],
            60.0, places=9)

        # With nobody connected during the second half, the whole 30 kWh stays
        # dock base load (fresh ledger so the correction does not rewrite V2).
        quiet = connect(":memory:")
        self.addCleanup(quiet.close)
        power.import_data(quiet, {
            "transformers": transformer(),
            "contracts": contracts("shared"),
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        })
        arrival.import_data(quiet, {"occupancies": [
            occupancy("V1", "B1", T09, T0930),
            occupancy("V2", "B2", T09, T0930),
        ]})
        dock = quiet.execute(
            "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution WHERE recipient='dock' "
            "AND start=? AND end=?", (T0930, T10)).fetchone()["s"]
        self.assertAlmostEqual(dock, 30.0, places=9)
        for seg in quiet.execute("SELECT * FROM power_segment WHERE trusted=1"):
            total = quiet.execute(
                "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution WHERE segment_id=?",
                (seg["id"],)).fetchone()["s"]
            self.assertAlmostEqual(total, seg["increment"], places=9)

    def test_load_cap_breach_gives_range_not_a_picked_allocation(self):
        # V1 wants 40 kWh but its signed cap is 20 kWh for the hour.
        self._seed("shared", caps={"V1": 20.0, "V2": 100.0})
        arrival.import_data(self.db, both_present_09_10())
        v1 = power.decide(self.db, "V1")
        self.assertEqual(v1["state"], "pending")
        self.assertIsNone(v1["kwh"])
        self.assertEqual(v1["kwh_min"], 20.0)          # firm cap-limited amount
        self.assertEqual(v1["kwh_max"], 20.0)          # V1 itself cannot take more
        # The bill-changing surplus is held open, never assigned by ordering.
        overflow = self.db.execute(
            "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution "
            "WHERE recipient='unallocated'").fetchone()["s"]
        self.assertAlmostEqual(overflow, 20.0, places=9)
        self._segments_conserve()

    def test_meter_rollback_only_taints_affected_interval(self):
        self._seed("shared")
        power.import_data(self.db, {
            "transformer_readings": readings([(T09, 200), (T10, 260), (T11, 250)]),
        })
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T11), occupancy("V2", "B2", T09, T11),
        ]})
        v1 = power.decide(self.db, "V1")
        self.assertEqual(v1["state"], "disputed")
        # The verified 09:00-10:00 consumption survives: firm 40 kWh remains.
        self.assertEqual(v1["kwh_min"], 40.0)
        self.assertIsNone(v1["kwh"])
        trusted = self.db.execute(
            "SELECT trusted FROM power_segment WHERE start=? AND end=?", (T09, T10)
        ).fetchone()["trusted"]
        tainted = self.db.execute(
            "SELECT trusted FROM power_segment WHERE start=? AND end=?", (T10, T11)
        ).fetchone()["trusted"]
        self.assertEqual((trusted, tainted), (1, 0))
        # Raw sealed readings are untouched.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM transformer_reading").fetchone()[0], 3)

    def test_conflicting_same_timestamp_readings_poison_one_interval(self):
        self._seed("shared")
        power.import_data(self.db, {"transformer_readings": [
            {"event_id": "rd-conflict", "meter_id": "TM1", "at": T10,
             "kwh_total": 270},
        ]})
        arrival.import_data(self.db, both_present_09_10())
        v1 = power.decide(self.db, "V1")
        self.assertEqual(v1["state"], "disputed")
        self.assertIn("conflicting", v1["reason"])
        # Add a later clean pair; the later interval remains usable.
        power.import_data(self.db, {"transformer_readings": [
            {"event_id": "rd-11", "meter_id": "TM1", "at": T11, "kwh_total": 330},
        ]})
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T11, "-ext"),
            occupancy("V2", "B2", T09, T11, "-ext"),
        ]})
        later = self.db.execute(
            "SELECT trusted FROM power_segment WHERE start=? AND end=?", (T10, T11)
        ).fetchone()["trusted"]
        self.assertEqual(later, 1)

    def test_outage_requires_resumption_evidence_then_recovers(self):
        self._seed("shared")
        power.import_data(self.db, {
            "transformer_readings": readings([(T09, 200), (T10, 260), (T11, 300)]),
            "outages": [{"event_id": "out-1", "meter_id": "TM1",
                         "start": T10, "end": T1030}],
        })
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T11), occupancy("V2", "B2", T09, T11),
        ]})
        v1 = power.decide(self.db, "V1")
        self.assertEqual(v1["state"], "disputed")
        self.assertEqual(v1["kwh_min"], 40.0)       # earlier verified hour kept
        # Supply the re-metering reading and link it to the outage.
        power.import_data(self.db, {
            "transformer_readings": [
                {"event_id": "rd-resume", "meter_id": "TM1", "at": T1030,
                 "kwh_total": 280},
            ],
            "outages": [{"event_id": "out-2", "meter_id": "TM1",
                         "start": T10, "end": T1030, "resumed_by": "rd-resume"}],
        })
        v1 = power.decide(self.db, "V1")
        self.assertEqual(v1["state"], "apportioned")
        self.assertGreater(v1["kwh"], 40.0)
        self._segments_conserve()

    def test_occupancy_correction_moves_energy_from_dock_to_voyage(self):
        self._seed("shared")
        # First evidence: both ships connected only until 09:30; the
        # 09:30-10:00 increment therefore rests with the dock as base load.
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T0930),
            occupancy("V2", "B2", T09, T0930),
        ]})
        before = power.decide(self.db, "V1")
        self.assertEqual(before["kwh"], 20.0)
        dock_half = self.db.execute(
            "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution WHERE recipient='dock' "
            "AND start=? AND end=?", (T0930, T10)).fetchone()["s"]
        self.assertAlmostEqual(dock_half, 30.0, places=9)
        self._segments_conserve()

        # Supplemental on-site evidence extends V1's actual connection to 10:00.
        arrival.import_data(self.db, {"occupancies": [
            {"event_id": "occ-V1-corrected", "voyage": "V1", "berth": "B1",
             "start": T09, "end": T10},
        ]})
        after = power.decide(self.db, "V1")
        self.assertEqual(after["state"], "apportioned")
        # 20 from the shared first half + the reclaimed 30 = changed bill.
        self.assertEqual(after["kwh"], 50.0)
        self._segments_conserve()
        # The energy moved off the dock's base-load line onto V1.
        dock_after = self.db.execute(
            "SELECT COALESCE(SUM(kwh),0) AS s FROM power_attribution WHERE recipient='dock'"
        ).fetchone()["s"]
        self.assertAlmostEqual(dock_after, 0.0, places=9)
        # Berth plans can never substitute for actual connection evidence:
        # allocations table is untouched and raw readings stay sealed.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0], 0)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM transformer_reading").fetchone()[0], 2)
        # The correction is itself an auditable decision transition.
        self.assertGreaterEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM decision_log WHERE subsystem='power' "
                "AND object_id='V1'").fetchone()[0], 2)

    def test_third_ship_without_issued_basis_only_gets_ranges(self):
        power.import_data(self.db, {
            "transformers": transformer(("B1", "B2", "B3")),
            "contracts": contracts("shared") + [
                {"event_id": "contract-V3", "voyage": "V3", "kind": "shared",
                 "berth": "B3", "signed_at": "2026-11-17T08:00:00"}],
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        })
        arrival.import_data(self.db, {"occupancies": [
            occupancy("V1", "B1", T09, T10), occupancy("V2", "B2", T09, T10),
            occupancy("V3", "B3", T09, T10),
        ]})
        for v in ("V1", "V2", "V3"):
            d = power.decide(self.db, v)
            self.assertEqual(d["state"], "pending")
            self.assertIsNone(d["kwh"])
            self.assertGreater(d["kwh_max"], 0)
        # Ranges still reconcile to the trusted increment.
        self.assertAlmostEqual(
            self.db.execute("SELECT SUM(kwh) FROM power_attribution").fetchone()[0],
            60.0, places=9)

    def test_duplicate_intake_is_idempotent(self):
        self._seed("shared")
        payload = both_present_09_10()
        arrival.import_data(self.db, payload)
        first = report.build(self.db)
        logs_before = self.db.execute(
            "SELECT COUNT(*) FROM decision_log").fetchone()[0]
        # Re-importing identical evidence changes nothing.
        arrival.import_data(self.db, payload)
        logs_after = self.db.execute(
            "SELECT COUNT(*) FROM decision_log").fetchone()[0]
        self.assertEqual(logs_before, logs_after)
        self.assertEqual(report.build(self.db), first)
        # Same for the power intake itself.
        self.assertEqual(power.import_data(self.db, {
            "transformers": transformer(),
            "contracts": contracts("shared"),
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        }), [])

    def test_dedicated_own_meter_still_settles_and_deducts_from_shared_meter(self):
        # V1 holds a dedicated contract and ships its own meter readings.
        power.import_data(self.db, {
            "transformers": transformer(),
            "contracts": [
                {"event_id": "contract-V1", "voyage": "V1", "kind": "dedicated",
                 "berth": "B1", "signed_at": "2026-11-17T08:00:00"},
                {"event_id": "contract-V2", "voyage": "V2", "kind": "shared",
                 "berth": "B2", "weight": 1, "load_cap": 100.0,
                 "signed_at": "2026-11-17T08:00:00"},
            ],
            "readings": [
                {"event_id": "own-1", "meter_id": "OWN1", "voyage": "V1",
                 "at": T09, "kwh_total": 100},
                {"event_id": "own-2", "meter_id": "OWN1", "voyage": "V1",
                 "at": T10, "kwh_total": 130},
            ],
            "transformer_readings": readings([(T09, 200), (T10, 260)]),
        })
        arrival.import_data(self.db, both_present_09_10())
        v1, v2 = power.decide(self.db, "V1"), power.decide(self.db, "V2")
        self.assertEqual((v1["state"], v1["kwh"]), ("measured", 30))
        self.assertEqual(v2["state"], "apportioned")
        # V1's measured 30 leaves 30 of the transformer total for V2/dock.
        self.assertLessEqual(v2["kwh"], 30.0 + 1e-9)
        self._segments_conserve()


if __name__ == "__main__":
    unittest.main()
