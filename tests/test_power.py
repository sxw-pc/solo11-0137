import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portbook import power
from portbook.store import connect


def fixture():
    return json.loads((ROOT / "fixtures" / "power-ok.json").read_text(encoding="utf8"))


class PowerCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_meter_account_independent_of_berth_dispatch(self):
        result = power.import_data(self.db, fixture())[0]
        self.assertEqual((result["kwh"], result["state"]), (50, "measured"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0], 0)
        self.assertEqual(power.import_data(self.db, fixture()), [])

    def test_missing_close_and_backward_total(self):
        first, last = fixture()["readings"]
        self.assertEqual(power.import_data(self.db, {"readings": [first]})[0]["state"], "pending")
        backward = dict(last, kwh_total=900)
        self.assertEqual(power.import_data(self.db, {"readings": [backward]})[0]["state"], "disputed")

    def test_conflicting_event_id_rolls_back(self):
        power.import_data(self.db, fixture())
        changed = dict(fixture()["readings"][0], kwh_total=7)
        with self.assertRaises(ValueError):
            power.import_data(self.db, {"readings": [changed]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM power_reading").fetchone()[0], 2)

