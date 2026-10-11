import { test } from 'node:test';
import assert from 'node:assert/strict';
import { layout, linksOf } from '../wizard-views/wizard-graph.js';

const apps = [
  { id: 'ui', uses: ['agent'] },
  { id: 'agent', uses: [] },
  { id: 'video', uses: ['stt', 'browser', 'files'] },
  { id: 'files', provides: ['files'], needs: ['files'] },
  { id: 'stt', needs: ['files'] },
  { id: 'browser' },
  { id: 'keys' },
  { id: 'drop' },
];
const rows = { Views: ['ui'], Agents: ['agent'], Apps: ['video'], System: ['keys', 'drop', 'files', 'stt', 'browser'] };
const links = linksOf(apps);
const at = (L, k, id) => L.rows[k].cells.find((c) => c.id === id);
const cols = (L, k) => L.rows[k].cells.map((c) => c.col).concat(L.rows[k].more ? [L.rows[k].more.col] : []);

test('links run both ways, by id or by what an app provides, never to itself', () => {
  assert.deepEqual([...links.get('agent')], ['ui']);
  assert.deepEqual([...links.get('files')].sort(), ['stt', 'video']);
});

test('linked items come first, rows have no gaps, extras fold into a last "+N"', () => {
  const L = layout(rows, links, 'video', { cols: 4 });
  assert.equal(at(L, 'Apps', 'video').kind, 'anchor');
  const sys = L.rows.System;
  assert.deepEqual(sys.cells.slice(0, 3).map((c) => c.kind), ['rel', 'rel', 'rel']);
  assert.deepEqual(cols(L, 'System'), [0, 1, 2, 3], 'left-aligned, no empty cells');
  assert.equal(sys.more.col, 3, '+N is last');
  assert.deepEqual(sys.more.ids.sort(), ['drop', 'keys']);
  assert.deepEqual(L.lines.sort(), ['browser', 'files', 'stt']);
  assert.equal(at(L, 'Agents', 'agent').col, 0);
});

test('the anchor keeps its column and its row neighbours sit beside it', () => {
  const L = layout(rows, links, 'stt', { cols: 4, anchorCol: 2 });
  assert.equal(at(L, 'System', 'stt').col, 2);
  assert.equal(Math.abs(at(L, 'System', 'files').col - 2), 1, 'beside the anchor');
  assert.deepEqual(cols(L, 'System').sort(), [0, 1, 2, 3]);
  assert.equal(L.rows.System.more.col, 3);
});

test('pinned tiles stay last, after "+N"; an open row shows everything', () => {
  const r = { ...rows, Apps: ['video', 'x1', 'x2', 'x3', 'get'] };
  const P = layout(r, links, null, { cols: 4, pinned: new Set(['get']) });
  assert.equal(at(P, 'Apps', 'get').col, 3);
  assert.equal(P.rows.Apps.more.col, 2);
  const O = layout(r, links, null, { cols: 4, pinned: new Set(['get']), open: new Set(['Apps']) });
  assert.equal(O.rows.Apps.cells.length, 5);
  assert.equal(O.rows.Apps.more.open, true);
});
