import os
import tempfile
import unittest
from unittest.mock import patch
import _fakes
from arb_monitor.core import arb_ctf_redemption as C

class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'ARB_CTF_JOURNAL':self.tmp.name+'/journal.json','ARB_CTF_ROUTE_VERIFIED':'true'});self.env.start();self.addCleanup(self.env.stop)
        self.pos=dict(pair_id='p',shares=10,cost_basis_usdc=9,poly_yes_token='1',kalshi_ticker='K',kalshi_side='NO',status='pending_redeem')
        self.calls=[];self.marks=[];self.visible=100;self.paid=10;self.pending=False;self.fail_submit=False;self.fail_db=False
    def send(self,action,**kw):
        self.calls.append(action)
        if action=='prepare':return dict(payoutRaw=str(self.paid),cashBefore='100')
        if action=='submit':
            if self.fail_submit:raise TimeoutError()
            return dict(txHash='0x'+'ab'*32)
        return {'pending':True} if self.pending else dict(payoutRaw=str(self.paid),cashRaw=str(100+self.paid))
    def mark(self,pair,value):
        if self.fail_db:return False
        self.marks.append((pair,value));return True
    def tick(self):return C.tick(self.pos,send=self.send,kalshi=lambda p:0,cash=lambda:self.visible,mark=self.mark,prepare_mapping=lambda p:{})
    def test_cash_visibility_and_restart_exactly_once(self):
        self.assertEqual(self.tick(),'waiting_cash_visibility');self.visible=110
        self.assertEqual(self.tick(),'reconciled');self.assertEqual(self.tick(),'reconciled')
        self.assertEqual(self.calls.count('submit'),1);self.assertEqual(len(self.marks),1)
    def test_unverified_route_does_not_create_uncertain_submission(self):
        with patch.dict(os.environ,{'ARB_CTF_ROUTE_VERIFIED':'false'}):
            self.assertEqual(self.tick(),'route_unverified')
        self.assertNotIn('submit',self.calls)
        self.assertEqual(self.tick(),'waiting_cash_visibility')
    def test_unknown_submit_never_retries(self):
        self.fail_submit=True
        with self.assertRaises(TimeoutError):self.tick()
        self.assertEqual(self.tick(),'unknown_submission');self.assertEqual(self.calls.count('submit'),1)
    def test_second_pair_waits_for_first_cash_reconciliation(self):
        self.tick()
        self.pos={**self.pos,'pair_id':'q'}
        self.assertEqual(self.tick(),'waiting_other_redemption')
        self.assertEqual(self.calls.count('submit'),1)
    def test_pending_receipt(self):
        self.pending=True;self.assertEqual(self.tick(),'pending_receipt');self.assertEqual(self.tick(),'pending_receipt')
        self.assertEqual(self.calls.count('submit'),1);self.assertFalse(self.marks)
    def test_zero_payout_losing_token(self):
        self.paid=0;self.assertEqual(self.tick(),'reconciled');self.assertEqual(self.marks[0][1],0)
    def test_database_retry_no_new_submission(self):
        self.fail_db=True;self.tick();self.visible=110
        self.assertEqual(self.tick(),'waiting_database');self.fail_db=False
        self.assertEqual(self.tick(),'reconciled');self.assertEqual(self.calls.count('submit'),1)
    def test_changed_holdings_rejected(self):
        self.tick();self.pos['shares']=11
        with self.assertRaises(ValueError):self.tick()
    def test_partial_payout_no_nav_transition(self):
        self.tick();self.paid=5;self.visible=105
        with self.assertRaises(ValueError):self.tick()
        self.assertFalse(self.marks)
    def test_corrupt_journal_fails_closed(self):
        with open(C.journal_path(),'w') as f:f.write('{bad')
        with self.assertRaises(Exception):self.tick()
        self.assertFalse(self.calls)
    def test_submicro_shares_rejected(self):
        with self.assertRaises(ValueError):C.raw_shares('0.0000001')

class ReporterGuard(unittest.TestCase):
    def test_open_settled_and_pending_positions_block_nav(self):
        from arb_monitor.core import arb_reporter as R, arb_withdrawals as W
        from arb_monitor.core.arb_nav import ArbPosition
        p=ArbPosition('p','1','K',10,9,0,'open')
        with patch.object(C,'enabled',return_value=True), patch.object(W,'_load_ledger',return_value=[]):
            with patch.object(C,'kalshi_payout',return_value=0):
                self.assertIn('awaiting CTF',R._robinhood_report_guards({'positions':[p]}))
            with patch.object(C,'kalshi_payout',side_effect=TimeoutError):
                self.assertIn('unavailable',R._robinhood_report_guards({'positions':[p]}))
            with patch.object(C,'kalshi_payout',return_value=None):
                self.assertIsNone(R._robinhood_report_guards({'positions':[p]}))
            p.status='pending_redeem';p.resolved_venue='polymarket'
            self.assertIn('not reconciled',R._robinhood_report_guards({'positions':[p]}))

class KalshiEvidence(unittest.TestCase):
    def test_settlements_are_exact_and_fail_closed(self):
        from unittest.mock import Mock
        from arb_monitor.core import kalshi_auth
        position={'kalshi_ticker':'K','kalshi_side':'NO','shares':10}
        row={'ticker':'K','no_count_fp':'10.00','yes_count_fp':'0.00','market_result':'no','settled_time':'2026-10-23T00:00:00Z','revenue':1000}
        response=Mock();response.json.return_value={'settlements':[row]}
        with patch.object(kalshi_auth,'get_kalshi_headers',return_value={'test':'test'}), patch('requests.get',return_value=response):
            self.assertEqual(C.kalshi_payout(position),10000000)
            row['revenue']=500
            self.assertEqual(C.kalshi_payout(position),5000000)
            row['no_count_fp']='5.00'
            with self.assertRaises(ValueError):C.kalshi_payout(position)
            row['no_count_fp']='10.00';response.json.return_value={'settlements':[row],'cursor':'next'}
            with self.assertRaises(ValueError):C.kalshi_payout(position)
            response.json.return_value={'settlements':[]}
            self.assertIsNone(C.kalshi_payout(position))
            response.raise_for_status.side_effect=RuntimeError('HTTP failure')
            with self.assertRaises(RuntimeError):C.kalshi_payout(position)
