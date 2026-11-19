import copy
import json
import tempfile
import unittest
from pathlib import Path

from portbook import notice, waterway, resources, arrival, report
from portbook.store import connect

ROOT = Path(__file__).resolve().parents[1]
def fixture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf8'))

CRED = {'signer': 'harbor-office', 'source': 'harbor',
        'valid_from': '2026-01-01T00:00:00', 'valid_to': '2027-01-01T00:00:00'}

def n(voyage, eta, draft=9, event_id=None, revision=1):
    return {'event_id': event_id or 'n-' + voyage, 'signer': 'harbor-office',
            'source': 'harbor', 'issued_at': '2026-11-16T12:00:00',
            'voyage': voyage, 'revision': revision, 'eta': eta, 'draft': draft,
            'cancelled': False}

def app(id_, voyage, start, end, priority, berth='B1', tug='T1', draft=9):
    return {'id': id_, 'voyage': voyage, 'berth': berth, 'tug': tug,
            'start': start, 'end': end, 'priority': priority, 'draft': draft,
            'required_power': 2, 'formal': True}

# The canonical world: berth B1 and tug T1 are both free 09:00-11:00.
# A (priority 10) wants the whole window; B and C (priority 6) want one hour
# each and can together cover the same window.
def abc_world(db, release=True):
    notice.import_data(db, {'credentials': [CRED], 'notices': [
        n('VA', '2026-11-17T09:00:00'),
        n('VB', '2026-11-17T09:00:00', event_id='n-VB'),
        n('VC', '2026-11-17T10:00:00', event_id='n-VC'),
    ]})
    waterway.import_data(db, fixture('waterway-ok.json'))  # safe cargo windows 08-12, 12-16
    resources.import_data(db, {
        'berths': [{'id': 'B1', 'depth': 12, 'ship_class': 'cargo'},
                   {'id': 'B2', 'depth': 12, 'ship_class': 'cargo'}],
        'tugs': [{'id': 'T1', 'power': 3, 'qualified': True,
                  'start': '2026-11-17T08:00:00', 'end': '2026-11-17T12:00:00'}],
        'applications': [
            # Q is already confirmed and retained: it uses a different hour.
            dict(app('appQ', 'VQ', '2026-11-17T08:00:00', '2026-11-17T09:00:00', 5),
                 formal=False),
            # P holds the 09:00-11:00 slot until its release.
            dict(app('appP', 'VP', '2026-11-17T09:00:00', '2026-11-17T11:00:00', 1),
                 formal=False),
        ],
    })
    resources.import_data(db, {'applications': [
        app('appA', 'VA', '2026-11-17T09:00:00', '2026-11-17T11:00:00', 10),
        app('appB', 'VB', '2026-11-17T09:00:00', '2026-11-17T10:00:00', 6),
        app('appC', 'VC', '2026-11-17T10:00:00', '2026-11-17T11:00:00', 6),
    ]})
    # Release P without triggering one-by-one backfill: the dispatcher must be
    # able to inspect the whole queue as one feasible group first.
    if release:
        resources.invalidate_voyage(db, 'VP', 'berth and tug released', promote_after=False)
    return ['appA', 'appB', 'appC']


class DelegationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.temp.name) / 'delegation.db'))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def states(self, *ids):
        return {i: self.db.execute('SELECT state FROM allocations WHERE id=?', (i,)).fetchone()[0]
                for i in ids}

    def delegation_row(self, did):
        return dict(self.db.execute('SELECT * FROM delegations WHERE id=?', (did,)).fetchone())

    # ------------------------------------------------------------------
    # The two contracts are not a sorting switch over one rule.
    # ------------------------------------------------------------------
    def test_emergency_delegation_honours_single_highest_grade_ship(self):
        ids = abc_world(self.db)
        req = {'id': 'del-eme-1', 'kind': 'emergency', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        plan = resources.group_dispatch(self.db, req)
        self.assertEqual(plan['selected_ids'], ['appA'])
        self.assertEqual(plan['candidate_ids'], ids)
        self.assertEqual(self.states('appA', 'appB', 'appC'),
                         {'appA': 'confirmed', 'appB': 'waiting', 'appC': 'waiting'})
        row = self.delegation_row('del-eme-1')
        self.assertEqual(row['kind'], 'emergency')
        self.assertEqual(json.loads(row['candidate_ids']), ids)
        self.assertEqual(json.loads(row['selected_ids']), ['appA'])
        item = {r['allocation_id']: dict(r)
                for r in self.db.execute("SELECT * FROM delegation_items WHERE delegation_id='del-eme-1'")}
        self.assertEqual(item['appA']['state'], 'selected')
        self.assertIn('highest priority 10', item['appA']['reason'])
        self.assertEqual(item['appB']['state'], 'rejected')
        self.assertIsNotNone(item['appA']['notice_version'])
        self.assertIsNotNone(item['appA']['window_version'])
        # The pre-existing confirmed plan Q is preserved, never displaced.
        self.assertEqual(self.states('appQ')['appQ'], 'confirmed')

    def test_throughput_delegation_honours_most_voyages_within_safety(self):
        abc_world(self.db)
        req = {'id': 'del-thr-1', 'kind': 'throughput', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        plan = resources.group_dispatch(self.db, req)
        self.assertEqual(plan['selected_ids'], ['appB', 'appC'])
        self.assertEqual(self.states('appA', 'appB', 'appC'),
                         {'appA': 'waiting', 'appB': 'confirmed', 'appC': 'confirmed'})
        row = self.delegation_row('del-thr-1')
        self.assertEqual(row['kind'], 'throughput')
        item = {r['allocation_id']: dict(r)
                for r in self.db.execute("SELECT * FROM delegation_items WHERE delegation_id='del-thr-1'")}
        self.assertEqual(item['appA']['state'], 'rejected')
        self.assertIn('maximum feasible pack of 2', item['appB']['reason'])
        self.assertIn('berth occupied', item['appA']['reason'])

    def test_kind_is_part_of_the_contract_not_a_sort_flag(self):
        # Identical input, different contract id/kind -> different verdicts,
        # and both stay recorded independently.
        abc_world(self.db)
        scope = {'berth': 'B1', 'start': '2026-11-17T09:00:00',
                 'end': '2026-11-17T11:00:00'}
        eme = resources.group_dispatch(self.db, dict(scope, id='d-eme', kind='emergency'))
        # Reset the queue by releasing the emergency choice for the second contract.
        resources.invalidate_voyage(self.db, 'VA', 'emergency window closed', promote_after=False)
        thr = resources.group_dispatch(self.db, dict(scope, id='d-thr', kind='throughput'))
        self.assertEqual(eme['selected_ids'], ['appA'])
        self.assertEqual(thr['selected_ids'], ['appB', 'appC'])
        self.assertEqual(self.delegation_row('d-eme')['kind'], 'emergency')
        self.assertEqual(self.delegation_row('d-thr')['kind'], 'throughput')
        with self.assertRaises(ValueError):
            resources.group_dispatch(self.db, dict(scope, id='d-eme', kind='throughput'))

    # ------------------------------------------------------------------
    # Berths can be shared while one tug is contended.
    # ------------------------------------------------------------------
    def test_cross_berth_tug_contention(self):
        notice.import_data(self.db, {'credentials': [CRED], 'notices': [
            n('VX', '2026-11-17T09:00:00', event_id='n-X'),
            n('VY', '2026-11-17T09:00:00', event_id='n-Y'),
            n('VZ', '2026-11-17T10:00:00', event_id='n-Z'),
        ]})
        waterway.import_data(self.db, fixture('waterway-ok.json'))
        resources.import_data(self.db, {
            'berths': [{'id': 'B1', 'depth': 12, 'ship_class': 'cargo'},
                       {'id': 'B2', 'depth': 12, 'ship_class': 'cargo'}],
            'tugs': [{'id': 'T1', 'power': 3, 'qualified': True,
                      'start': '2026-11-17T08:00:00', 'end': '2026-11-17T12:00:00'}],
            'applications': [
                dict(app('appP', 'VP', '2026-11-17T09:00:00', '2026-11-17T11:00:00', 1,
                         berth='B1', tug='T1'), formal=False),
            ],
        })
        resources.import_data(self.db, {'applications': [
            app('appX', 'VX', '2026-11-17T09:00:00', '2026-11-17T10:00:00', 8, berth='B1'),
            app('appY', 'VY', '2026-11-17T09:00:00', '2026-11-17T10:00:00', 7, berth='B2'),
            app('appZ', 'VZ', '2026-11-17T10:00:00', '2026-11-17T11:00:00', 7, berth='B2'),
        ]})
        resources.invalidate_voyage(self.db, 'VP', 'tug released', promote_after=False)
        plan = resources.group_dispatch(self.db, {
            'id': 'd-tug', 'kind': 'throughput', 'tug': 'T1',
            'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'})
        self.assertEqual(plan['selected_ids'], ['appX', 'appZ'])
        items = {i['id']: i for i in plan['items']}
        self.assertEqual(items['appY']['state'], 'rejected')
        self.assertIn('tug occupied', items['appY']['reason'])

    # ------------------------------------------------------------------
    # Recomputation after a notice or window is revoked: stored verdict is
    # immutable, the recomputed verdict reflects the current ledger.
    # ------------------------------------------------------------------
    def test_recompute_after_notice_withdrawal(self):
        abc_world(self.db)
        req = {'id': 'd-thr', 'kind': 'throughput', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        resources.group_dispatch(self.db, req)
        notice.import_data(self.db, {'withdrawals': [{
            'event_id': 'w-n-VB', 'target_event_id': 'n-VB', 'signer': 'harbor-office',
            'issued_at': '2026-11-16T13:00:00', 'reason': 'voyage withdrawn'}]})
        # Stored verdict is untouched.
        self.assertEqual(json.loads(self.delegation_row('d-thr')['selected_ids']),
                         ['appB', 'appC'])
        recalc = resources.recompute_group(self.db, 'd-thr')
        self.assertTrue(recalc['changed'])
        self.assertEqual(recalc['stored_selected_ids'], ['appB', 'appC'])
        self.assertEqual(recalc['selected_ids'], ['appA'])
        items = {i['id']: i for i in recalc['items']}
        self.assertEqual(items['appB']['state'], 'rejected')
        self.assertEqual(items['appB']['reason'], 'notice not accepted')
        # Re-importing the same delegation evidence must not confirm anything again.
        again = resources.group_dispatch(self.db, req)
        self.assertEqual(again['state'], 'idempotent')
        self.assertEqual(again['selected_ids'], ['appB', 'appC'])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM decision_log WHERE subsystem='delegation' AND object_id LIKE 'd-thr:%' AND state='selected'")
            .fetchone()[0], 2)

    def test_recompute_after_safe_window_revoked(self):
        abc_world(self.db)
        req = {'id': 'd-eme', 'kind': 'emergency', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        resources.group_dispatch(self.db, req)
        bad = {'gauges': [{'event_id': 'g-new', 'station': 'A',
                           'at': '2026-11-17T10:00:00', 'datum': 'local',
                           'depth': 8, 'version': 2}],
               'templates': fixture('waterway-ok.json')['templates']}
        self.assertEqual(waterway.import_data(self.db, bad)[0]['state'], 'unsafe')
        recalc = resources.recompute_group(self.db, 'd-eme')
        self.assertTrue(recalc['changed'])
        self.assertEqual(recalc['selected_ids'], [])
        self.assertTrue(all(i['reason'] == 'safe passage window unavailable'
                            for i in recalc['items']))
        # Original ledger row still records appA as confirmed under the old version.
        self.assertEqual(json.loads(self.delegation_row('d-eme')['selected_ids']), ['appA'])

    # ------------------------------------------------------------------
    # Physical occupancy is a hard boundary even when the berth looks free
    # on the allocation ledger.
    # ------------------------------------------------------------------
    def test_open_occupancy_blocks_group_confirmation(self):
        abc_world(self.db)
        arrival.import_data(self.db, {'occupancies': [{
            'event_id': 'o-block', 'voyage': 'VPHYS', 'berth': 'B1',
            'start': '2026-11-17T09:30:00'}]})
        for kind in ('emergency', 'throughput'):
            plan = resources.group_dispatch(self.db, {
                'id': 'd-block-' + kind, 'kind': kind, 'berth': 'B1',
                'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'})
            self.assertEqual(plan['selected_ids'], [])
            self.assertTrue(all(i['reason'] == 'berth occupied' for i in plan['items']))
            self.assertEqual(plan['state'], 'no feasible candidate')
        self.assertEqual(set(self.states('appA', 'appB', 'appC').values()), {'waiting'})
        row = self.delegation_row('d-block-emergency')
        self.assertEqual(row['state'], 'no feasible candidate')
        self.assertEqual(json.loads(row['selected_ids']), [])

    def test_hard_failure_cannot_be_packed_around(self):
        # B loses its notice; the throughput pack must not silently treat B as
        # usable nor move any confirmed allocation to reach the voyage target.
        ids = abc_world(self.db)
        notice.import_data(self.db, {'withdrawals': [{
            'event_id': 'w-kill-b', 'target_event_id': 'n-VB', 'signer': 'harbor-office',
            'issued_at': '2026-11-16T13:00:00', 'reason': 'clerical correction'}]})
        # Explicit candidate set keeps the whole original queue in view even
        # though the withdrawal backfilled one slot one-by-one.
        plan = resources.group_dispatch(self.db, {
            'id': 'd-hard', 'kind': 'throughput', 'candidate_ids': ids})
        items = {i['id']: i for i in plan['items']}
        self.assertEqual(items['appB']['reason'], 'notice not accepted')
        # A spans both hours, so once B is unusable at most one ship fits;
        # the highest-priority feasible ship wins deterministically.
        self.assertEqual(plan['selected_ids'], ['appA'])

    # ------------------------------------------------------------------
    # Idempotent replay: same evidence never confirms twice; changed evidence
    # is rejected rather than rewriting the recorded contract.
    # ------------------------------------------------------------------
    def test_idempotent_replay_and_immutable_record(self):
        abc_world(self.db)
        req = {'id': 'd-idem', 'kind': 'emergency', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        first = resources.group_dispatch(self.db, req)
        second = resources.group_dispatch(self.db, copy.deepcopy(req))
        self.assertEqual(second['state'], 'idempotent')
        self.assertEqual(second['selected_ids'], first['selected_ids'])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM delegations WHERE id='d-idem'").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM allocations WHERE state='confirmed' AND id='appA'").fetchone()[0], 1)
        tampered = dict(req, end='2026-11-17T12:00:00')
        with self.assertRaises(ValueError):
            resources.group_dispatch(self.db, tampered)
        # Re-importing the original applications also cannot double-confirm.
        replay = resources.import_data(self.db, {'applications': [
            app('appA', 'VA', '2026-11-17T09:00:00', '2026-11-17T11:00:00', 10)]})
        self.assertEqual(replay[0]['state'], 'idempotent')

    def test_preview_does_not_write_and_matches_dispatch(self):
        abc_world(self.db)
        req = {'id': 'd-preview', 'kind': 'throughput', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        preview = arrival.preview_dispatch(self.db, req)
        self.assertEqual(preview['selected_ids'], ['appB', 'appC'])
        self.assertIsNone(self.db.execute(
            "SELECT * FROM delegations WHERE id='d-preview'").fetchone())
        self.assertEqual(set(self.states('appA', 'appB', 'appC').values()), {'waiting'})
        confirmed = resources.group_dispatch(self.db, req)
        self.assertEqual(confirmed['selected_ids'], preview['selected_ids'])

    def test_dispatch_inside_arrival_batch_is_atomic_and_idempotent(self):
        abc_world(self.db)
        req = {'id': 'd-arrival', 'kind': 'emergency', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        batch = {'occupancies': [{'event_id': 'o-note', 'voyage': 'VNOTE', 'berth': 'B2',
                                  'start': '2026-11-17T08:30:00',
                                  'end': '2026-11-17T08:45:00'}],
                 'delegations': [req]}
        out = arrival.import_data(self.db, batch)
        self.assertEqual(out[-1]['selected_ids'], ['appA'])
        again = arrival.import_data(self.db, copy.deepcopy(batch))
        self.assertEqual(again[-1]['state'], 'idempotent')
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM delegations WHERE id='d-arrival'").fetchone()[0], 1)

    def test_throughput_tie_break_is_stable_and_explainable(self):
        # Two equal-priority one-hour ships contend for the same first hour;
        # the lexicographic id tie-break must always pick the same one and say so.
        abc_world(self.db, release=False)  # P still holds the slot
        extra = app('appB2', 'VB2', '2026-11-17T09:00:00', '2026-11-17T10:00:00', 6)
        notice.import_data(self.db, {'notices': [
            n('VB2', '2026-11-17T09:00:00', event_id='n-VB2')]})
        resources.import_data(self.db, {'applications': [extra]})  # waits: P occupies B1
        resources.invalidate_voyage(self.db, 'VP', 'berth released', promote_after=False)
        plan = resources.group_dispatch(self.db, {
            'id': 'd-tie', 'kind': 'throughput', 'berth': 'B1',
            'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'})
        self.assertEqual(plan['selected_ids'], ['appB', 'appC'])
        items = {i['id']: i for i in plan['items']}
        self.assertEqual(items['appB2']['state'], 'rejected')
        self.assertIn('tie-break', items['appB']['reason'])

    def test_journal_replay_reproduces_group_ledger(self):
        # Replay the same ordered evidence on a fresh database; the delegation
        # ledgers must match (report.audit compares the stable columns).
        abc_world(self.db)
        req = {'id': 'd-replay', 'kind': 'throughput', 'berth': 'B1',
               'start': '2026-11-17T09:00:00', 'end': '2026-11-17T11:00:00'}
        resources.group_dispatch(self.db, req)
        fresh = connect(':memory:')
        try:
            abc_world(fresh)
            resources.group_dispatch(fresh, copy.deepcopy(req))
            for name in ('delegations', 'delegation_items'):
                cols = ('id,kind,sha,state,reason,candidate_ids,selected_ids,evidence_id'
                        if name == 'delegations' else '*')
                left = [tuple(r) for r in self.db.execute(
                    f'SELECT {cols} FROM {name} ORDER BY 1')]
                right = [tuple(r) for r in fresh.execute(
                    f'SELECT {cols} FROM {name} ORDER BY 1')]
                self.assertEqual(left, right)
        finally:
            fresh.close()

    def test_report_contains_group_ledgers(self):
        abc_world(self.db)
        resources.group_dispatch(self.db, {'id': 'd-rpt', 'kind': 'emergency',
                                           'berth': 'B1',
                                           'start': '2026-11-17T09:00:00',
                                           'end': '2026-11-17T11:00:00'})
        data = report.build(self.db)
        self.assertEqual(data['delegations'][0]['id'], 'd-rpt')
        self.assertEqual(len(data['delegation_items']), 3)


if __name__ == '__main__':
    unittest.main()
