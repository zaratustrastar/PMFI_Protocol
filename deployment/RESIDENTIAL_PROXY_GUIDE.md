# 🌐 Residential Proxy Setup Guide

This guide explains how to configure residential proxies for Polymarket trading to bypass Cloudflare protection.

---

## ⚠️ Why You Need Residential Proxy

**Polymarket's API blocks datacenter IPs** (like those from DigitalOcean, AWS, Vultr, etc.) using Cloudflare protection.

**What gets blocked:**
- Order placement (`POST /orders`)
- Order status checks (`GET /orders`)
- Fill monitoring

**What works without proxy:**
- Market data fetching (uses different API)

**Solution**: Use residential IP addresses that appear as real home internet connections.

---

## 🏠 Option 1: VPS with Residential IP (Easiest)

Some VPS providers offer residential IP addresses directly.

### Recommended Providers

**TradingVPS.io** (Germany Location)
- Price: ~$30-50/month
- Features: Ultra-low latency, trading-optimized
- Setup: Select "Residential IP" option when ordering
- No additional proxy configuration needed

**TradingFXVPS**
- Price: ~$25-40/month
- Features: Rotating residential IPs included
- Best for: Forex/crypto trading bots

### Verification
```bash
# SSH into VPS and check IP type
curl https://ipinfo.io

# Look for "org" field - should NOT say "Digital Ocean" or "AWS"
# Should show ISP name like "Comcast" or "Deutsche Telekom"
```

---

## 🔌 Option 2: Datacenter VPS + Residential Proxy Service

Use any cheap VPS ($5-10/month) + subscribe to residential proxy service.

### Top Residential Proxy Providers

#### 1. **Bright Data** (Premium, Most Reliable)
- **Price**: $500 minimum ($0.60-1.20 per GB)
- **Features**:
  - 72M+ residential IPs
  - Auto IP rotation
  - 99.99% uptime
  - Country/city targeting
- **Best for**: Production trading bots
- **Setup**:
  ```bash
  # Get credentials from dashboard
  HTTP_PROXY=http://customer-USERNAME-cc-us:PASSWORD@brd.superproxy.io:22225
  HTTPS_PROXY=http://customer-USERNAME-cc-us:PASSWORD@brd.superproxy.io:22225
  ```

#### 2. **IPRoyal** (Mid-Range, Good Balance)
- **Price**: From $7/month (1GB) to $80/month (unlimited)
- **Features**:
  - Rotating residential IPs
  - US/EU locations
  - CAPTCHA bypass support
- **Best for**: Small to medium trading operations
- **Setup**:
  ```bash
  HTTP_PROXY=http://USERNAME:PASSWORD@geo.iproyal.com:12321
  HTTPS_PROXY=http://USERNAME:PASSWORD@geo.iproyal.com:12321
  ```

#### 3. **Oxylabs** (Enterprise)
- **Price**: Custom pricing ($$$$)
- **Features**:
  - 100M+ IPs
  - Premium support
  - Advanced targeting
- **Best for**: Large-scale operations

#### 4. **NordVPN** (Budget Option)
- **Price**: $3-12/month
- **Features**:
  - Limited residential IPs via "Meshnet" feature
  - SOCKS5 proxy support
  - Good for testing
- **Limitations**: Not designed for automation, may have rate limits
- **Setup**:
  ```bash
  # Note: NordVPN uses SOCKS5, may need adapter
  HTTP_PROXY=socks5://USERNAME:PASSWORD@residential.nordvpn.com:1080
  HTTPS_PROXY=socks5://USERNAME:PASSWORD@residential.nordvpn.com:1080
  ```

#### 5. **Smartproxy**
- **Price**: From $50/month (2GB)
- **Features**:
  - 40M+ residential IPs
  - Rotating proxies
  - Good documentation
- **Setup**:
  ```bash
  HTTP_PROXY=http://USERNAME:PASSWORD@gate.smartproxy.com:7000
  HTTPS_PROXY=http://USERNAME:PASSWORD@gate.smartproxy.com:7000
  ```

---

## ⚙️ Configuration

### Method 1: Environment Variables (Recommended)

Add to `/opt/polymarket-bot/.env`:
```bash
HTTP_PROXY=http://username:password@proxy-host:port
HTTPS_PROXY=http://username:password@proxy-host:port
```

The Python `curl_cffi` library will automatically use these proxies.

### Method 2: System-Wide (All Processes)

Edit `/etc/environment`:
```bash
http_proxy="http://username:password@proxy.com:port"
https_proxy="http://username:password@proxy.com:port"
no_proxy="localhost,127.0.0.1"
```

Reload:
```bash
source /etc/environment
```

### Method 3: Code-Level (Advanced)

Edit `trading_bot/polymarket_trader.py` to add proxy parameter:
```python
# If you need custom proxy rotation logic
def _make_request(self, method, url, **kwargs):
    proxies = {
        'http': 'http://user:pass@proxy.com:port',
        'https': 'http://user:pass@proxy.com:port'
    }
    kwargs['proxies'] = proxies
    return self.session.request(method, url, **kwargs)
```

---

## 🧪 Testing Your Proxy

### Test 1: Basic Connectivity
```bash
# Test proxy connection
curl --proxy http://username:password@proxy.com:port https://ipinfo.io

# Should show residential IP info
```

