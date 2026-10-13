// URL adapter for Runtipi's existing domain/local-domain Open actions.
// No proxy or routing configuration lives here; those belong to stock Runtipi.
export function runtipiDomainUrl(row, settings = {}) {
  const app = row?.app || {};
  const info = row?.info || {};
  if (info.no_gui || app.status !== 'running' || app.pendingRestart) return '';
  const suffix = info.url_suffix || '/';
  if (
    typeof suffix !== 'string' ||
    !suffix.startsWith('/') ||
    suffix.startsWith('//') ||
    suffix.includes('\\') ||
    [...suffix].some((c) => c.charCodeAt(0) <= 32 || c.charCodeAt(0) === 127)
  )
    return '';
  let host = '';
  if (app.exposed && app.domain) host = app.domain;
  else if (app.exposedLocal && settings.localDomain && row?.metadata?.localSubdomain) {
    host = `${row.metadata.localSubdomain}.${settings.localDomain}`;
  }
  if (!host || !/^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i.test(host)) return '';
  const port = Number(settings.sslPort ?? 443);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return '';
  return `https://${host}${port === 443 ? '' : `:${port}`}${suffix}`;
}
