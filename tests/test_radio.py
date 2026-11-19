import unittest
from portbook.store import connect
from portbook import radio, radio_ack


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


if __name__ == '__main__':
    unittest.main()
