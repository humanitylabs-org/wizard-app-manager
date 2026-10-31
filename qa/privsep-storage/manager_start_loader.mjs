// QA-only module customization hook (registered by /diag/hook.mjs).
// For exactly one URL (file:///app/main.mjs) it appends ONE statement to the in-memory
// module source so the preload learns that main.mjs's top-level body finished. No file,
// image layer or other module is changed; every other load passes through untouched.
let MAIN = 'file:///app/main.mjs';

export async function initialize(data) {
  if (data && typeof data.main === 'string') MAIN = data.main;
}

export async function load(url, context, nextLoad) {
  const r = await nextLoad(url, context);
  if (url !== MAIN || r.format !== 'module' || r.source == null) return r;
  const src = typeof r.source === 'string' ? r.source : new TextDecoder().decode(r.source);
  return { ...r, source: src + '\n;globalThis[Symbol.for("msq.mark")]?.("main_module_evaluated");\n' };
}
