// QA-only diagnostic preload for the pinned Wizard App Manager image (backend 595523fb).
//
// Mounted read-only at /diag as hook.mjs (+ loader.mjs) and activated ONLY through
// NODE_OPTIONS=--import=/diag/hook.mjs. The image, its CMD and /app are unchanged.
// It writes ONLY one finite JSON document per process to /diag-out/diag-<0..7>.json
// (milestone enum, exit facts, allowlisted counts, v2 trace below); never env, argv, paths,
// messages or log text, and stacks only as MAIN line:col numbers. Observation is passive: it never adds an 'uncaughtException' or
// 'unhandledRejection' listener (either would change crash semantics), never catches
// an application error, and re-raises nothing itself. Wrapped functions call the
// original synchronously with the same arguments and return its value unchanged.
//
// Milestone detection (documented in manager_start.md):
//   preload_loaded         this module evaluated (before the main module)
//   main_module_evaluated  loader.mjs appends a single call to the IN-MEMORY source of
//                          file:///app/main.mjs (module.register load hook); it runs after
//                          main.mjs's top-level body, i.e. after bootstrap() was called
//   env_file_generated     fs.promises.writeFile('/data/.env', <non-empty>) resolved
//                          (generateSystemEnvFile's final write)
//   nest_create_started    first Reflect.getMetadata('imports', ...) after main evaluation
//                          (Nest DependenciesScanner module scan inside NestFactory.create)
//   nest_create_finished   first http.createServer(...) (NestApplication constructor,
//                          after initialize(); NestFactory.create returns right after)
//   bootstrap_finished     first express app.set('trust proxy', ...) on the app passed to
//                          that createServer (main.ts line after appService.bootstrap())
//   listen_called          server.listen(...) on that server
//   listening_3000         that server's 'listening' event with address().port === 3000
//
// Trace fields (msq-diag-v2, finite; added after the v1 discriminator showed the bundle body
// apparently never ran in the container: reflect-metadata absent at main end, ~35 ms eval):
//   trace.main_file    size + sha256 of MAIN's file as read by this process at preload
//   trace.main_source  byte length + sha256 of the source the load hook handed to V8 (before
//                      the QA prefix/suffix), reported by a one-call PREFIX on line 1
//   trace.prefix_chars length of that prefix (line-1 columns in stacks are shifted by it)
//   trace.cwd_is_main_dir  boolean only
//   trace.app_mounts   count of /proc/self/mountinfo mount points at or under MAIN's dir
//   trace.root_fstype  'overlay' | 'other' | 'unknown' for '/'
//   trace.pending      at first beforeExit: count of never-resolved promises (async_hooks) and up
//                      to 8 distinct creation stacks reduced to MAIN 'line:col' frames ('node' /
//                      'other' for frames outside MAIN). Never messages, paths or values.
import { createHook } from 'node:async_hooks';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { register, syncBuiltinESMExports } from 'node:module';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const OUT_DIR = process.env.MSQ_DIAG_OUT_FOR_TESTS || '/diag-out';
const ENV_PATH = process.env.MSQ_DIAG_ENV_FOR_TESTS || '/data/.env';
const MAIN_URL = process.env.MSQ_DIAG_MAIN_FOR_TESTS || 'file:///app/main.mjs';
const PORT = 3000;
const SLOTS = 8;
const MILESTONES = ['preload_loaded', 'main_module_evaluated', 'env_file_generated', 'nest_create_started',
  'nest_create_finished', 'bootstrap_finished', 'listen_called', 'listening_3000'];
const RESOURCES = ['CloseReq', 'FSEventWrap', 'FSReqCallback', 'FSReqPromise', 'GetAddrInfoReqWrap',
  'GetNameInfoReqWrap', 'HTTPINCOMINGMESSAGE', 'HTTPCLIENTREQUEST', 'Immediate', 'JSSTREAM', 'MessagePort',
  'PipeConnectWrap', 'PipeServerWrap', 'PipeWrap', 'ProcessWrap', 'PromiseResolveThenableJob',
  'QueryReqWrap', 'ShutdownWrap', 'SignalWrap', 'StatWatcher', 'TCPConnectWrap', 'TCPServerWrap', 'TCPWrap',
  'TTYWrap', 'Timeout', 'UDPSendWrap', 'UDPWrap', 'Worker', 'WriteWrap', 'ZlibWrap'];
