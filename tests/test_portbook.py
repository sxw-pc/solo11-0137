import copy
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
import portbook
from portbook import notice, waterway, resources, arrival, report
from portbook.store import connect

ROOT=Path(__file__).resolve().parents[1]
def fixture(name):
    return json.loads((ROOT/'fixtures'/name).read_text(encoding='utf8'))

class PortBookTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(str(Path(self.temp.name)/'port.db'))
    def tearDown(self):
        self.db.close();self.temp.cleanup()
    def ready(self):
        notice.import_data(self.db,fixture('notices-ok.json'))
        waterway.import_data(self.db,fixture('waterway-ok.json'))
        resources.import_data(self.db,fixture('resources-ok.json'))
    def test_module_origin(self):
        self.assertTrue(str(Path(portbook.__file__).resolve()).startswith(str(ROOT)))
    def test_notice_source_and_parallel_conflict(self):
        out=notice.import_data(self.db,fixture('notices-bad.json'))
        self.assertEqual(out[0]['state'],'untrusted')
        self.assertEqual(self.db.execute("SELECT state FROM notice_decisions WHERE voyage='V4'").fetchone()[0],'conflict')
    def test_conflict_independent_of_import_order(self):
        data=fixture('notices-bad.json')
        first=[data['notices'][1],data['notices'][2]]
        data['notices']=list(reversed(first))
        notice.import_data(self.db,data)
        self.assertEqual(self.db.execute("SELECT state FROM notice_decisions WHERE voyage='V4'").fetchone()[0],'conflict')
        withdrawal={'event_id':'withdraw-conflict-b','target_event_id':'conflict-b','signer':'harbor-office','issued_at':'2026-11-16T12:05:00','reason':'clerical correction'}
        notice.import_data(self.db,{'withdrawals':[withdrawal]})
        self.assertEqual(self.db.execute("SELECT state FROM notice_decisions WHERE voyage='V4'").fetchone()[0],'accepted')
    def test_notice_replay_and_changed_event(self):
        data=fixture('notices-ok.json')
        notice.import_data(self.db,data)
        self.assertEqual(notice.import_data(self.db,data)[0]['state'],'idempotent')
        changed=copy.deepcopy(data);changed['notices'][0]['eta']='2026-11-17T11:00:00'
        with self.assertRaises(ValueError): notice.import_data(self.db,changed)
    def test_failed_batch_rolls_back_new_notice(self):
        base=fixture('notices-ok.json')
        notice.import_data(self.db,base)
        mixed={'notices':[dict(base['notices'][0],event_id='new-v3',voyage='V3'),
                          dict(base['notices'][0],eta='2026-11-17T11:00:00')]}
        with self.assertRaises(ValueError): notice.import_data(self.db,mixed)
        self.assertIsNone(self.db.execute("SELECT event_id FROM evidence WHERE event_id='new-v3'").fetchone())
    def test_waterway_minimum_and_closure(self):
        normal=waterway.import_data(self.db,fixture('waterway-ok.json'))
        self.assertEqual([x['state'] for x in normal],['safe','safe'])
        bad=fixture('waterway-bad.json');bad['templates'][0]['ship_class']='barge'
        self.assertEqual(waterway.import_data(self.db,bad)[0]['state'],'unsafe')
        bad['restrictions']=[{'event_id':'c1-revoke','order_id':'C1','version':2,'start':'2026-11-17T09:00:00','end':'2026-11-17T10:00:00','signer':'harbor','revoked':True}]
        bad['templates'][0]['ship_class']='barge2'
        self.assertEqual(waterway.import_data(self.db,bad)[0]['state'],'unsafe')
    def test_closure_splits_safe_window(self):
        data=fixture('waterway-ok.json')
        data['restrictions']=[{'event_id':'cut','order_id':'C1','start':'2026-11-17T09:00:00','end':'2026-11-17T10:00:00','signer':'harbor'}]
        self.assertEqual(waterway.import_data(self.db,data)[0]['state'],'partially_safe')
        parts=self.db.execute("SELECT start,end FROM windows WHERE ship_class='cargo' AND state='safe' AND base_start='2026-11-17T08:00:00'").fetchall()
        self.assertEqual([(x['start'],x['end']) for x in parts],[('2026-11-17T08:00:00','2026-11-17T09:00:00'),('2026-11-17T10:00:00','2026-11-17T12:00:00')])
    def test_waterway_missing_sample(self):
        data=fixture('waterway-ok.json');data['gauges']=data['gauges'][:2]
        self.assertEqual(waterway.import_data(self.db,data)[0]['state'],'pending')
    def test_tug_conflict_even_with_empty_berth(self):
        out=resources.import_data(self.db,fixture('resources-bad.json'))
        self.assertEqual([x['state'] for x in out],['confirmed','waiting'])
        self.assertEqual(out[1]['reason'],'tug occupied')
    def test_failed_replan_preserves_confirmed_allocation(self):
        self.ready()
        revised=fixture('resources-ok.json')['applications'][0].copy()
        revised.update(id='bad-replan',start='2026-11-17T11:00:00',end='2026-11-17T12:00:00',draft=20)
        out=resources.import_data(self.db,{'applications':[revised]})
        self.assertEqual(out[0]['state'],'waiting')
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a1'").fetchone()[0],'confirmed')
    def test_formal_requires_notice_and_window(self):
        data=fixture('resources-ok.json')
        self.assertEqual(resources.import_data(self.db,data)[0]['state'],'waiting')
        notice.import_data(self.db,fixture('notices-ok.json'))
        waterway.import_data(self.db,fixture('waterway-ok.json'))
        resources.promote(self.db)
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a1'").fetchone()[0],'confirmed')
    def test_waterway_revision_invalidates_plan(self):
        self.ready()
        bad={'gauges':[{'event_id':'g2-new','station':'A','at':'2026-11-17T10:00:00','datum':'local','depth':8,'version':2}],
             'templates':fixture('waterway-ok.json')['templates']}
        self.assertEqual(waterway.import_data(self.db,bad)[0]['state'],'unsafe')
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a1'").fetchone()[0],'pending')
    def test_signed_cancellation_keeps_physical_history(self):
        self.ready()
        arrival.import_data(self.db,fixture('arrival-ok.json'))
        data=fixture('notices-ok.json')
        cancel=dict(data['notices'][0],event_id='cancel-v1',revision=2,cancelled=True)
        notice.import_data(self.db,{'notices':[cancel]})
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a1'").fetchone()[0],'disputed')
        self.assertEqual(self.db.execute("SELECT count(*) FROM occupancy WHERE voyage='V1'").fetchone()[0],1)
    def test_announcement_revision_releases_future_slot(self):
        self.ready()
        data=fixture('notices-ok.json')
        revised=dict(data['notices'][0],event_id='rev-v1',revision=2,eta='2026-11-17T11:00:00')
        notice.import_data(self.db,{'notices':[revised]})
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a1'").fetchone()[0],'released')
    def test_late_witness_releases_and_promotes(self):
        self.ready()
        extra=fixture('resources-ok.json')['applications'][0].copy()
        extra.update(id='a3',voyage='V3',formal=False,priority=1)
        resources.import_data(self.db,{'applications':[extra]})
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a3'").fetchone()[0],'waiting')
        out=arrival.import_data(self.db,fixture('arrival-bad.json'))
        self.assertEqual(out[0]['state'],'forfeited')
        self.assertEqual(self.db.execute("SELECT state FROM allocations WHERE id='a3'").fetchone()[0],'confirmed')
    def test_occupancy_without_plan_blocks_resource(self):
        arrival.import_data(self.db,{'occupancies':[{'event_id':'o-x','voyage':'X','berth':'B2','start':'2026-11-17T09:00:00'}]})
        self.assertEqual(self.db.execute("SELECT state FROM fulfilments WHERE voyage='X'").fetchone()[0],'disputed')
        result=resources.import_data(self.db,fixture('resources-bad.json'))
        self.assertEqual(result[0]['reason'],'berth occupied')
    def test_report_evidence_recomputable(self):
        self.ready()
        data=report.build(self.db)
        self.assertTrue(data['evidence'])
        self.assertTrue(data['decision_log'])
        self.assertEqual(data['windows'][0]['state'],'safe')
    def test_cli_replay_audit_from_raw_files(self):
        path=str(Path(self.temp.name)/'cli.db')
        run=subprocess.run([sys.executable,'-m','portbook','demo','--db',path],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(run.stdout)['audit']['replay_ok'])
        audit=subprocess.run([sys.executable,'-m','portbook','audit','--db',path],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(audit.stdout)['journal_count'],4)

if __name__=='__main__': unittest.main()
