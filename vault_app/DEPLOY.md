# Deploy pSNIPER Vault with Invite Gate

## Quick Deploy to VPS

### 1. Copy files to VPS

```bash
# On VPS, create directory
mkdir -p /opt/vault-app

# Copy these files from Replit to VPS:
# - vault_app/app.py
# - vault_app/invite_routes.py
# - vault_app/requirements.txt
# - vault_app/templates/index.html
# - vault_app/templates/vault.html
# - vault_app/templates/admin.html
# - vault_app/static/logo.png (your logo)
```

### 2. Install dependencies

```bash
cd /opt/vault-app
pip install -r requirements.txt
```

### 3. Set environment variables

Add to your `.env.systemd` or export:

```bash
export DATABASE_URL="postgresql://..."
export PMFI_ADMIN_TOKEN="your-secret-admin-token-here"
export PMFI_HMAC_SECRET="a-random-32-char-secret-for-hashing"
```

Generate secure secrets:
```bash
python3 -c "import secrets; print('PMFI_ADMIN_TOKEN=' + secrets.token_hex(16))"
python3 -c "import secrets; print('PMFI_HMAC_SECRET=' + secrets.token_hex(32))"
```

### 4. Run the app

Test manually:
```bash
cd /opt/vault-app
python app.py
```

### 5. Create systemd service

```bash
sudo tee /etc/systemd/system/pmfi-vault.service << 'EOF'
[Unit]
Description=PMFI Vault App with Invite Gate
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vault-app
EnvironmentFile=/opt/polymarket-bot/.env.systemd
ExecStart=/usr/bin/python3 /opt/vault-app/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pmfi-vault
sudo systemctl start pmfi-vault
```

### 6. Check status

```bash
systemctl status pmfi-vault
journalctl -u pmfi-vault -f
```

## Usage

- **Main app**: http://your-vps-ip:8080/
- **Admin panel**: http://your-vps-ip:8080/admin
- **Request access**: Links to your Tally form

## Admin Panel

1. Go to `/admin`
2. Enter your `PMFI_ADMIN_TOKEN`
3. Generate invite codes
4. View whitelisted wallets
5. Revoke codes if needed

## Integrating with Existing Vault

The `/vault` route currently shows a placeholder. To integrate your existing vault:

**Option A: Replace vault.html**
Copy your existing vault dashboard HTML into `templates/vault.html`

**Option B: Reverse proxy**
In `app.py`, change the `/vault` route to proxy to your existing vault app:
```python
@app.route('/vault')
def vault():
    return redirect('http://localhost:8081/')  # Your existing vault port
```

**Option C: Run invite gate as a separate frontend**
Run the invite gate on port 8080, existing vault on 8081, and use nginx to route between them.