const CTORS = ['AbortError', 'AggregateError', 'CircularDependencyException', 'DatabaseError', 'DrizzleError',
  'DrizzleQueryError', 'Error', 'EvalError', 'HttpException', 'InvalidClassModuleException', 'PostgresError',
  'RangeError', 'ReferenceError', 'RuntimeException', 'SyntaxError', 'TransactionRollbackError', 'TypeError',
  'URIError', 'UndefinedDependencyException', 'UnknownDependenciesException', 'UnknownElementException',
  'ValidationError'];
const CODE = /^E[A-Z0-9_]{2,30}$/;
const MARK = Symbol.for('msq.mark');

const PREFIX = 'globalThis[Symbol.for("msq.src")]?.(';    // loader.mjs builds PREFIX + N + ',"<hex>");'
const PENDING_CAP = 50000;
const STACKS = 8;
const FRAMES = 12;
const HEX = /^[0-9a-f]{64}$/;

const state = {
  schema: 'msq-diag-v2', slot: null, node_major: Number(process.versions.node.split('.')[0]) || 0,
  milestones: [], t_ms: {}, loader_hook: 'not_attempted', reflect_hook: 'not_armed',
  exit: { fired: false, code: null },
  before_exit: { fired: false, count: 0, resources: {} },
  uncaught: { count: 0, first: null },
  exit_calls: [], hook_errors: 0,
  trace: {
    main_file: { size: null, sha256: null, error: null }, main_source: null, prefix_chars: null,
    cwd_is_main_dir: false, app_mounts: 0, root_fstype: 'unknown',
    pending: { armed: false, count: 0, overflow: 0, stacks: [] },
  },
};
let outPath = null;
const writeSync = fs.writeFileSync;

function elapsed() {
  const v = Math.round(performance.now());
  return v >= 0 && v <= 3600000 ? v : 3600000;
}

function save() {
  if (outPath === null) return;
  try { writeSync(outPath, JSON.stringify(state) + '\n'); } catch { /* never disturb the app */ }
}

function mark(name) {
  try {
    if (!MILESTONES.includes(name) || state.milestones.includes(name)) return;
    state.milestones.push(name); state.t_ms[name] = elapsed();
    if (name === 'main_module_evaluated') armReflect();
    save();
  } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
}

function exitCode(code) {
  if (code === undefined) return 'undefined';
  if (typeof code === 'string' && /^-?[0-9]{1,3}$/.test(code)) code = Number(code);
  return Number.isInteger(code) && code >= -255 && code <= 255 ? code : 'OTHER';
}

function ctorName(err) {
  try {
    const n = err !== null && (typeof err === 'object' || typeof err === 'function') ? err.constructor?.name : null;
    return CTORS.includes(n) ? n : 'OTHER';
  } catch { return 'OTHER'; }
}

function errCode(err) {
  try {
    const c = err !== null && typeof err === 'object' ? err.code : undefined;
    if (c === undefined || c === null) return null;
    return typeof c === 'string' && CODE.test(c) ? c : 'OTHER';
  } catch { return 'OTHER'; }
}

function uncaught(err, origin) {
  try {
    state.uncaught.count = Math.min(state.uncaught.count + 1, 99);
    if (state.uncaught.first === null) {
      state.uncaught.first = { ctor: ctorName(err), code: errCode(err),
        origin: ['uncaughtException', 'unhandledRejection', 'unhandledRejection_event'].includes(origin) ? origin : 'OTHER' };
    }
    save();
  } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
}

function armReflect() {
  const R = globalThis.Reflect; const orig = R && R.getMetadata;
  if (typeof orig !== 'function') { state.reflect_hook = 'absent'; return; }
  R.getMetadata = function getMetadata(key, ...rest) {
    if (key === 'imports') mark('nest_create_started');
    return orig.call(this, key, ...rest);
  };
  state.reflect_hook = 'armed';
}

