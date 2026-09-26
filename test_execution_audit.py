import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import openpyxl
import execution_audit as ea
import two_trade_selector as selector
import report
import trader

IST=ZoneInfo('Asia/Kolkata')

class AuditTests(unittest.TestCase):
    def test_timestamp_preserved_and_no_future_quote(self):
        b=pd.DataFrame({'dt':pd.date_range('2026-09-25 09:30',periods=3,freq='5min',tz=IST),
                        't':['09:30','09:35','09:40'],'close':[100.,101.,200.]})
        tr={'time':'09:30','entry':100.}
        ea.capture(tr,b,datetime(2026,9,25,9,41,tzinfo=IST))
        self.assertEqual(tr['audit']['delay_minutes'],6)
        self.assertEqual(tr['audit']['observation_price'],101.)
        self.assertEqual(ea.preserve(tr,{'entry':100.})['audit'],tr['audit'])

    def test_selector_dedup_cap_stale_and_no_outcome_ranking(self):
        def trade(sym,stamp,delay,signal='SELL-EX8',net=0):
            return {'symbol':sym,'side':'SELL','signal':signal,'audit':{
                'observed_at':stamp,'delay_minutes':delay},'pnl':net}
        ts='2026-09-25T10:00:00+05:30'
        state={'date':'2026-09-25','trades':{
            'a':trade('A',ts,3,net=-900), 'duplicate':trade('A',ts,2,net=900),
            'stale':trade('X','2026-09-25T09:00:00+05:30',30),
            'b':trade('B','2026-09-25T10:01:00+05:30',4),
            'c':trade('C','2026-09-25T10:02:00+05:30',3)}}
        self.assertEqual([x['symbol'] for x in selector.select(state,'2026-09-25')],['A','B'])

    def test_report_separates_open_marks(self):
        times=pd.date_range('2026-09-25 09:15',periods=10,freq='5min',tz=IST)
        bars=pd.DataFrame({'dt':times,'t':times.strftime('%H:%M'),'open':[100.]*10,
                           'high':[101.]*10,'low':[99.]*10,'close':[100.]*10})
        tr=trader.evaluate('TEST','BUY','09:30',100.,'TEST',bars)
        self.assertFalse(tr['closed'])
        with tempfile.TemporaryDirectory() as tmp:
            fp=Path(tmp)/'audit.xlsx';report.build([tr],'test',{},str(fp))
            w=openpyxl.load_workbook(fp,data_only=True)
            self.assertEqual(w['Execution audit']['I2'].value,'OPEN / MARK')
            self.assertIsNone(w['Execution audit']['K2'].value)

if __name__=='__main__':unittest.main()
