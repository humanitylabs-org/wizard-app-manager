// QA-only module customization hook (registered by /diag/hook.mjs).
// For exactly one URL (file:///app/main.mjs) it adds ONE call at the start of line 1 (reporting the
// byte length + sha256 of the source handed over by the default loader, and the prefix length so
// line-1 stack columns can be corrected) and appends ONE statement to the in-memory module source
// so the preload learns that main.mjs's top-level body finished. No file, image layer or other
// module is changed; every other load passes through untouched.
import { createHash } from 'node:crypto';

let MAIN = 'file:///app/main.mjs';

export async function initialize(data) {
  if (data && typeof data.main === 'string') MAIN = data.main;
}

export async function load(url, context, nextLoad) {
  const r = await nextLoad(url, context);
  if (url !== MAIN || r.format !== 'module' || r.source == null) return r;
  const raw = typeof r.source === 'string' ? Buffer.from(r.source, 'utf8') : Buffer.from(r.source);
  const src = typeof r.source === 'string' ? r.source : new TextDecoder().decode(r.source);
  const head = 'globalThis[Symbol.for("msq.src")]?.(' + raw.length + ',"' + createHash('sha256').update(raw).digest('hex') + '",';
  let n = head.length + 3;
  while ((head + n + ');').length !== n) n = (head + n + ');').length;
  const prefix = head + n + ');';
  return { ...r, source: prefix + src + '\n;globalThis[Symbol.for("msq.mark")]?.("main_module_evaluated");\n' };
}