// --- trace: identities and pending promises (finite projections only) ---
const SELF_URL = import.meta.url;
const MAIN_PATH = (() => { try { return fileURLToPath(MAIN_URL); } catch { return null; } })();
const MAIN_DIR = MAIN_PATH ? MAIN_PATH.slice(0, MAIN_PATH.lastIndexOf('/')) || '/' : null;
const MAIN_NAME = MAIN_PATH ? MAIN_PATH.slice(MAIN_PATH.lastIndexOf('/') + 1) : null;

function traceIdentity() {
  const t = state.trace;
  try {
    const raw = fs.readFileSync(MAIN_PATH);
    t.main_file = { size: raw.length, sha256: createHash('sha256').update(raw).digest('hex'), error: null };
  } catch (e) {
    const c = e && typeof e.code === 'string' && CODE.test(e.code) ? e.code : 'OTHER';
    t.main_file = { size: null, sha256: null, error: c };
  }
  try { t.cwd_is_main_dir = process.cwd() === MAIN_DIR; } catch { t.cwd_is_main_dir = false; }
  try {
    let n = 0; let root = 'unknown';
    for (const line of fs.readFileSync('/proc/self/mountinfo', 'latin1').split('\n')) {
      const f = line.split(' '); const sep = f.indexOf('-');
      if (f.length < 7 || sep < 6) continue;
      const mp = f[4];
      if (mp === MAIN_DIR || mp.startsWith(MAIN_DIR + '/')) n += 1;
      if (mp === '/') root = f[sep + 1] === 'overlay' ? 'overlay' : 'other';
    }
    t.app_mounts = Math.min(n, 64); t.root_fstype = root;
  } catch { /* stays unknown */ }
}

// Reduce one captured V8 stack to MAIN 'line:col' frames; nothing else leaves this function.
function frames(holder) {
  const out = [];
  try {
    const lines = String(holder.stack).split('\n').slice(1).filter((l) => !l.includes(SELF_URL) && !/internal\/async_hooks/.test(l));
    for (const l of lines) {
      if (out.length >= FRAMES) break;
      const m = /([^/\\():\s]+):(\d{1,7}):(\d{1,8})\)?\s*$/.exec(l);
      if (m && m[1] === MAIN_NAME && l.includes(MAIN_URL)) out.push(m[2] + ':' + m[3]);
      else if (/\(node:|at node:|\bnode:[a-z_/]+:\d/.test(l)) out.push('node');
      else out.push('other');
    }
  } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
  return out;
}

const pending = new Map();
function armPending() {
  try {
    const onInit = function onInit(id, type) {
      try {
        if (type !== 'PROMISE') return;
        if (pending.size >= PENDING_CAP) { state.trace.pending.overflow = Math.min(state.trace.pending.overflow + 1, 999999); return; }
        const holder = {}; const lim = Error.stackTraceLimit;
        Error.stackTraceLimit = FRAMES + 4; Error.captureStackTrace(holder); Error.stackTraceLimit = lim;
        pending.set(id, holder);
      } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
    };
    createHook({ init: onInit, promiseResolve(id) { pending.delete(id); } }).enable();
    state.trace.pending.armed = true;
  } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
}

function snapshotPending() {
  const p = state.trace.pending;
  try {
    p.count = Math.min(pending.size, 999999);
    const all = [...pending.values()];
    const seen = new Set(); const distinct = [];
    for (const h of all) {
      const f = frames(h); const k = f.join(' ');
      if (!seen.has(k)) { seen.add(k); distinct.push(f); }
    }
    const pick = distinct.length <= STACKS ? distinct : distinct.slice(0, STACKS / 2).concat(distinct.slice(-STACKS / 2));
    p.stacks = pick;
  } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
}

