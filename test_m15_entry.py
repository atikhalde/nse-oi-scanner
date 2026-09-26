import unittest
import pandas as pd
from m15_entry import candidate

class EntryTest(unittest.TestCase):
    def bars(self):
        times = pd.date_range('2026-09-25 09:15', periods=9, freq='5min')
        return pd.DataFrame({'t': times.strftime('%H:%M'), 'open': [100.0]*9,
                             'high': [101.0]*9, 'low': [99.0]*9, 'close': [100.0]*9})

    def test_reclaim_requires_closed_directional_sweep(self):
        b = self.bars()
        b.loc[7, ['open', 'low', 'close']] = [98.5, 98, 100.2]
        self.assertEqual(candidate(b, 7)['side'], 'BUY')
        self.assertIsNone(candidate(b, 6))
        b.loc[7, 'close'] = 98.5
        self.assertIsNone(candidate(b, 7))

if __name__ == '__main__':
    unittest.main()
