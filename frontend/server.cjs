const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { Pool } = require('pg');

const PORT = 3001;

// Admin authentication token (set via environment variable)
const ADMIN_TOKEN = process.env.PMFI_ADMIN_TOKEN || crypto.randomBytes(16).toString('hex');

// HMAC secret for code hashing (set via environment variable)
const HMAC_SECRET = process.env.PMFI_HMAC_SECRET || crypto.randomBytes(32).toString('hex');

// Database connection
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL?.includes('neon') ? { rejectUnauthorized: false } : false
});

// In-memory rate limiting
const rateLimitStore = new Map();
const RATE_LIMIT_WINDOW_MS = 60000; // 1 minute
const RATE_LIMIT_MAX_ATTEMPTS = 5;

// MIME types for static files
const mimeTypes = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml'
};

// Initialize database tables
async function initDatabase() {
  const client = await pool.connect();
  try {
    // Invite codes table (stores hashed codes)
    await client.query(`
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
    `);

    // Audit logs table
    await client.query(`
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
    `);

    // Whitelisted wallets (for quick access check)
    await client.query(`
      CREATE TABLE IF NOT EXISTS whitelisted_wallets (
        wallet_address VARCHAR(42) PRIMARY KEY,
        code_id INTEGER REFERENCES invite_codes(id),
        whitelisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    `);

    console.log('✅ Database tables initialized');
  } finally {
    client.release();
  }
}

// Hash a code using HMAC-SHA256 with server secret
function hashCode(code) {
  return crypto.createHmac('sha256', HMAC_SECRET).update(code.toLowerCase().trim()).digest('hex');
}

// Generate a random invite code
function generateCode() {
  return crypto.randomBytes(4).toString('hex').toUpperCase();
}

// Get client IP
function getClientIP(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim() || 
         req.socket?.remoteAddress || 
         'unknown';
}

// Verify admin authentication
function verifyAdmin(req) {
  const authHeader = req.headers['authorization'];
  if (!authHeader) return false;
  
  // Support "Bearer <token>" or just "<token>"
  const token = authHeader.startsWith('Bearer ') 
    ? authHeader.slice(7) 
    : authHeader;
  
  return token === ADMIN_TOKEN;
}

// Check rate limit
function checkRateLimit(ip) {
  const now = Date.now();
  const record = rateLimitStore.get(ip);
  
  if (!record) {
    rateLimitStore.set(ip, { count: 1, windowStart: now });
    return { allowed: true, remaining: RATE_LIMIT_MAX_ATTEMPTS - 1 };
  }
  
  // Reset window if expired
  if (now - record.windowStart > RATE_LIMIT_WINDOW_MS) {
    rateLimitStore.set(ip, { count: 1, windowStart: now });
    return { allowed: true, remaining: RATE_LIMIT_MAX_ATTEMPTS - 1 };
  }
  
  // Check if limit exceeded
  if (record.count >= RATE_LIMIT_MAX_ATTEMPTS) {
    return { allowed: false, remaining: 0 };
  }
  
  // Increment count
  record.count++;
  return { allowed: true, remaining: RATE_LIMIT_MAX_ATTEMPTS - record.count };
}

