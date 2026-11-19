import unittest

from portbook.store import connect
from portbook import pollution


class PollutionTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def test_independent_sample_result_and_replay(self):
        batch = {
            'limits': [{'event_id': 'l1', 'station': 'P1', 'marker': 'oil',
                        'version': 1, 'ceiling': 0.4, 'signer': 'lab'}],
            'samples': [{'event_id': 's1', 'station': 'P1', 'marker': 'oil',
                         'sampled_at': '2026-03-10T08:15:00+00:00',
                         'reading': 0.7, 'laboratory': 'lab'}],
        }
        result = pollution.import_data(self.db, batch)
        self.assertEqual((result['imported'], result['cases'][0]['state']),
                         (2, 'exceeded'))
        self.assertEqual(pollution.import_data(self.db, batch)['imported'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pollution_cases').fetchone()[0], 1)

    def test_same_version_conflict_and_rollback(self):
        first = {'event_id': 'l1', 'station': 'P1', 'marker': 'oil',
                 'version': 1, 'ceiling': 0.4, 'signer': 'lab'}
        pollution.import_data(self.db, {'limits': [first]})
        changed = dict(first, event_id='l2', ceiling=0.5)
        self.assertEqual(pollution.import_data(self.db, {'limits': [changed]})['cases'][0]['state'],
                         'pending')
        with self.assertRaises(ValueError):
            pollution.import_data(self.db, {'limits': [dict(first, ceiling=0.6)]})


if __name__ == '__main__':
    unittest.main()
