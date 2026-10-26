"""MILORD Safe/Polymarket recovery: pUSD redemption of CTF, neg-risk CTF and Protocol V2 positions.
Explicit per-pair system and condition mapping; disabled by default.

No price-based resolution. A sending intent is durable BEFORE submission. Unknown
submission outcomes never retry automatically. A confirmed payout is not a complete
pair settlement until both venue evidence and the cash observed by NAV agree.
"""
import json
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from . import arb_ledger as ledger


def enabled():
    from ..config import is_robinhood_funding
    return is_robinhood_funding() and os.environ.get('ARB_CTF_ENABLED') == 'true'


def journal_path():
    return os.environ.get('ARB_CTF_JOURNAL', str(Path(ledger.intents_path()).with_name('arb_ctf_redemptions.json')))


def nav_lock():
    return ledger._locked(journal_path() + '.nav', exclusive=True)


def transport(action, **data):
    from .shadow_guard import assert_not_shadow
    assert_not_shadow(f'CTF redemption transport {action}')
    script = Path(__file__).resolve().parents[3] / 'trading_bot' / 'ctf_redemption.mjs'
    result = subprocess.run(['node', str(script)], input=json.dumps({'action': action, **data}),
                            capture_output=True, text=True, timeout=180, check=True)
    # Relayer SDK may print diagnostics before the final JSON record.
    return json.loads(result.stdout.strip().splitlines()[-1])


def raw_shares(value):
    d = Decimal(str(value)) * 1000000
    if not d.is_finite() or d <= 0 or d != d.to_integral_value():
        raise ValueError('shares must be exact positive six-decimal units')
    return int(d)


SYSTEMS = ('ctf', 'ctf_negrisk', 'v2')


def mapping(position):
    """Per-pair position system and condition. Declared, never guessed; the transport re-derives the ids on chain.

    MILORD_CTF_MARKETS_FILE: {"<pair_id>": {"system": "ctf" | "ctf_negrisk" | "v2", "conditionId": "0x..."}}
    ctf/ctf_negrisk take the 32-byte CTF condition id; v2 takes the 31-byte Protocol V2 condition id.
    """
    path = os.environ.get('MILORD_CTF_MARKETS_FILE')
    if not path:
        raise ValueError('MILORD_CTF_MARKETS_FILE required; position system and condition must not be guessed')
    data = json.loads(Path(path).read_text())[position['pair_id']]
    if data.get('system') not in SYSTEMS:
        raise ValueError('position system must be declared as ctf, ctf_negrisk or v2')
    side = 'YES' if position['kalshi_side'].upper() == 'NO' else 'NO'
    if position.get('poly_side', side).upper() != side:
        raise ValueError('inconsistent tracked sides')
    return dict(system=data['system'], conditionId=data['conditionId'], outcome=0 if side == 'YES' else 1,
                yesTokenId=str(position['poly_yes_token']), sharesRaw=str(raw_shares(position['shares'])))


