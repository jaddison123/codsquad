/*
  server.js – tiny web server for the fishery site
  ------------------------------------------------
  It does two jobs:
    1. Serves the website files (index.html, styles.css, images/).
    2. Forwards map data requests (/api/...) to Global Fishing Watch and adds your
       API token on the way. The token stays on the server and never reaches the browser.

  Requirements: Node.js 18 or newer. No packages to install.

  Run (Mac/Linux):    GFW_TOKEN=your_token_here node server.js
  Run (Windows PS):   $env:GFW_TOKEN="your_token_here"; node server.js
  Then open:          http://localhost:3000
*/

const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const PORT = process.env.PORT || 3000;
const TOKEN = process.env.GFW_TOKEN;                         // your Global Fishing Watch API access token
const API_BASE = 'https://gateway.api.globalfishingwatch.org';
const ALLOWED_API_PREFIX = '/v3/4wings/';                    // only map-data requests may be forwarded
const ROOT = __dirname;

if (!TOKEN) {
  console.warn('Warning: GFW_TOKEN is not set. The site will load but the map data will be refused.');
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon'
};

// ---- Forward /api/... to Global Fishing Watch ---------------------
async function proxy(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const apiPath = url.pathname.slice('/api'.length);         // "/api/v3/..." -> "/v3/..."

  // Marks every reply from this proxy, so the page can tell it apart from some other server
  res.setHeader('X-Via', 'fishery-proxy');

  // Only allow GET requests to the 4Wings map endpoints (this is not an open relay)
  if (req.method !== 'GET' || !apiPath.startsWith(ALLOWED_API_PREFIX)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    return res.end('Not allowed');
  }

  if (!TOKEN) {
    res.writeHead(401, { 'Content-Type': 'text/plain' });
    return res.end('No GFW_TOKEN set on the server');
  }

  try {
    // url.search keeps the query string exactly as the browser sent it
    const upstream = await fetch(API_BASE + apiPath + url.search, {
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    let body = Buffer.from(await upstream.arrayBuffer());

    // Some responses arrive still gzipped; unpack them so the browser can read the tile
    if (body.length > 2 && body[0] === 0x1f && body[1] === 0x8b) body = zlib.gunzipSync(body);

    // Show failures in the terminal to make problems easy to diagnose
    if (!upstream.ok) console.error('Global Fishing Watch said', upstream.status, body.toString('utf8', 0, 300));

    res.writeHead(upstream.status, {
      'Content-Type': upstream.headers.get('content-type') || 'application/octet-stream',
      // Let browsers reuse tiles for a few minutes so panning stays quick
      'Cache-Control': upstream.ok ? 'public, max-age=300' : 'no-store'
    });
    res.end(body);
  } catch (err) {
    console.error('Proxy error:', err.message);
    res.writeHead(502, { 'Content-Type': 'text/plain' });
    res.end('Could not reach Global Fishing Watch');
  }
}

// ---- Serve the website files -----------------------------------------
function serveStatic(req, res) {
  let pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  if (pathname === '/') pathname = '/index.html';

  // Only these files are public: the page, the stylesheet and the images folder
  const isPublic = pathname === '/index.html' || pathname === '/styles.css' || pathname.startsWith('/images/');
  const filePath = path.normalize(path.join(ROOT, pathname));

  // Block anything that tries to escape the site folder (e.g. "/images/../server.js")
  if (!isPublic || !filePath.startsWith(ROOT + path.sep)) {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    return res.end('Not found');
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('Not found');
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream' });
    res.end(data);
  });
}

http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) return proxy(req, res);
  serveStatic(req, res);
}).listen(PORT, () => console.log('Site running at http://localhost:' + PORT));
