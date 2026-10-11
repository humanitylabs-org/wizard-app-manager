// Layout for the My Apps / App Store graph: four rows of fixed width, one anchor, its links pulled into view.
// Pure (no DOM), so it runs in node tests too. wizard-apps.js draws it.
//
//   layout(rows, links, anchor, opts) -> { rows: { Views: { cells, more }, ... }, related, lines }
//
// rows    { Views: [id...], Agents: [...], Apps: [...], System: [...] } in their natural order
// links   Map id -> Set of ids (undirected: uses, used by, needs)
// anchor  id or null
// opts    cols (tiles per row), anchorCol (keep the anchor where it was), open (Set of rows shown in full),
//         pinned (Set of ids that stay last and never fold, e.g. Get apps)
//
// Rules: rows are always left-aligned, with no empty cells. In other rows, linked items come first. In the anchor's
// own row the anchor keeps its column and its linked items sit right next to it. When a row has more than fits, the
// rest folds into one "+N" tile at its end (before pinned tiles), linked items last of all. Nothing is ever shrunk.
// Lines are routed by the drawing code through the gaps between rows and columns, never through a tile.

export const ROWS = ['Views', 'Agents', 'Apps', 'System'];

/** id -> layer index (Views 0 ... System 3). */
export const layersOf = (rows) => new Map(ROWS.flatMap((k, n) => (rows[k] || []).map((id) => [id, n])));

/**
 * Connections only run between layers, never along one: things in a row tie in through the rows above and below.
 * lineage: everything reachable from the anchor moving only toward the views (up) or only toward the system (down).
 * Edges come back as [upper, lower] (views side first), each once.
 */
export function lineage(links, anchor, layer) {
  const nodes = new Set();
  const edges = [];
  if (anchor == null || !layer.has(anchor)) return { nodes, edges };
  nodes.add(anchor);
  const seen = new Set();
  for (const dir of [-1, 1]) {
    const queue = [anchor];
    const done = new Set(queue);
    while (queue.length) {
      const x = queue.shift();
      for (const y of links.get(x) || []) {
        if (!layer.has(y) || Math.sign(layer.get(y) - layer.get(x)) !== dir) continue;
        const e = dir < 0 ? [y, x] : [x, y];
        if (!seen.has(e.join('>'))) {
          seen.add(e.join('>'));
          edges.push(e);
        }
        nodes.add(y);
        if (!done.has(y)) {
          done.add(y);
          queue.push(y);
        }
      }
    }
  }
  return { nodes, edges };
}

/** The anchor's direct links to other layers only (the App Store shows what one app needs and what needs it). */
export function neighbours(links, anchor, layer) {
  if (anchor == null || !layer.has(anchor)) return [];
  return [...(links.get(anchor) || [])]
    .filter((id) => layer.has(id) && layer.get(id) !== layer.get(anchor))
    .map((id) => (layer.get(id) < layer.get(anchor) ? [id, anchor] : [anchor, id]));
}

export function layout(rows, links, anchor, opts = {}) {
  const cols = Math.max(2, Math.floor(opts.cols || 4));
  const open = opts.open || new Set();
  const pinned = opts.pinned || new Set();
  const wrap = opts.wrap || new Set(); // rows that never fold: they wrap onto more lines instead
  const shut = opts.shut || new Set(); // collapsed rows: header only
  const rowOf = new Map();
  for (const k of ROWS) for (const id of rows[k] || []) rowOf.set(id, k);
  if (anchor && !rowOf.has(anchor)) anchor = null;
  const related = new Set(
    anchor ? [...(opts.related || links.get(anchor) || [])].filter((id) => id !== anchor && rowOf.has(id)) : [],
  );

  const out = {};
  for (const k of ROWS) {
    if (shut.has(k)) {
      out[k] = { cells: [], more: null, shut: true };
      continue;
    }
    const ids = rows[k] || [];
    const pins = ids.filter((id) => pinned.has(id));
    const rel = ids.filter((id) => related.has(id));
    const rest = ids.filter((id) => id !== anchor && !related.has(id) && !pinned.has(id));
    const fold = ids.length > cols && !open.has(k) && !wrap.has(k);
    const room = fold ? cols - 1 - pins.length : Infinity; // cells for apps, leaving "+N" and pinned tiles
    let order;
    if (k === rowOf.get(anchor)) {
      // The anchor stays in its column; its linked items follow it (or precede it when the row is short of room).
      const cap = Math.min(room, ids.length - pins.length);
      const a = Math.max(0, Math.min(opts.anchorCol ?? ids.indexOf(anchor), cap - 1, rest.length));
      if (a + 1 + rel.length <= cap || a < rel.length) {
        const n = Math.max(0, Math.min(a, cap - 1 - rel.length));
        order = [...rest.slice(0, n), anchor, ...rel, ...rest.slice(n)];
      } else order = [...rest.slice(0, a - rel.length), ...rel, anchor, ...rest.slice(a - rel.length)];
    } else order = [...rel, ...rest];
    const shown = order.slice(0, room);
    const hidden = order.slice(shown.length);
    const kind = (id) => (id === anchor ? 'anchor' : related.has(id) ? 'rel' : anchor ? 'dim' : 'plain');
    const cells = [];
    const place = (id) => cells.push({ id, col: cells.length % cols, line: Math.floor(cells.length / cols), kind: kind(id) });
    shown.forEach(place);
    let more = null;
    if (hidden.length || (open.has(k) && !wrap.has(k) && ids.length > cols)) {
      const n = cells.length;
      more = { col: n % cols, line: Math.floor(n / cols), ids: hidden, open: open.has(k) };
      cells.push(null);
    }
    pins.forEach(place);
    out[k] = { cells: cells.filter(Boolean), more };
  }
  const visible = new Set(ROWS.flatMap((k) => out[k].cells.map((c) => c.id)));
  return { rows: out, related, visible, lines: [...related].filter((id) => visible.has(id)) };
}

/** Undirected links from `uses` / `needs`, matched by app id or by what an app `provides` (the parked one first). */
export function linksOf(apps, parked = new Map()) {
  const byId = new Map(apps.map((a) => [a.id, a]));
  const providers = new Map();
  for (const a of apps)
    for (const p of a.provides || []) {
      if (!providers.has(p)) providers.set(p, []);
      providers.get(p).push(a.id);
    }
  const links = new Map(apps.map((a) => [a.id, new Set()]));
  const join = (x, y) => {
    if (x === y || !links.has(x) || !links.has(y)) return;
    links.get(x).add(y);
    links.get(y).add(x);
  };
  for (const a of apps)
    for (const want of [...(a.uses || []), ...(a.needs || [])]) {
      if (byId.has(want)) join(a.id, want);
      else join(a.id, parked.get(want) || (providers.get(want) || [])[0]);
    }
  return links;
}
