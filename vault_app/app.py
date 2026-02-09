"""
pSNIPER Vault Flask App with Invite Code Gate
Run: python app.py
"""

import os
from flask import Flask, render_template, send_from_directory, redirect, request
from flask_cors import CORS
from invite_routes import invite_bp
import psycopg2

app = Flask(__name__, 
            template_folder='templates',
            static_folder='static')
CORS(app)

# Register invite code routes
app.register_blueprint(invite_bp)

# Database initialization
DATABASE_URL = os.getenv('DATABASE_URL')

def init_database():
    """Create required tables if they don't exist"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id SERIAL PRIMARY KEY,
                code_hash VARCHAR(64) NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by VARCHAR(255),
                expires_at TIMESTAMP,
                redeemed_by VARCHAR(42),
                redeemed_at TIMESTAMP,
                note TEXT
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_audit_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(50) NOT NULL,
                code_hash VARCHAR(64),
                wallet_address VARCHAR(42),
                ip_address VARCHAR(45),
                success BOOLEAN NOT NULL,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whitelisted_wallets (
                wallet_address VARCHAR(42) PRIMARY KEY,
                code_id INTEGER REFERENCES invite_codes(id),
                whitelisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database tables initialized")
    except Exception as e:
        print(f"❌ Database init error: {e}")

# Check if wallet is whitelisted
def is_wallet_whitelisted(wallet):
    if not wallet:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet.lower(),))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except:
        return False

@app.route('/')
def index():
    """Show invite gate"""
    return render_template('index.html')

@app.route('/vault')
def vault():
    """
    Protected vault route - your existing vault dashboard
    For now, redirect to the original vault app running on a different port
    Or render your vault template here
    """
    # Option 1: Redirect to existing vault app
    # return redirect('http://localhost:8081/')
    
    # Option 2: Render vault template with config from env
    return render_template('vault.html', 
        vault_address=os.getenv('VAULT_V7_ADDRESS', '0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1')
    )

@app.route('/admin')
def admin():
    """Admin panel for managing invite codes"""
    return render_template('admin.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

if __name__ == '__main__':
    init_database()
    
    # Print admin token for first-time setup
    admin_token = os.getenv('PMFI_ADMIN_TOKEN')
    if admin_token:
        print(f"\n🔐 Admin Token: {admin_token[:8]}...")
    else:
        print("\n⚠️  No PMFI_ADMIN_TOKEN set - using random token (check logs)")
    
    print("\n🚀 Starting pSNIPER Vault on http://0.0.0.0:8080")
    app.run(host='0.0.0.0', port=8080, debug=False)
