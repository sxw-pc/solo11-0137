import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portbook import ais
from portbook.store import connect


def fixture():
    return json.loads((ROOT / "fixtures" / "ais-ok.json").read_text(encoding="utf8"))


class AisCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_track_is_independent_of_berth_dispatch(self):
        result = ais.import_data(self.db, fixture())[0]
        self.assertEqual((result["pings"], result["state"]), (2, "reported"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0], 0)
        self.assertEqual(ais.import_data(self.db, fixture()), [])

    def test_multiple_mmsi_disputes_track(self):
        ais.import_data(self.db, fixture())
        third = dict(fixture()["pings"][1], event_id="ais-3", mmsi="412000002", at="2026-11-17T09:10:00")
        self.assertEqual(ais.import_data(self.db, {"pings": [third]})[0]["state"], "disputed")

    def test_event_id_conflict_rolls_back(self):
        ais.import_data(self.db, fixture())
        changed = dict(fixture()["pings"][0], lat=35.0)
        with self.assertRaises(ValueError):
            ais.import_data(self.db, {"pings": [changed]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ais_ping").fetchone()[0], 2)