globalThis[Symbol.for('msq.src')] = function src(bytes, hex, prefixChars) {
  try {
    if (Number.isInteger(bytes) && bytes >= 0 && bytes <= 268435456 && typeof hex === 'string' && HEX.test(hex) &&
        Number.isInteger(prefixChars) && prefixChars > PREFIX.length && prefixChars <= 256) {
      state.trace.main_source = { bytes, sha256: hex }; state.trace.prefix_chars = prefixChars; save();
    }
  } catch { /* */ }
};

// --- slot: first free of diag-0..7 (O_EXCL); after 8 runs nothing is written ---
try {
  for (let k = 0; k < SLOTS && outPath === null; k++) {
    const p = OUT_DIR + '/diag-' + k + '.json';
    try { fs.closeSync(fs.openSync(p, 'wx', 0o644)); outPath = p; state.slot = k; } catch { /* taken */ }
  }
} catch { state.hook_errors += 1; }

try {
  globalThis[MARK] = mark;
  mark('preload_loaded');
  traceIdentity();
  armPending();

  // process.exit: record the requested code, then the original with the same arguments.
  const origExit = process.exit;
  process.exit = function exit(code) {
    try { if (state.exit_calls.length < 4) state.exit_calls.push(exitCode(arguments.length ? code : undefined)); save(); } catch { /* */ }
    return origExit.apply(this, arguments);
  };
  process.on('exit', (code) => { state.exit.fired = true; state.exit.code = exitCode(code); save(); });
  process.on('beforeExit', () => {
    try {
      state.before_exit.count = Math.min(state.before_exit.count + 1, 99);
      if (!state.before_exit.fired) {
        state.before_exit.fired = true;
        const counts = {};
        const info = typeof process.getActiveResourcesInfo === 'function' ? process.getActiveResourcesInfo() : [];
        for (const t of info.slice(0, 4096)) {
          const k = RESOURCES.includes(t) ? t : 'OTHER';
          counts[k] = Math.min((counts[k] || 0) + 1, 9999);
        }
        state.before_exit.resources = counts;
        snapshotPending();
      }
      save();
    } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
  });
  // Monitor only: does not handle the exception or change the exit path.
  process.on('uncaughtExceptionMonitor', (err, origin) => uncaught(err, origin));
  // Observe 'unhandledRejection' WITHOUT registering a listener: pass-through emit wrapper
  // (Node's rejection tracker calls process.emit and uses its boolean result unchanged).
  const origEmit = process.emit;
  process.emit = function emit(event, ...rest) {
    if (event === 'unhandledRejection') uncaught(rest[0], 'unhandledRejection_event');
    return origEmit.call(this, event, ...rest);
  };

  const origWriteFile = fs.promises.writeFile;
  fs.promises.writeFile = function writeFile(file, data, ...rest) {
    const p = origWriteFile.call(this, file, data, ...rest);
    try {
      if (file === ENV_PATH && data != null && data.length > 0) p.then(() => mark('env_file_generated'), () => {});
    } catch { /* */ }
    return p;
  };

  const origCreateServer = http.createServer;
  http.createServer = function createServer(...args) {
    const srv = origCreateServer.apply(this, args);
    try {
      if (!state.milestones.includes('nest_create_finished')) {
        mark('nest_create_finished');
        const app = typeof args[0] === 'function' ? args[0] : args[1];
        if (typeof app === 'function' && typeof app.set === 'function') {
          const origSet = app.set;
          app.set = function set(key, ...rest) {
            if (key === 'trust proxy') mark('bootstrap_finished');
            return origSet.call(this, key, ...rest);
          };
        }
        const origListen = srv.listen;
        srv.listen = function listen(...a) { mark('listen_called'); return origListen.apply(this, a); };
        srv.once('listening', () => {
          try { const ad = srv.address(); if (ad && typeof ad === 'object' && ad.port === PORT) mark('listening_3000'); } catch { /* */ }
        });
      }
    } catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); }
    return srv;
  };
  syncBuiltinESMExports();

  try {
    register('./loader.mjs', { parentURL: import.meta.url, data: { main: MAIN_URL } });
    state.loader_hook = 'registered';
  } catch { state.loader_hook = 'failed'; }
  save();
} catch { state.hook_errors = Math.min(state.hook_errors + 1, 99); save(); }
