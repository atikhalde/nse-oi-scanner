#!/usr/bin/env python3
import datetime as dt,os
import fast_feed as F

def ok(n,c):assert c,n;print('PASS',n)
class Resp:
 def __init__(self,status=200,payload=None):self.status_code=status;self._p=payload or {};self.content=b''
 def raise_for_status(self):
  if self.status_code>=400:raise F.requests.HTTPError(f'HTTP {self.status_code}',response=self)
 def json(self):return self._p
def yahoo_payload():return {'chart':{'result':[{'timestamp':[1786410900], 'indicators':{'quote':[{'open':[100],'high':[101],'low':[99],'close':[100.5],'volume':[1000]}]}}]}}
def main():
 oldtok=os.environ.get('DHAN_TOKEN');oldpost,oldget=F.requests.post,F.requests.get;calls={'post':0,'get':0}
 try:
  os.environ['DHAN_TOKEN']='dummy'
  def post(*a,**k):calls['post']+=1;return Resp(429)
  def get(*a,**k):calls['get']+=1;return Resp(200,yahoo_payload())
  F.requests.post=post;F.requests.get=get;c=F.FastFeedCycle();now=dt.datetime(2026,8,11,10,0,tzinfo=dt.timezone(dt.timedelta(hours=5,minutes=30)))
  d,s=c.fetch('AAA',1,now);ok('first Dhan 429 falls through immediately to Yahoo',s=='yahoo-fast' and len(d)==1 and calls=={'post':1,'get':1})
  d,s=c.fetch('BBB',2,now);ok('circuit breaker skips Dhan for every remaining symbol',s=='yahoo-fast' and calls=={'post':1,'get':2})
  ok('429 reason logged once per cycle',c.trip_reason=='HTTP 429')
  # Yahoo receives one attempt only.
  def badget(*a,**k):calls['get']+=1;raise F.requests.Timeout('x')
  F.requests.get=badget;before=calls['get'];d,s=c.fetch('CCC',3,now);ok('Yahoo failure has no retry or sleep',d is None and s=='none' and calls['get']==before+1)
 finally:
  F.requests.post,F.requests.get=oldpost,oldget
  if oldtok is None:os.environ.pop('DHAN_TOKEN',None)
  else:os.environ['DHAN_TOKEN']=oldtok
 print('ALL FAST FEED TESTS PASSED')
if __name__=='__main__':main()
