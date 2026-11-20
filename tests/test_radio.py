import unittest
from portbook.store import connect
from portbook import radio, radio_ack, report


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def test_order_and_ack_independence(self):
        order = {'event_id': 'o1', 'command_id': 'c1', 'voyage': 'v1',
                 'revision': 1, 'action': 'hold', 'issuer': 'master',
                 'issued_at': '2026-03-10T08:00:00+00:00'}
        ack = {'event_id': 'a1', 'command_id': 'c1', 'voyage': 'v1',
               'actor': 'pilot', 'response': 'received',
               'received_at': '2026-03-10T08:01:00+00:00'}
        self.assertEqual(radio.import_data(self.db, {'orders': [order]})['cases'][0]['state'],
                         'unconfirmed')
        self.assertEqual(radio_ack.import_data(self.db, {'acks': [ack]})['cases'][0]['state'],
                         'acknowledged')
        self.assertEqual(radio_ack.import_data(self.db, {'acks': [ack]})['imported'], 0)
        with self.assertRaises(ValueError):
            radio_ack.import_data(self.db, {'acks': [dict(ack, response='refused')]})


AUTH = {'issuer': 'harbor-master', 'scope': 'harbor-release',
        'valid_from': '2026-01-01T00:00:00+00:00', 'valid_to': '2027-01-01T00:00:00+00:00'}


def directive(event_id, command_id='C1', voyage='V1', revision=1, phase='prepare',
              action='proceed', digest='D1', issuer='harbor-master',
              scope='harbor-release', parents=None,
              issued_at='2026-03-10T08:00:00+00:00'):
    return {'event_id': event_id, 'command_id': command_id, 'voyage': voyage,
            'revision': revision, 'phase': phase, 'action': action, 'digest': digest,
            'issuer': issuer, 'scope': scope, 'parents': list(parents or []),
            'issued_at': issued_at}


def receipt(event_id, order_event, command_id='C1', voyage='V1', actor='pilot-1',
            role='pilot', response='confirm', digest='D1', sender_seq=1,
            receiver_seq=1, parents=None, received_at='2026-03-10T08:01:00+00:00'):
    return {'event_id': event_id, 'command_id': command_id, 'voyage': voyage,
            'order_event': order_event, 'digest': digest, 'actor': actor,
            'role': role, 'response': response, 'sender_seq': sender_seq,
            'receiver_seq': receiver_seq, 'parents': list(parents or []),
            'received_at': received_at}


class FormalRadioTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def cert(self, voyage='V1', command_id='C1'):
        return self.db.execute(
            'SELECT * FROM radio_cert WHERE voyage=? AND command_id=?',
            (voyage, command_id)).fetchone()

    def release_flow(self, voyage='V1', command_id='C1', prefix=''):
        prep = directive(prefix + 'prep', command_id=command_id, voyage=voyage)
        commit = directive(prefix + 'commit', command_id=command_id, voyage=voyage,
                           phase='commit',
                           parents=[prefix + 'cf-pilot', prefix + 'cf-tug'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        cf_pilot = receipt(prefix + 'cf-pilot', prefix + 'prep', command_id=command_id,
                           voyage=voyage, actor='pilot-1', role='pilot', sender_seq=1,
                           parents=[prefix + 'prep'])
        cf_tug = receipt(prefix + 'cf-tug', prefix + 'prep', command_id=command_id,
                         voyage=voyage, actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=[prefix + 'prep'])
        return radio_ack.import_data(self.db, {'receipts': [cf_pilot, cf_tug]})

    def test_effective_release_happy_path(self):
        out = self.release_flow()
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'effective')
        self.assertEqual((cert['revision'], cert['digest'], cert['commit_event']),
                         (1, 'D1', 'commit'))
        personal = {p['actor']: p for p in out['personal']}
        self.assertEqual(personal['pilot-1']['state'], 'complete')
        self.assertEqual(personal['pilot-1']['last_order_event'], 'prep')

    def test_stale_legacy_ack_does_not_certify_new_revision(self):
        orders = [
            {'event_id': 'o1', 'command_id': 'C', 'voyage': 'V1', 'revision': 1,
             'action': 'hold', 'issuer': 'harbor-master',
             'issued_at': '2026-03-10T08:00:00+00:00'},
            {'event_id': 'o2', 'command_id': 'C', 'voyage': 'V1', 'revision': 2,
             'action': 'proceed', 'issuer': 'harbor-master',
             'issued_at': '2026-03-10T08:05:00+00:00'}]
        ack = {'event_id': 'a1', 'command_id': 'C', 'voyage': 'V1', 'actor': 'pilot-1',
               'response': 'received', 'received_at': '2026-03-10T08:01:00+00:00'}
        radio.import_data(self.db, {'orders': orders})
        out = radio_ack.import_data(self.db, {'acks': [ack]})
        # legacy simple status still mistakes the rev1 receipt for rev2
        self.assertEqual(out['cases'][0]['state'], 'acknowledged')
        prep2 = directive('prep2', command_id='C', revision=2, digest='D2')
        out = radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep2]})
        cert = {c['command_id']: c for c in out['certs']}['C']
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('missing confirmation', cert['reason'])
        self.assertIn('no commit', cert['reason'])
        # old conclusion remains queryable
        case = self.db.execute("SELECT state FROM radio_case WHERE voyage='V1'").fetchone()[0]
        self.assertEqual(case, 'acknowledged')

    def test_two_independent_executors_required(self):
        prep = directive('prep')
        commit = directive('commit', phase='commit', parents=['cf-pilot'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        cf_pilot = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [cf_pilot]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('missing confirmation from tug', cert['reason'])

    def test_concurrent_veto_keeps_release_pending_but_personal_complete(self):
        self.release_flow()
        veto = receipt('veto', 'prep', actor='tug-1', role='tug', response='refuse',
                       sender_seq=2, receiver_seq=3, parents=[])
        out = radio_ack.import_data(self.db, {'receipts': [veto]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('concurrent commit and safety veto by tug-1 (veto)', cert['reason'])
        # the pilot's own receipt record is still certifiable after the fact
        personal = {p['actor']: p for p in out['personal']}
        self.assertEqual(personal['pilot-1']['state'], 'complete')
        self.assertEqual(personal['pilot-1']['last_order_event'], 'prep')
        self.assertEqual(personal['tug-1']['state'], 'complete')

    def test_concurrent_revoke_not_decided_by_write_order(self):
        self.release_flow()
        revoke = directive('revoke', phase='revoke', action='cancel', parents=[])
        out = radio.import_data(self.db, {'directives': [revoke]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('concurrent commit and revoke (revoke)', cert['reason'])

    def test_provable_subsequent_revoke_is_vetoed(self):
        self.release_flow()
        revoke = directive('revoke', phase='revoke', action='cancel', parents=['commit'])
        out = radio.import_data(self.db, {'directives': [revoke]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'vetoed')
        self.assertIn('revoke (revoke) causally after commit', cert['reason'])
        self.assertEqual(cert['commit_event'], 'commit')

    def test_missing_causal_edge_stays_possible(self):
        prep = directive('prep')
        commit = directive('commit', phase='commit',
                           parents=['cf-pilot', 'cf-tug', 'ghost'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        cf_pilot = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep'])
        cf_tug = receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [cf_pilot, cf_tug]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('missing causal edge to ghost', cert['reason'])

    def test_same_revision_body_conflict(self):
        p1 = directive('prep-a', digest='D1')
        p2 = directive('prep-b', digest='D2')
        commit = directive('commit', phase='commit', digest='D1',
                           parents=['cf-pilot', 'cf-tug'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [p1, p2, commit]})
        cf_pilot = receipt('cf-pilot', 'prep-a', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep-a'])
        cf_tug = receipt('cf-tug', 'prep-a', actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=['prep-a'])
        out = radio_ack.import_data(self.db, {'receipts': [cf_pilot, cf_tug]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('parallel revision body conflict at revision 1', cert['reason'])
        self.assertIn('commit commit targets conflicted revision 1', cert['reason'])

    def test_causal_cycle_detected(self):
        prep = directive('prep', parents=['commit'])
        commit = directive('commit', phase='commit', parents=['prep'])
        out = radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('causal cycle involving commit,prep', cert['reason'])

    def test_duplicate_role_conflicts(self):
        prep = directive('prep')
        commit = directive('commit', phase='commit', parents=['cf-p1', 'cf-p2', 'cf-tug'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        recs = [receipt('cf-p1', 'prep', actor='pilot-1', role='pilot', sender_seq=1,
                        parents=['prep']),
                receipt('cf-p2', 'prep', actor='pilot-2', role='pilot', sender_seq=1,
                        receiver_seq=2, parents=['prep']),
                receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                        receiver_seq=3, parents=['prep'])]
        out = radio_ack.import_data(self.db, {'receipts': recs})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('duplicate role pilot confirmed by pilot-1,pilot-2', cert['reason'])

    def test_stale_digest_receipt_explained(self):
        prep = directive('prep', digest='D1')
        commit = directive('commit', phase='commit', digest='D1',
                           parents=['cf-pilot', 'cf-tug'])
        radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        stale = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot', digest='D0',
                        sender_seq=1, parents=['prep'])
        ok = receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                     receiver_seq=2, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [stale, ok]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('stale digest on receipt cf-pilot', cert['reason'])
        self.assertIn('missing confirmation from pilot', cert['reason'])

    def test_unauthorized_commit_rejected(self):
        prep = directive('prep')
        commit = directive('commit', phase='commit', parents=['cf-pilot', 'cf-tug'])
        radio.import_data(self.db, {'directives': [prep, commit]})
        cf_pilot = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep'])
        cf_tug = receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [cf_pilot, cf_tug]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('unauthorized issuer', cert['reason'])

    def test_cross_ledger_late_supplement(self):
        # commit references confirmations that have not arrived yet
        prep = directive('prep')
        commit = directive('commit', phase='commit', parents=['cf-pilot', 'cf-tug'])
        out = radio.import_data(self.db, {'authorities': [AUTH], 'directives': [prep, commit]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'possible')
        self.assertIn('missing causal edge to cf-pilot,cf-tug', cert['reason'])
        # the receipts ledger later supplies the confirmations with their parent chain
        cf_pilot = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep'])
        cf_tug = receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [cf_pilot, cf_tug]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'effective')
        self.assertEqual(cert['commit_event'], 'commit')
        # a newer prepare plus commit cannot reuse the old-digest receipts
        prep2 = directive('prep2', revision=2, digest='D2')
        commit2 = directive('commit2', revision=2, phase='commit', digest='D2',
                            parents=['commit'])
        out = radio.import_data(self.db, {'directives': [prep2, commit2]})
        cert = out['certs'][0]
        self.assertEqual(cert['state'], 'effective')
        self.assertEqual((cert['revision'], cert['digest']), (1, 'D1'))
        self.assertIn('missing confirmation from pilot for prepare prep2', cert['reason'])

    def test_replay_is_idempotent(self):
        prep = directive('prep')
        commit = directive('commit', phase='commit', parents=['cf-pilot', 'cf-tug'])
        batch = {'authorities': [AUTH], 'directives': [prep, commit]}
        radio.import_data(self.db, batch)
        cf_pilot = receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                           sender_seq=1, parents=['prep'])
        cf_tug = receipt('cf-tug', 'prep', actor='tug-1', role='tug', sender_seq=1,
                         receiver_seq=2, parents=['prep'])
        abatch = {'receipts': [cf_pilot, cf_tug]}
        radio_ack.import_data(self.db, abatch)
        cert = self.cert()
        self.assertEqual(cert['state'], 'effective')
        self.assertEqual(radio.import_data(self.db, batch)['imported'], 0)
        self.assertEqual(radio_ack.import_data(self.db, abatch)['imported'], 0)
        again = self.cert()
        self.assertEqual((again['state'], again['version'], again['reason']),
                         (cert['state'], cert['version'], cert['reason']))

    def test_changed_event_id_rolls_back_whole_batch(self):
        self.release_flow()
        before = self.db.execute('SELECT count(*) FROM radio_directive').fetchone()[0]
        with self.assertRaises(ValueError):
            radio.import_data(self.db, {'directives': [
                directive('prep2', revision=2, digest='D2'),
                directive('prep', digest='D9')]})
        self.assertEqual(self.db.execute('SELECT count(*) FROM radio_directive').fetchone()[0],
                         before)
        self.assertIsNone(self.db.execute(
            "SELECT event_id FROM evidence WHERE event_id='prep2'").fetchone())
        cert = self.cert()
        self.assertEqual((cert['state'], cert['revision']), ('effective', 1))
        with self.assertRaises(ValueError):
            radio_ack.import_data(self.db, {'receipts': [
                receipt('cf-new', 'prep', actor='pilot-1', role='pilot', sender_seq=2),
                receipt('cf-pilot', 'prep', actor='pilot-1', role='pilot',
                        sender_seq=1, digest='D9')]})
        self.assertIsNone(self.db.execute(
            "SELECT event_id FROM radio_receipt WHERE event_id='cf-new'").fetchone())

    def test_personal_record_gap_incomplete(self):
        self.release_flow()
        extra = receipt('cf-pilot-3', 'prep', actor='pilot-1', role='pilot',
                        response='heard', sender_seq=3, receiver_seq=9, parents=['prep'])
        out = radio_ack.import_data(self.db, {'receipts': [extra]})
        personal = {p['actor']: p for p in out['personal']}
        self.assertEqual(personal['pilot-1']['state'], 'incomplete')
        self.assertIn('gap in sender sequence 2', personal['pilot-1']['reason'])
        # a heard receipt is not an executable confirmation; the release is untouched
        self.assertEqual(self.cert()['state'], 'effective')

    def test_report_lists_release_buckets_with_causal_edges(self):
        self.release_flow()
        self.release_flow(voyage='V2', command_id='C2', prefix='b-')
        radio.import_data(self.db, {'directives': [
            directive('b-revoke', command_id='C2', voyage='V2', phase='revoke',
                      action='cancel', parents=['b-commit'])]})
        radio.import_data(self.db, {'orders': [
            {'event_id': 'o1', 'command_id': 'C9', 'voyage': 'V9', 'revision': 1,
             'action': 'hold', 'issuer': 'master',
             'issued_at': '2026-03-10T08:00:00+00:00'}]})
        data = report.build(self.db)
        eff = [(x['voyage'], x['command_id']) for x in data['radio_release']['effective']]
        veto = [(x['voyage'], x['command_id']) for x in data['radio_release']['vetoed']]
        self.assertEqual(eff, [('V1', 'C1')])
        self.assertEqual(veto, [('V2', 'C2')])
        edges = data['radio_release']['effective'][0]['edges']
        self.assertIn(['commit', 'cf-pilot'], edges)
        self.assertIn(['commit', 'cf-tug'], edges)
        self.assertIn(['cf-pilot', 'prep'], edges)
        # legacy conclusion and raw events stay traceable
        self.assertEqual(data['radio_case'][0]['state'], 'unconfirmed')
        self.assertTrue(data['radio_directive'])
        self.assertTrue(data['radio_receipt'])
        report.render(self.db)


if __name__ == '__main__':
    unittest.main()
