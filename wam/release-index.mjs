// Compatibility with released WAM images: their normal index lookup must see the
// release commit, not replace an already prepared recipe with today's catalog.
// No installs, credentials, writable mounts, host ports or Docker socket here.
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';

const manifest = JSON.parse(readFileSync(process.env.WIZARD_RELEASE_PINS || '/release-pins.json', 'utf8'));
const pins = new Map();
for (const row of Object.values(manifest.default_apps || {})) {
  if (!row || (!row.repo && !row.commit)) continue;
  if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(row.app) ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(row.repo) ||
      !/^[0-9a-f]{40}$/.test(row.commit) || pins.has(row.app)) throw Error('invalid release source pin');
  pins.set(row.app, { id: row.app, repo: row.repo, name: row.app, summary: '', version: row.index_version,
    install: { git: `https://github.com/${row.repo}`, commit: row.commit } });
}
const upstream = process.env.WIZARD_RELEASE_INDEX_UPSTREAM || 'https://apps.aiwizards.com';
const server = createServer(async (req, res) => {
  const send = (status, data) => {
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(data));
  };
  const url = new URL(req.url, 'http://localhost');
  if (req.method !== 'GET') return send(405, { error: 'read only' });
  if (url.pathname === '/healthz') return send(200, { release: manifest.version });
  // Released WAM's install/update path always resolves by exact id. Answer locally
  // even if the public index is unavailable or no longer lists this app.
  const pin = url.pathname === '/v1/search' && pins.get(url.searchParams.get('id'));
  if (pin) return send(200, { apps: [pin], next_offset: null });
  if (url.pathname !== '/v1/search' && !/^\/v1\/apps\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(url.pathname)) {
    return send(404, { error: 'unknown index route' });
  }
  const repoPin = [...pins.values()].find(p => url.pathname === `/v1/apps/${p.repo}`);
  if (repoPin) return send(200, repoPin);
  try {
    const response = await fetch(upstream + url.pathname + url.search, { signal: AbortSignal.timeout(15000) });
    const data = await response.json();
    // Browsing keeps the catalog's ordering/metadata but never advertises a different
    // source commit for an installed release default. Unpinned entries pass through.
    if (Array.isArray(data.apps)) data.apps = data.apps.map(a => pins.has(a.id) ? { ...a, ...pins.get(a.id) } : a);
    send(response.status, data);
  } catch {
    send(502, { error: 'public index unavailable' });
  }
});
server.listen(Number(process.env.PORT || 7789), process.env.HOST || '0.0.0.0');