def kalshi_payout(position):
    """Read-only settlement evidence. Reject shared/partial/ambiguous market holdings."""
    from ..config import KALSHI_BASE_URL
    from .kalshi_auth import get_kalshi_headers
    import requests
    url = KALSHI_BASE_URL.rstrip('/') + '/portfolio/settlements'
    headers = get_kalshi_headers('GET', url)
    if not headers:
        raise ValueError('Kalshi settlement credentials unavailable')
    response = requests.get(url, headers=headers, params={'ticker': position['kalshi_ticker'], 'limit': 100}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get('cursor'):
        raise ValueError('ambiguous paginated settlements; reconcile explicitly')
    rows = [r for r in data['settlements'] if r['ticker'] == position['kalshi_ticker']]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('multiple settlement records for pair market')
    r = rows[0]
    side = position['kalshi_side'].lower()
    other = 'no' if side == 'yes' else 'yes'
    count = Decimal(str(r.get(side + '_count_fp', r.get(side + '_count'))))
    other_count = Decimal(str(r.get(other + '_count_fp', r.get(other + '_count'))))
    if count != Decimal(str(position['shares'])) or other_count != 0:
        raise ValueError('Kalshi settlement holdings do not match isolated pair')
    if not r.get('settled_time') or r.get('market_result') not in ('yes', 'no', 'scalar', 'void'):
        raise ValueError('incomplete Kalshi settlement')
    revenue = Decimal(str(r['revenue'])) * 10000  # cents -> raw six-decimal cash
    if not revenue.is_finite() or revenue < 0 or revenue > raw_shares(position['shares']) or revenue != revenue.to_integral_value():
        raise ValueError('invalid Kalshi revenue')
    return int(revenue)


def tick(position, *, send=transport, kalshi=kalshi_payout, cash=None, mark=None, prepare_mapping=mapping):
    from .arb_positions_db import mark_position_redeemed, mark_position_resolved
    from .arb_nav import _get_poly_cash_raw
    cash = cash or (lambda: _get_poly_cash_raw(require_configured=True))
    mark = mark or mark_position_redeemed
    pair = position['pair_id']
    with nav_lock():
        rows = ledger.read_rows(journal_path(), strict=True)
        row = next((r for r in rows if r['pair_id'] == pair), None)
        fingerprint = [str(position['shares']), str(position['cost_basis_usdc']), position['poly_yes_token'], position['kalshi_ticker']]
        if row and row['fingerprint'] != fingerprint:
            raise ValueError('pair reused or holdings changed; explicit reconciliation required')
        if row and row['status'] == 'reconciled':
            return 'reconciled'
        if row is None:
            # Receipt cash deltas are Safe-wide. Keep one recovery in flight so
            # another redemption cannot invalidate the first payout's snapshot.
            if any(r['status'] != 'reconciled' for r in rows):
                return 'waiting_other_redemption'
            k = kalshi(position)
            if k is None:
                return 'waiting_kalshi'
            plan = send('prepare', **prepare_mapping(position))
            if plan.get('pending'):
                return 'waiting_ctf_resolution'
            if not plan.get('approved'):
                return 'approval_required'  # one-time Safe approval of the adapter/Router; never bundled here
            if int(plan['cashBefore']) != cash():
                raise ValueError('CTF collateral does not match NAV venue cash')
            # Pending status must be durable before funds can move.
            if position['status'] == 'open' and not mark_position_resolved(pair, 'polymarket', int(plan['payoutRaw']) / 1e6):
                raise ValueError('cannot persist pending redemption')
            row = dict(pair_id=pair, fingerprint=fingerprint, status='prepared', plan=plan, kalshi_raw=k)
            ledger.append_row(journal_path(), row)
        def save(**updates):
            row.update(updates)
            ledger.update_rows(journal_path(), lambda rs: next(r for r in rs if r['pair_id'] == pair).update(updates))
        if row['status'] == 'prepared':
            if os.environ.get('ARB_CTF_ROUTE_VERIFIED') != 'true':
                return 'route_unverified'
            save(status='sending')
            result = send('submit', plan=row['plan'])
            tx = result['txHash']
            if not isinstance(tx,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', tx):
                raise ValueError('invalid submission hash; reconcile before retry')
            save(status='submitted', tx_hash=tx)
        if row['status'] == 'sending':
            return 'unknown_submission'  # crash or timeout: never resend automatically
        result = send('observe', plan=row['plan'], txHash=row['tx_hash'])
        if result.get('pending'):
            return 'pending_receipt'
        if int(result['payoutRaw']) != int(row['plan']['payoutRaw']):
            raise ValueError('partial or mismatched payout; no NAV transition')
        if cash() != int(result['cashRaw']):
            return 'waiting_cash_visibility'
        total = int(result['payoutRaw']) + row['kalshi_raw']
        if not mark(pair, Decimal(total) / Decimal(1000000)):
            return 'waiting_database'  # receipt persisted, no second submit on retry
        save(status='reconciled', payout_raw=total)
        return 'reconciled'
