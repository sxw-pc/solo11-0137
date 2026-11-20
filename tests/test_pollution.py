import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from portbook.store import connect
from portbook import pollution, report

ROOT = Path(__file__).resolve().parents[1]


def qc_fixture():
    return copy.deepcopy(json.loads((ROOT / 'fixtures' / 'pollution-qc-ok.json').read_text()))


def base_batch(batch_id='B1', station='P1'):
    """完整、可签发的质控批次，r = c + 0.25 校准线。"""
    return {
        'calibration_windows': [
            {'event_id': f'w-{batch_id}', 'instrument': 'spec-1', 'batch_id': batch_id,
             'marker': 'oil', 'unit': 'mg/L', 'signer': 'harbor-lab',
             'start_at': '2026-03-10T07:00:00+00:00', 'end_at': '2026-03-10T12:00:00+00:00'}],
        'standards': [
            {'event_id': f'std-{batch_id}-1', 'batch_id': batch_id, 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:30:00+00:00', 'known_conc': 0.2,
             'response': 0.45, 'tolerance': 0.02},
            {'event_id': f'std-{batch_id}-2', 'batch_id': batch_id, 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:35:00+00:00', 'known_conc': 0.4,
             'response': 0.65, 'tolerance': 0.02},
            {'event_id': f'std-{batch_id}-3', 'batch_id': batch_id, 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:40:00+00:00', 'known_conc': 0.6,
             'response': 0.85, 'tolerance': 0.02}],
        'blanks': [
            {'event_id': f'blk-{batch_id}', 'batch_id': batch_id, 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:45:00+00:00',
             'response_lo': 0.04, 'response_hi': 0.04}],
        'spikes': [
            {'event_id': f'spk-{batch_id}', 'batch_id': batch_id, 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T08:40:00+00:00', 'added_conc': 0.2,
             'response_lo': 0.89, 'response_hi': 0.91,
             'recovery_min': 0.9, 'recovery_max': 1.1}],
        'parallels': [
            {'event_id': f'par-{batch_id}', 'sample_event': f's-{batch_id}',
             'batch_id': batch_id, 'marker': 'oil', 'instrument': 'spec-1',
             'signer': 'harbor-lab', 'measured_at': '2026-03-10T08:20:00+00:00',
             'response_lo': 0.7, 'response_hi': 0.7}],
        'limits': [
            {'event_id': f'l-{batch_id}', 'station': station, 'marker': 'oil',
             'version': 1, 'ceiling': 0.4, 'unit': 'mg/L', 'signer': 'harbor-lab'}],
        'samples': [
            {'event_id': f's-{batch_id}', 'station': station, 'marker': 'oil',
             'batch_id': batch_id, 'sampled_at': '2026-03-10T08:15:00+00:00',
             'reading': 0.7, 'unit': 'mg/L', 'instrument': 'spec-1',
             'laboratory': 'lab-a'}],
    }


class PollutionTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def warning(self, station='P1', marker='oil'):
        return dict(self.db.execute(
            'SELECT * FROM pollution_cases WHERE station=? AND marker=?',
            (station, marker)).fetchone())

    def formal(self, station='P1', marker='oil'):
        return dict(self.db.execute(
            'SELECT * FROM pollution_verdicts WHERE station=? AND marker=?',
            (station, marker)).fetchone())

    # ------------------------------------------------ 双轨：预警与正式结论分离

    def test_warning_and_formal_verdict_are_stored_separately(self):
        pollution.import_data(self.db, base_batch())
        warning = self.warning()
        formal = self.formal()
        # 同一 P1 油类 reading=0.7、ceiling=0.4：值班预警仍然 exceeded
        self.assertEqual(warning['state'], 'exceeded')
        self.assertAlmostEqual(warning['excess'], 0.3)
        # 正式签发在另一张表，字段互不覆盖
        self.assertNotIn('formal_state', warning.keys())
        self.assertNotIn('state', formal.keys())
        self.assertEqual(formal['formal_state'], 'exceeded')
        # decide 同时返回两条轨道
        case = pollution.decide(self.db, 'P1', 'oil')
        self.assertEqual(case['state'], 'exceeded')
        self.assertEqual(case['formal_state'], 'exceeded')

    def test_same_raw_reading_different_formal_conclusions(self):
        data = qc_fixture()
        result = pollution.import_data(self.db, data)
        states = {c['station']: c for c in result['cases']}
        # 两批原始 reading 都是 0.7，值班预警完全相同
        self.assertEqual(states['P1']['state'], 'exceeded')
        self.assertEqual(states['P2']['state'], 'exceeded')
        # P1 真实浓度只能确认在 [0.35, 0.45]，实验室不能签发必然超限
        p1 = self.formal('P1')
        self.assertEqual(p1['formal_state'], 'indeterminate')
        self.assertAlmostEqual(p1['conc_lo'], 0.35)
        self.assertAlmostEqual(p1['conc_hi'], 0.45)
        # P2 复测/平行把下界收紧到 0.41，可正式签发超限
        p2 = self.formal('P2')
        self.assertEqual(p2['formal_state'], 'exceeded')
        self.assertAlmostEqual(p2['conc_lo'], 0.41)
        self.assertAlmostEqual(p2['conc_hi'], 0.41)
        # 对两批使用同一个固定扣空白量会得到相同的错误结果
        fixed = 0.7 - 0.04 - 0.25
        self.assertGreater(fixed, 0.4)  # 固定扣空白会把 P1 也判超限
        # 只看中心值同样错误：P1 中心 0.40 恰在限值上，无法定论
        center = (p1['conc_lo'] + p1['conc_hi']) / 2
        self.assertLessEqual(center, 0.4 + 1e-9)

    def test_formal_pending_without_qc_evidence_but_warning_remains(self):
        batch = base_batch()
        # 移除全部实验室质控证据，只留原始样本与限值
        for key in ('calibration_windows', 'standards', 'blanks', 'spikes', 'parallels'):
            del batch[key]
        pollution.import_data(self.db, batch)
        self.assertEqual(self.warning()['state'], 'exceeded')
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'pending')
        self.assertIsNone(formal['conc_lo'])
        self.assertIn('batch blank evidence required',
                      json.loads(formal['evidence'])['reasons'])

    # ------------------------------------------------ 校准解释与复测

    def test_two_equally_valid_calibration_interpretations_force_review(self):
        batch = base_batch()
        # 四个标准点，std3 被污染；两条独立复测各支持一条满分单调直线，
        # 一条使样本必然超限，另一条允许不超限
        batch['standards'] = [
            {'event_id': 'std-B1-1', 'batch_id': 'B1', 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:30:00+00:00', 'known_conc': 0.2,
             'response': 0.315, 'tolerance': 0.06},
            {'event_id': 'std-B1-2', 'batch_id': 'B1', 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:34:00+00:00', 'known_conc': 0.4,
             'response': 0.455, 'tolerance': 0.06},
            {'event_id': 'std-B1-3', 'batch_id': 'B1', 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:38:00+00:00', 'known_conc': 0.6,
             'response': 0.79, 'tolerance': 0.06},
            {'event_id': 'std-B1-4', 'batch_id': 'B1', 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:42:00+00:00', 'known_conc': 0.8,
             'response': 0.735, 'tolerance': 0.06}]
        batch['standard_retests'] = [
            {'event_id': 'rt3a', 'standard_event': 'std-B1-3', 'batch_id': 'B1',
             'marker': 'oil', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T09:00:00+00:00', 'response': 0.63,
             'reason': 'contamination suspected'},
            {'event_id': 'rt3b', 'standard_event': 'std-B1-3', 'batch_id': 'B1',
             'marker': 'oil', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T09:05:00+00:00', 'response': 0.56,
             'reason': 'independent duplicate retest'}]
        # 宽回收带：两套校准线下加标增量差异较大，回收约束不再额外淘汰解释
        batch['spikes'][0].update(response_lo=0.63, response_hi=0.75,
                                  recovery_min=0.8, recovery_max=1.2)
        # 样本响应取在两条校准线之间：线 A 下 C=0.3833 合格，线 B 下 C=0.4029 超限
        batch['samples'][0]['reading'] = 0.49
        batch['parallels'][0].update(response_lo=0.49, response_hi=0.49)
        pollution.import_data(self.db, batch)
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'review_required')
        detail = json.loads(formal['evidence'])
        # 两套证据都必须展示，且都保留 4 个证据事件
        self.assertEqual(len(detail['interpretations']), 2)
        self.assertTrue(all(len(i['retained_evidence']) == 4
                            for i in detail['interpretations']))
        concs = [(i['concentration_lo'], i['concentration_hi'])
                 for i in detail['interpretations']]
        self.assertTrue(any(hi <= 0.4 for lo, hi in concs))
        self.assertTrue(any(lo > 0.4 for lo, hi in concs))

    def test_anomalous_standard_without_retest_cannot_be_dropped(self):
        batch = base_batch()
        # 污染 std2 且没有复测：不允许随意丢点，没有任何合格校准解释
        batch['standards'][1]['response'] = 0.99
        pollution.import_data(self.db, batch)
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'pending')
        self.assertIn('no monotone calibration interpretation satisfies QC',
                      json.loads(formal['evidence'])['reasons'])
        # 预警不受影响
        self.assertEqual(self.warning()['state'], 'exceeded')

    def test_retest_evidence_allows_isolation_and_signing(self):
        batch = base_batch()
        batch['standards'][1]['response'] = 0.99  # 污染点
        batch['standard_retests'] = [
            {'event_id': 'rt2', 'standard_event': 'std-B1-2', 'batch_id': 'B1',
             'marker': 'oil', 'instrument': 'spec-1', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T09:00:00+00:00', 'response': 0.65,
             'reason': 'repeated measurement after cuvette rinse'}]
        pollution.import_data(self.db, batch)
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'exceeded')
        detail = json.loads(formal['evidence'])
        self.assertIn('rt2', detail['retests'])

    # ------------------------------------------------ 空白/回收相关约束

    def test_blank_and_recovery_are_joint_not_independent_extremes(self):
        batch = base_batch()
        batch['blanks'][0].update(response_lo=0.02, response_hi=0.09)
        batch['parallels'][0].update(response_lo=0.69, response_hi=0.72)
        # 窄回收带使 r_s=0.72 的端点在联合域中不可行
        batch['spikes'][0].update(response_lo=0.88, response_hi=0.90,
                                  recovery_min=0.95, recovery_max=1.05)
        pollution.import_data(self.db, batch)
        formal = self.formal()
        # 联合约束：上界 0.42；独立取极值会错误地给出 0.45
        self.assertAlmostEqual(formal['conc_lo'], 0.35)
        self.assertAlmostEqual(formal['conc_hi'], 0.42)
        independent_hi = 0.72 - 0.02 - 0.25
        self.assertGreater(independent_hi, formal['conc_hi'])

    def test_infeasible_recovery_band_blocks_signing(self):
        batch = base_batch()
        batch['spikes'][0].update(response_lo=1.5, response_hi=1.6)
        pollution.import_data(self.db, batch)
        self.assertEqual(self.formal()['formal_state'], 'pending')
        self.assertEqual(self.warning()['state'], 'exceeded')

    # ------------------------------------------------ 单位与检出界

    def test_unit_mismatch_blocks_formal_signing(self):
        batch = base_batch()
        batch['limits'][0]['unit'] = 'ug/L'
        pollution.import_data(self.db, batch)
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'pending')
        self.assertIn('sample unit does not match signed limit unit',
                      json.loads(formal['evidence'])['reasons'])

    def test_detection_bounds_clip_response_interval(self):
        batch = base_batch()
        batch['samples'][0]['det_lower'] = 0.72  # reading 0.70 低于检出下界
        pollution.import_data(self.db, batch)
        formal = self.formal()
        detail = json.loads(formal['evidence'])
        self.assertIn('response clipped at lower detection bound', detail['reasons'])
        # 响应被截断到检出下界 0.72（平行样 0.70 同样被截）；联合回收带进一步收紧
        detail = json.loads(formal['evidence'])
        self.assertIn('response clipped at lower detection bound', detail['reasons'])
        self.assertAlmostEqual(formal['conc_hi'], 0.72 - 0.04 - 0.25)
        self.assertLessEqual(formal['conc_lo'], formal['conc_hi'])
        self.assertGreaterEqual(formal['conc_lo'], 0.4)

    # ------------------------------------------------ 仪器与授权时段

    def test_calibration_not_mixed_across_instrument_or_window(self):
        batch = base_batch()
        # 样本在 spec-2 测量，但标准液/授权时段只覆盖 spec-1
        batch['samples'][0]['instrument'] = 'spec-2'
        pollution.import_data(self.db, batch)
        formal = self.formal()
        self.assertEqual(formal['formal_state'], 'pending')
        self.assertIn('sample outside authorized calibration window/instrument',
                      json.loads(formal['evidence'])['reasons'])

    def test_sample_outside_authorized_window_is_pending(self):
        batch = base_batch()
        batch['calibration_windows'][0]['end_at'] = '2026-03-10T08:00:00+00:00'
        pollution.import_data(self.db, batch)
        self.assertEqual(self.formal()['formal_state'], 'pending')

    def test_other_batch_standards_are_not_reused(self):
        batch = base_batch()
        for std in batch['standards']:
            std['batch_id'] = 'OTHER-BATCH'
        pollution.import_data(self.db, batch)
        self.assertEqual(self.formal()['formal_state'], 'pending')

    def test_qc_events_on_other_instrument_are_ignored(self):
        batch = base_batch()
        # 一条 spec-2 的标准液不能混进 spec-1 样本的校准
        batch['standards'].append(
            {'event_id': 'std-foreign', 'batch_id': 'B1', 'marker': 'oil',
             'unit': 'mg/L', 'instrument': 'spec-2', 'signer': 'harbor-lab',
             'measured_at': '2026-03-10T07:50:00+00:00', 'known_conc': 1.0,
             'response': 9.9, 'tolerance': 0.02})
        pollution.import_data(self.db, batch)
        # spec-1 自身三点仍构成合格校准，正式结论不受 foreign 点影响
        self.assertEqual(self.formal()['formal_state'], 'exceeded')

    def test_window_withdrawal_recomputes_to_pending(self):
        pollution.import_data(self.db, base_batch())
        self.assertEqual(self.formal()['formal_state'], 'exceeded')
        pollution.import_data(self.db, {
            'withdrawals': [
                {'event_id': 'wd-window', 'target_event_id': 'w-B1',
                 'batch_id': 'B1', 'marker': 'oil', 'signer': 'harbor-lab',
                 'reason': 'authorization window revoked'}]})
        self.assertEqual(self.formal()['formal_state'], 'pending')
        # 窗口源事件仍保留可追查
        self.assertIsNotNone(self.db.execute(
            'SELECT event_id FROM evidence WHERE event_id=?', ('w-B1',)).fetchone())

    # ------------------------------------------------ 撤销、重算与历史保留

    def test_standard_withdrawal_recomputes_batch_but_keeps_history(self):
        pollution.import_data(self.db, base_batch())
        first = self.formal()
        self.assertEqual(first['formal_state'], 'exceeded')
        # 撤销两条标准液：本批只剩一个标准点，无法建立合格校准，正式结论转 pending
        pollution.import_data(self.db, {
            'withdrawals': [
                {'event_id': 'wd-std2', 'target_event_id': 'std-B1-2',
                 'batch_id': 'B1', 'marker': 'oil', 'signer': 'harbor-lab',
                 'reason': 'standard solution recalled by supplier'},
                {'event_id': 'wd-std3', 'target_event_id': 'std-B1-3',
                 'batch_id': 'B1', 'marker': 'oil', 'signer': 'harbor-lab',
                 'reason': 'same recall campaign'}]})
        self.assertEqual(self.formal()['formal_state'], 'pending')
        # 旧签发仍可追查
        history = self.db.execute(
            'SELECT formal_state FROM pollution_verdict_history').fetchall()
        self.assertEqual([row['formal_state'] for row in history], ['exceeded'])
        # 源事件原始字节保留
        self.assertIsNotNone(self.db.execute(
            'SELECT event_id FROM evidence WHERE event_id=?', ('std-B1-2',)).fetchone())
        # 预警始终独立保留
        self.assertEqual(self.warning()['state'], 'exceeded')

    def test_new_sample_event_recomputes_and_archives_previous_verdict(self):
        pollution.import_data(self.db, base_batch())
        follow_up = {
            'samples': [
                {'event_id': 's-B1-new', 'station': 'P1', 'marker': 'oil',
                 'batch_id': 'B1', 'sampled_at': '2026-03-11T08:15:00+00:00',
                 'reading': 0.3, 'unit': 'mg/L', 'instrument': 'spec-1',
                 'laboratory': 'lab-a'}],
            'parallels': [
                {'event_id': 'par-new', 'sample_event': 's-B1-new',
                 'batch_id': 'B1', 'marker': 'oil', 'instrument': 'spec-1',
                 'signer': 'harbor-lab', 'measured_at': '2026-03-11T08:20:00+00:00',
                 'response_lo': 0.3, 'response_hi': 0.3}]}
        pollution.import_data(self.db, follow_up)
        self.assertEqual(self.warning()['state'], 'within_limit')
        self.assertEqual(self.warning()['sample_event'], 's-B1-new')
        history = self.db.execute(
            'SELECT formal_state FROM pollution_verdict_history').fetchall()
        self.assertEqual([row['formal_state'] for row in history], ['exceeded'])

    def test_idempotent_same_bytes_and_rollback_changed_bytes(self):
        batch = base_batch()
        pollution.import_data(self.db, batch)
        # 同事件同字节重放：幂等，不新增签发历史
        self.assertEqual(pollution.import_data(self.db, batch)['imported'], 0)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM pollution_verdict_history').fetchone()[0], 0)
        # 异字节：整批回滚
        changed = copy.deepcopy(batch)
        changed['blanks'][0]['response_hi'] = 0.5
        with self.assertRaises(ValueError):
            pollution.import_data(self.db, changed)
        self.assertEqual(self.db.execute(
            "SELECT response_hi FROM pollution_blanks WHERE event_id='blk-B1'").fetchone()[0],
            0.04)

    # ------------------------------------------------ 总览联动与 CLI

    def test_report_overview_links_warning_and_formal_tracks(self):
        pollution.import_data(self.db, qc_fixture())
        built = report.build(self.db)
        self.assertIn('pollution_cases', built)
        self.assertIn('pollution_verdicts', built)
        overview = {row['station']: row for row in built['pollution_overview']}
        p1, p2 = overview['P1'], overview['P2']
        self.assertEqual(p1['relation'], 'unresolved')
        self.assertEqual(p1['warning_state'], 'exceeded')
        self.assertEqual(p1['formal_state'], 'indeterminate')
        self.assertEqual(p2['relation'], 'same')
        # 指向两条轨道分别使用的限值、样本、标准液和质控事件
        self.assertEqual(p1['warning_limit_event'], 'limit-p1')
        self.assertEqual(p1['formal_sample_event'], 'sample-p1')
        refs = p1['evidence_refs']
        self.assertTrue(refs['standards'] and refs['blanks']
                        and refs['spikes'] and refs['parallels']
                        and refs['windows'])

    def test_cli_import_report_and_audit_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'cli.db')
            fixture_path = ROOT / 'fixtures' / 'pollution-qc-ok.json'
            run = subprocess.run(
                [sys.executable, '-m', 'portbook', 'pollution-import',
                 '--db', db_path, '--file', str(fixture_path)],
                cwd=ROOT, capture_output=True, text=True, check=True)
            cases = {c['station']: c for c in json.loads(run.stdout)['cases']}
            self.assertEqual(cases['P1']['state'], 'exceeded')
            self.assertEqual(cases['P1']['formal_state'], 'indeterminate')
            rendered = subprocess.run(
                [sys.executable, '-m', 'portbook', 'report', '--db', db_path],
                cwd=ROOT, capture_output=True, text=True, check=True)
            overview = {row['station']: row
                        for row in json.loads(rendered.stdout)['pollution_overview']}
            self.assertEqual(overview['P2']['relation'], 'same')
            audit = subprocess.run(
                [sys.executable, '-m', 'portbook', 'audit', '--db', db_path],
                cwd=ROOT, capture_output=True, text=True, check=True)
            result = json.loads(audit.stdout)
            self.assertTrue(result['replay_ok'])
            self.assertEqual(result['mismatches'], [])


class PollutionLegacyTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def test_independent_sample_result_and_replay(self):
        batch = {
            'limits': [{'event_id': 'l1', 'station': 'P1', 'marker': 'oil',
                        'version': 1, 'ceiling': 0.4, 'signer': 'lab'}],
            'samples': [{'event_id': 's1', 'station': 'P1', 'marker': 'oil',
                         'sampled_at': '2026-03-10T08:15:00+00:00',
                         'reading': 0.7, 'laboratory': 'lab'}]}
        result = pollution.import_data(self.db, batch)
        self.assertEqual((result['imported'], result['cases'][0]['state']),
                         (2, 'exceeded'))
        self.assertEqual(pollution.import_data(self.db, batch)['imported'], 0)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM pollution_cases').fetchone()[0], 1)
        # 无批次质控的旧数据：正式结论只能待证，预警不被覆盖
        self.assertEqual(result['cases'][0]['formal_state'], 'pending')

    def test_same_version_conflict_and_rollback(self):
        first = {'event_id': 'l1', 'station': 'P1', 'marker': 'oil',
                 'version': 1, 'ceiling': 0.4, 'signer': 'lab'}
        pollution.import_data(self.db, {'limits': [first]})
        changed = dict(first, event_id='l2', ceiling=0.5)
        self.assertEqual(pollution.import_data(
            self.db, {'limits': [changed]})['cases'][0]['state'], 'pending')
        with self.assertRaises(ValueError):
            pollution.import_data(
                self.db, {'limits': [dict(first, ceiling=0.6)]})


if __name__ == '__main__':
    unittest.main()