### Test 2: Polymarket API Access
```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate

python3 << EOF
import os
os.environ['HTTP_PROXY'] = 'http://username:password@proxy.com:port'
os.environ['HTTPS_PROXY'] = 'http://username:password@proxy.com:port'

from polymarket_trader import PolymarketTrader
trader = PolymarketTrader()

# Try to fetch markets
import requests
response = requests.get('https://clob.polymarket.com/markets')
print(f"Status: {response.status_code}")
print("✅ Proxy working!" if response.status_code == 200 else "❌ Proxy failed")
EOF
```

### Test 3: Order Placement (Actual Test)
```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate

# Run worker briefly to see if orders go through
python3 auto_trader.py
# Press Ctrl+C after 30 seconds
# Check logs for "Order placed successfully" or "403 Forbidden"
```

---

## 🔍 Troubleshooting

### Issue: 403 Forbidden Errors
**Cause**: Proxy IP still detected as datacenter or bot

**Solutions**:
1. Try different proxy provider
2. Enable IP rotation (most services offer this)
3. Add request delays (already implemented in code)
4. Use residential proxy with higher reputation score

### Issue: Proxy Connection Timeout
**Cause**: Proxy credentials wrong or proxy down

**Solutions**:
```bash
# Test proxy directly
curl -v --proxy http://user:pass@proxy.com:port https://google.com

# Check credentials
echo $HTTP_PROXY
cat /opt/polymarket-bot/.env | grep PROXY
```

### Issue: Slow Response Times
**Cause**: Proxy server overloaded or far from target

**Solutions**:
1. Choose proxy location near Polymarket servers (US/EU)
2. Upgrade to premium proxy tier
3. Use dedicated residential IPs (more expensive but faster)

### Issue: Orders Still Not Placing
**Check**:
```bash
# View worker logs
journalctl -u polymarket-worker.service -n 50

# Look for specific error messages:
# - "403 Forbidden" = Proxy not working
# - "Connection refused" = Proxy credentials wrong
# - "Timeout" = Proxy too slow or down
# - "Insufficient balance" = Need more USDC
```

---

## 💰 Cost Comparison

| Provider | Monthly Cost | Best Use Case | Reliability |
|----------|-------------|---------------|-------------|
| **Residential VPS** | $30-50 | Set-it-forget-it | ⭐⭐⭐⭐⭐ |
| **Bright Data** | $500+ (pay-per-GB) | Production, high volume | ⭐⭐⭐⭐⭐ |
| **IPRoyal** | $7-80 | Small to medium bots | ⭐⭐⭐⭐ |
| **Smartproxy** | $50-200 | Medium trading | ⭐⭐⭐⭐ |
| **NordVPN** | $3-12 | Testing, hobby projects | ⭐⭐⭐ |

---

## 📊 Recommendation by Use Case

### Hobby / Testing
- **Best**: NordVPN ($3-12/month) + cheap VPS ($5)
- **Total**: ~$10-15/month
- **Pros**: Very cheap
- **Cons**: May have reliability issues

### Serious Trading
- **Best**: IPRoyal Unlimited ($80/month) + VPS ($10)
- **Total**: ~$90/month
- **Pros**: Good balance of cost and reliability
- **Cons**: Need to monitor for blocks

### Production / High Volume
- **Best**: Residential VPS from TradingVPS.io ($40)
- **Alternative**: Bright Data ($500+) + cheap VPS ($10)
- **Total**: $40-510/month
- **Pros**: Maximum reliability, no proxy management
- **Cons**: Higher cost

### Quick Start (Easiest)
- **Best**: TradingVPS.io with residential IP ($30-40)
- **Pros**: Zero configuration, works immediately
- **Cons**: Slightly more expensive than DIY

---

## 🎯 Quick Setup Commands

### For Bright Data
```bash
echo 'HTTP_PROXY=http://customer-YOUR_USERNAME-cc-us:YOUR_PASSWORD@brd.superproxy.io:22225' >> /opt/polymarket-bot/.env
echo 'HTTPS_PROXY=http://customer-YOUR_USERNAME-cc-us:YOUR_PASSWORD@brd.superproxy.io:22225' >> /opt/polymarket-bot/.env
```

### For IPRoyal
```bash
echo 'HTTP_PROXY=http://YOUR_USERNAME:YOUR_PASSWORD@geo.iproyal.com:12321' >> /opt/polymarket-bot/.env
echo 'HTTPS_PROXY=http://YOUR_USERNAME:YOUR_PASSWORD@geo.iproyal.com:12321' >> /opt/polymarket-bot/.env
```

### Test
```bash
export $(cat /opt/polymarket-bot/.env | grep PROXY)
curl --proxy $HTTP_PROXY https://ipinfo.io
```

---

## ✅ Verification Checklist

After setup, verify:

- [ ] Proxy environment variables set in `.env`
- [ ] `curl --proxy` command returns residential IP
- [ ] Worker service starts without errors
- [ ] Monitor service starts without errors
- [ ] Test order placement succeeds (check logs)
- [ ] No 403 Forbidden errors in logs
- [ ] Telegram notifications arriving

---

**Need help choosing a proxy provider? Consider:**
- Budget: How much can you spend monthly?
- Volume: How many markets will you trade?
- Reliability needs: Hobby vs. production?

Reply with your budget and I can recommend the best option!
