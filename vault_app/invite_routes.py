"""
Invite Code Routes for pSNIPER Vault Flask App
Add these routes to your existing Flask app
"""

import os
import hmac
import hashlib
import secrets
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor

invite_bp = Blueprint('invite', __name__)

DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_TOKEN = os.getenv('PMFI_ADMIN_TOKEN', secrets.token_hex(16))
HMAC_SECRET = os.getenv('PMFI_HMAC_SECRET', secrets.token_hex(32))

rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 5

def get_db():
    return psycopg2.connect(DATABASE_URL)

def hash_code(code):
    return hmac.new(
        HMAC_SECRET.encode(),
        code.lower().strip().encode(),
        hashlib.sha256
    ).hexdigest()

def generate_code():
    return secrets.token_hex(4).upper()

def check_rate_limit(ip):
    now = datetime.now().timestamp()
    if ip in rate_limit_store:
        attempts, window_start = rate_limit_store[ip]
        if now - window_start > RATE_LIMIT_WINDOW:
            rate_limit_store[ip] = (1, now)
            return True
        if attempts >= RATE_LIMIT_MAX:
            return False
        rate_limit_store[ip] = (attempts + 1, window_start)
    else:
        rate_limit_store[ip] = (1, now)
    return True

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth[7:] != ADMIN_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def log_action(action, code_hash=None, wallet=None, ip=None, success=True, error=None):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO invite_audit_logs (action, code_hash, wallet_address, ip_address, success, error_message)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (action, code_hash, wallet, ip, success, error))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to log action: {e}")

@invite_bp.route('/api/invite/check-wallet', methods=['POST'])
def check_wallet():
    data = request.get_json() or {}
    wallet = data.get('wallet', '').lower()
    
    if not wallet or len(wallet) != 42:
        return jsonify({'error': 'Invalid wallet address'}), 400
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        return jsonify({'whitelisted': result is not None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/invite/validate', methods=['POST'])
def validate_code():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if not check_rate_limit(ip):
        log_action('validate', ip=ip, success=False, error='Rate limited')
        return jsonify({'error': 'Too many attempts. Please wait.'}), 429
    
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    
    if not code or len(code) != 8:
        return jsonify({'valid': False, 'error': 'Invalid code format'}), 400
    
    code_hash = hash_code(code)
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, status, expires_at, redeemed_by 
            FROM invite_codes 
            WHERE code_hash = %s
        """, (code_hash,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if not result:
            log_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code not found')
            return jsonify({'valid': False, 'error': 'Invalid code'})
        
        if result['status'] != 'active':
            log_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code already used')
            return jsonify({'valid': False, 'error': 'Code already used'})
        
        if result['expires_at'] and result['expires_at'] < datetime.now():
            log_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code expired')
            return jsonify({'valid': False, 'error': 'Code expired'})
        
        log_action('validate', code_hash=code_hash[:16], ip=ip, success=True)
        return jsonify({'valid': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/invite/redeem', methods=['POST'])
def redeem_code():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if not check_rate_limit(ip):
        return jsonify({'error': 'Too many attempts. Please wait.'}), 429
    
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    wallet = data.get('wallet', '').lower()
    
    if not code or len(code) != 8:
        return jsonify({'success': False, 'error': 'Invalid code format'}), 400
    
    if not wallet or len(wallet) != 42:
        return jsonify({'success': False, 'error': 'Invalid wallet address'}), 400
    
    code_hash = hash_code(code)
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'success': True, 'message': 'Wallet already whitelisted'})
        
        cur.execute("""
            SELECT id, status, expires_at 
            FROM invite_codes 
            WHERE code_hash = %s
        """, (code_hash,))
        result = cur.fetchone()
        
        if not result:
            log_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=False, error='Code not found')
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid code'})
        
        if result['status'] != 'active':
            log_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=False, error='Code already used')
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Code already used'})
        
        if result['expires_at'] and result['expires_at'] < datetime.now():
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Code expired'})
        
        cur.execute("""
            UPDATE invite_codes 
            SET status = 'redeemed', redeemed_by = %s, redeemed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (wallet, result['id']))
        
        cur.execute("""
            INSERT INTO whitelisted_wallets (wallet_address, code_id)
            VALUES (%s, %s)
            ON CONFLICT (wallet_address) DO NOTHING
        """, (wallet, result['id']))
        
        conn.commit()
        log_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=True)
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Access granted!'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/admin/codes', methods=['GET'])
@admin_required
def list_codes():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, status, created_at, expires_at, redeemed_by, redeemed_at, note,
                   SUBSTRING(code_hash, 1, 8) as code_prefix
            FROM invite_codes 
            ORDER BY created_at DESC 
            LIMIT 100
        """)
        codes = cur.fetchall()
        cur.close()
        conn.close()
        
        for code in codes:
            if code['created_at']:
                code['created_at'] = code['created_at'].isoformat()
            if code['expires_at']:
                code['expires_at'] = code['expires_at'].isoformat()
            if code['redeemed_at']:
                code['redeemed_at'] = code['redeemed_at'].isoformat()
        
        return jsonify({'codes': codes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/admin/codes', methods=['POST'])
@admin_required
def create_code():
    data = request.get_json() or {}
    count = min(int(data.get('count', 1)), 50)
    note = data.get('note', '')
    
    codes = []
    try:
        conn = get_db()
        cur = conn.cursor()
        
        for _ in range(count):
            code = generate_code()
            code_hash = hash_code(code)
            cur.execute("""
                INSERT INTO invite_codes (code_hash, note, created_by)
                VALUES (%s, %s, 'admin')
            """, (code_hash, note))
            codes.append(code)
        
        conn.commit()
        cur.close()
        conn.close()
        
        log_action('create_codes', ip=request.remote_addr, success=True)
        return jsonify({'codes': codes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/admin/codes/<int:code_id>', methods=['DELETE'])
@admin_required
def revoke_code(code_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE invite_codes SET status = 'revoked' WHERE id = %s", (code_id,))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@invite_bp.route('/api/admin/wallets', methods=['GET'])
@admin_required
def list_wallets():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT wallet_address, whitelisted_at 
            FROM whitelisted_wallets 
            ORDER BY whitelisted_at DESC
        """)
        wallets = cur.fetchall()
        cur.close()
        conn.close()
        
        for w in wallets:
            if w['whitelisted_at']:
                w['whitelisted_at'] = w['whitelisted_at'].isoformat()
        
        return jsonify({'wallets': wallets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
