import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portbook import handoff
from portbook.store import connect


def fixture():
    return json.loads((ROOT / "fixtures" / "handoff-ok.json").read_text(encoding="utf8"))


class HandoffCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_continuous_container_custody(self):
        result = handoff.import_data(self.db, fixture())
        self.assertEqual(result[0]["custodian"], "truck-8")
        self.assertEqual(result[0]["state"], "confirmed")
        self.assertEqual(handoff.import_data(self.db, fixture()), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM handoff_event").fetchone()[0], 2)

    def test_seal_break_disputes_custody_without_touching_berth(self):
        handoff.import_data(self.db, fixture())
        third = dict(fixture()["handoffs"][1], event_id="h3", from_party="truck-8", to_party="yard", at="2026-11-17T11:00:00", seal="S2")
        result = handoff.import_data(self.db, {"handoffs": [third]})
        self.assertEqual(result[0]["state"], "disputed")
        self.assertIsNone(result[0]["custodian"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0], 0)

    def test_event_id_conflict_rolls_back(self):
        handoff.import_data(self.db, fixture())
        changed = dict(fixture()["handoffs"][0], to_party="other")
        with self.assertRaises(ValueError):
            handoff.import_data(self.db, {"handoffs": [changed]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM handoff_event").fetchone()[0], 2)