// Log audit event
async function logAudit(action, codeHash, walletAddress, ip, success, errorMessage = null) {
  try {
    await pool.query(
      `INSERT INTO invite_audit_logs (action, code_hash, wallet_address, ip_address, success, error_message)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [action, codeHash, walletAddress, ip, success, errorMessage]
    );
  } catch (err) {
    console.error('Audit log error:', err);
  }
}

// Parse JSON body
function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(new Error('Invalid JSON'));
      }
    });
    req.on('error', reject);
  });
}

// Send JSON response
function sendJSON(res, statusCode, data) {
  res.writeHead(statusCode, { 
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
  });
  res.end(JSON.stringify(data));
}

// API: Redeem invite code (atomic transaction)
async function handleRedeem(req, res) {
  const ip = getClientIP(req);
  
  // Rate limit check
  const rateLimit = checkRateLimit(ip);
  if (!rateLimit.allowed) {
    await logAudit('redeem_rate_limited', null, null, ip, false, 'Rate limit exceeded');
    return sendJSON(res, 429, { 
      error: 'Too many attempts. Please wait 1 minute.',
      retryAfter: 60
    });
  }

  let body;
  try {
    body = await parseBody(req);
  } catch (e) {
    return sendJSON(res, 400, { error: 'Invalid request body' });
  }

  const { code, walletAddress } = body;
  
  if (!code || typeof code !== 'string') {
    return sendJSON(res, 400, { error: 'Invite code is required' });
  }
  
  if (!walletAddress || !/^0x[a-fA-F0-9]{40}$/.test(walletAddress)) {
    return sendJSON(res, 400, { error: 'Valid wallet address is required' });
  }

  const codeHash = hashCode(code);
  const normalizedWallet = walletAddress.toLowerCase();

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Check if wallet already whitelisted
    const walletCheck = await client.query(
      'SELECT * FROM whitelisted_wallets WHERE wallet_address = $1',
      [normalizedWallet]
    );
    
    if (walletCheck.rows.length > 0) {
      await client.query('ROLLBACK');
      await logAudit('redeem_already_whitelisted', codeHash, normalizedWallet, ip, false, 'Wallet already whitelisted');
      return sendJSON(res, 200, { 
        success: true, 
        message: 'Wallet already has access',
        alreadyWhitelisted: true
      });
    }

    // Check code validity (with FOR UPDATE to lock the row)
    const codeCheck = await client.query(
      `SELECT * FROM invite_codes 
       WHERE code_hash = $1 
       FOR UPDATE`,
      [codeHash]
    );

    if (codeCheck.rows.length === 0) {
      await client.query('ROLLBACK');
      await logAudit('redeem_invalid_code', codeHash, normalizedWallet, ip, false, 'Code not found');
      return sendJSON(res, 400, { error: 'Invalid invite code' });
    }

    const inviteCode = codeCheck.rows[0];

    // Check if already used
    if (inviteCode.status === 'used' || inviteCode.redeemed_by) {
      await client.query('ROLLBACK');
      await logAudit('redeem_already_used', codeHash, normalizedWallet, ip, false, 'Code already used');
      return sendJSON(res, 400, { error: 'This code has already been used' });
    }

    // Check if revoked
    if (inviteCode.status === 'revoked') {
      await client.query('ROLLBACK');
      await logAudit('redeem_revoked', codeHash, normalizedWallet, ip, false, 'Code revoked');
      return sendJSON(res, 400, { error: 'This code has been revoked' });
    }

    // Check if expired
    if (inviteCode.expires_at && new Date(inviteCode.expires_at) < new Date()) {
      await client.query('ROLLBACK');
      await logAudit('redeem_expired', codeHash, normalizedWallet, ip, false, 'Code expired');
      return sendJSON(res, 400, { error: 'This code has expired' });
    }

    // Mark code as used
    await client.query(
      `UPDATE invite_codes 
       SET status = 'used', redeemed_by = $1, redeemed_at = CURRENT_TIMESTAMP
       WHERE id = $2`,
      [normalizedWallet, inviteCode.id]
    );

    // Add wallet to whitelist
    await client.query(
      `INSERT INTO whitelisted_wallets (wallet_address, code_id)
       VALUES ($1, $2)`,
      [normalizedWallet, inviteCode.id]
    );

    await client.query('COMMIT');
    await logAudit('redeem_success', codeHash, normalizedWallet, ip, true);

    return sendJSON(res, 200, { 
      success: true, 
      message: 'Access granted! Welcome to PMFI Beta.'
    });

  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Redeem error:', err);
    await logAudit('redeem_error', codeHash, normalizedWallet, ip, false, err.message);
    return sendJSON(res, 500, { error: 'Server error. Please try again.' });
  } finally {
    client.release();
  }
}

// API: Check if wallet has access
async function handleCheckAccess(req, res) {
  const urlParts = req.url.split('/');
  const walletAddress = urlParts[urlParts.length - 1];
  
  if (!walletAddress || !/^0x[a-fA-F0-9]{40}$/.test(walletAddress)) {
    return sendJSON(res, 400, { error: 'Valid wallet address required' });
  }

  const normalizedWallet = walletAddress.toLowerCase();

  try {
    const result = await pool.query(
      'SELECT * FROM whitelisted_wallets WHERE wallet_address = $1',
      [normalizedWallet]
    );
    
    return sendJSON(res, 200, { 
      hasAccess: result.rows.length > 0,
      whitelistedAt: result.rows[0]?.whitelisted_at || null
    });
  } catch (err) {
    console.error('Check access error:', err);
    return sendJSON(res, 500, { error: 'Server error' });
  }
}

// API: Admin - Generate codes
async function handleAdminGenerate(req, res) {
  // TODO: Add admin authentication
  let body;
  try {
    body = await parseBody(req);
  } catch (e) {
    return sendJSON(res, 400, { error: 'Invalid request body' });
  }

  const count = Math.min(body.count || 1, 100); // Max 100 at once
  const expiresAt = body.expiresAt || null;
  const note = body.note || null;
  const createdBy = body.createdBy || 'admin';

  const codes = [];
  const client = await pool.connect();
  
  try {
    await client.query('BEGIN');
    
    for (let i = 0; i < count; i++) {
      const code = generateCode();
      const codeHash = hashCode(code);
      
      await client.query(
        `INSERT INTO invite_codes (code_hash, status, expires_at, note, created_by)
         VALUES ($1, 'active', $2, $3, $4)`,
        [codeHash, expiresAt, note, createdBy]
      );
      
      codes.push(code);
    }
    
    await client.query('COMMIT');
    await logAudit('admin_generate', null, null, getClientIP(req), true, `Generated ${count} codes`);
    
    return sendJSON(res, 200, { 
      success: true, 
      codes,
      message: `Generated ${count} invite code(s)`
    });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Generate error:', err);
    return sendJSON(res, 500, { error: 'Failed to generate codes' });
  } finally {
    client.release();
  }
}

// API: Admin - List codes
async function handleAdminList(req, res) {
  try {
    const result = await pool.query(`
      SELECT 
        id,
        SUBSTRING(code_hash, 1, 8) as code_hash_prefix,
        status,
        created_at,
        created_by,
        expires_at,
        redeemed_by,
        redeemed_at,
        note
      FROM invite_codes
      ORDER BY created_at DESC
      LIMIT 500
    `);
    
    return sendJSON(res, 200, { codes: result.rows });
  } catch (err) {
    console.error('List error:', err);
    return sendJSON(res, 500, { error: 'Failed to list codes' });
  }
}

// API: Admin - Revoke code by ID
async function handleAdminRevoke(req, res) {
  let body;
  try {
    body = await parseBody(req);
  } catch (e) {
    return sendJSON(res, 400, { error: 'Invalid request body' });
  }

  const { id } = body;
  
  if (!id) {
    return sendJSON(res, 400, { error: 'Code ID required' });
  }

  try {
    const result = await pool.query(
      `UPDATE invite_codes SET status = 'revoked' WHERE id = $1 AND status = 'active' RETURNING id`,
      [id]
    );
    
    if (result.rows.length === 0) {
      return sendJSON(res, 404, { error: 'Code not found or already used/revoked' });
    }
    
    await logAudit('admin_revoke', null, null, getClientIP(req), true, `Revoked code ID ${id}`);
    return sendJSON(res, 200, { success: true, message: 'Code revoked' });
  } catch (err) {
    console.error('Revoke error:', err);
    return sendJSON(res, 500, { error: 'Failed to revoke code' });
  }
}

// API: Admin - Export codes as CSV
async function handleAdminExport(req, res) {
  try {
    const result = await pool.query(`
      SELECT 
        id,
        status,
        created_at,
        created_by,
        expires_at,
        redeemed_by,
        redeemed_at,
        note
      FROM invite_codes
      ORDER BY created_at DESC
    `);
    
    const csv = [
      'id,status,created_at,created_by,expires_at,redeemed_by,redeemed_at,note',
      ...result.rows.map(r => 
        `${r.id},${r.status},${r.created_at},${r.created_by || ''},${r.expires_at || ''},${r.redeemed_by || ''},${r.redeemed_at || ''},${r.note || ''}`
      )
    ].join('\n');
    
    res.writeHead(200, {
      'Content-Type': 'text/csv',
      'Content-Disposition': 'attachment; filename=invite_codes.csv'
    });
    res.end(csv);
  } catch (err) {
    console.error('Export error:', err);
    return sendJSON(res, 500, { error: 'Failed to export codes' });
  }
}

// API: Admin - Get stats
async function handleAdminStats(req, res) {
  try {
    const stats = await pool.query(`
      SELECT 
        COUNT(*) FILTER (WHERE status = 'active') as active_codes,
        COUNT(*) FILTER (WHERE status = 'used') as used_codes,
        COUNT(*) FILTER (WHERE status = 'revoked') as revoked_codes,
        COUNT(*) as total_codes
      FROM invite_codes
    `);
    
    const wallets = await pool.query('SELECT COUNT(*) as count FROM whitelisted_wallets');
    
    return sendJSON(res, 200, {
      ...stats.rows[0],
      whitelisted_wallets: wallets.rows[0].count
    });
  } catch (err) {
    console.error('Stats error:', err);
    return sendJSON(res, 500, { error: 'Failed to get stats' });
  }
}

// Main request handler
const server = http.createServer(async (req, res) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    });
    return res.end();
  }

  // API routes
  if (req.url.startsWith('/api/')) {
    // Public endpoints
    if (req.method === 'POST' && req.url === '/api/invite/redeem') {
      return handleRedeem(req, res);
    }
    if (req.method === 'GET' && req.url.startsWith('/api/invite/check/')) {
      return handleCheckAccess(req, res);
    }
    
    // Admin endpoints (require authentication)
    if (req.url.startsWith('/api/admin/')) {
      if (!verifyAdmin(req)) {
        return sendJSON(res, 401, { error: 'Unauthorized. Admin token required.' });
      }
      
      if (req.method === 'POST' && req.url === '/api/admin/codes/generate') {
        return handleAdminGenerate(req, res);
      }
      if (req.method === 'GET' && req.url === '/api/admin/codes') {
        return handleAdminList(req, res);
      }
      if (req.method === 'POST' && req.url === '/api/admin/codes/revoke') {
        return handleAdminRevoke(req, res);
      }
      if (req.method === 'GET' && req.url === '/api/admin/codes/export') {
        return handleAdminExport(req, res);
      }
      if (req.method === 'GET' && req.url === '/api/admin/stats') {
        return handleAdminStats(req, res);
      }
    }
    
    return sendJSON(res, 404, { error: 'API endpoint not found' });
  }

  // Static file serving
  let urlPath = req.url.split('?')[0]; // Remove query string
  urlPath = urlPath === '/' ? 'index.html' : urlPath.replace(/^\//, '');
  
  // Serve admin.html for /admin route
  if (urlPath === 'admin') {
    urlPath = 'admin.html';
  }
  
  const filePath = path.join(__dirname, urlPath);
  const ext = path.extname(filePath).toLowerCase();
  const contentType = mimeTypes[ext] || 'application/octet-stream';

  fs.readFile(filePath, (err, content) => {
    if (err) {
      if (err.code === 'ENOENT') {
        res.writeHead(404);
        res.end('File not found');
      } else {
        res.writeHead(500);
        res.end('Server error');
      }
    } else {
      res.writeHead(200, { 'Content-Type': contentType });
      res.end(content);
    }
  });
});

// Start server
async function start() {
  await initDatabase();
  server.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀 PMFI Server running at http://0.0.0.0:${PORT}`);
    console.log(`   API: /api/invite/redeem, /api/invite/check/:wallet`);
    console.log(`   Admin: /api/admin/codes, /api/admin/codes/generate`);
    
    // Print admin token if auto-generated
    if (!process.env.PMFI_ADMIN_TOKEN) {
      console.log(`\n⚠️  Admin token (auto-generated): ${ADMIN_TOKEN}`);
      console.log(`   Set PMFI_ADMIN_TOKEN env var for production!`);
    } else {
      console.log(`   Admin auth: using PMFI_ADMIN_TOKEN from env`);
    }
    
    if (!process.env.PMFI_HMAC_SECRET) {
      console.log(`⚠️  HMAC secret auto-generated. Set PMFI_HMAC_SECRET for production!`);
    }
  });
}

start().catch(err => {
  console.error('Failed to start server:', err);
  process.exit(1);
});
