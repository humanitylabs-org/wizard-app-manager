"""Permission-safe access to container-owned backup trees.

The owner runs WAM, not root. Only inaccessible app-data and backup staging
paths use a root, offline one-off container of the already installed Manager
image (as AppData does). Never chmod/chown live state to make it readable.
The helper accepts filesystem operations, not shell fragments, and lives for
one operation. Ordinary owner paths and alongside Hermes stay host-side.
"""
from __future__ import annotations

import base64
import builtins
import contextvars
import errno
import functools
import io
import json
import os
from pathlib import Path as HostPath
import shutil
import subprocess
from contextlib import contextmanager

_current = contextvars.ContextVar("backup_filesystem", default=None)

NODE = r'''
const fs = require('node:fs');
const readline = require('node:readline');
const roots = JSON.parse(process.argv[1]);
const path = require('node:path');
function checked(p) {
  if (typeof p !== 'string' || !path.isAbsolute(p)) throw Error('absolute path required');
  p = path.normalize(p);
  if (!roots.some(r => p === r || p.startsWith(r + '/'))) throw Error('outside backup roots');
  // Ancestor links must not turn a confined operation into a host-tree escape.
  let cur = '/';
  for (const part of p.split('/').filter(Boolean).slice(0, -1)) {
    cur = path.join(cur, part);
    try { if (fs.lstatSync(cur).isSymbolicLink()) throw Error('symlinked backup ancestor'); }
    catch (e) { if (e.code === 'ENOENT') break; throw e; }
  }
  return p;
}
const handles = new Map(); let next = 1;
readline.createInterface({input:process.stdin}).on('line', line => {
  try {
    const q = JSON.parse(line); let v;
    const p = q.path === undefined ? undefined : checked(q.path);
    switch(q.op) {
      case 'stat': case 'lstat': {
        const s = q.op === 'stat' ? fs.statSync(p) : fs.lstatSync(p);
        v = [s.mode,s.ino,s.dev,s.nlink,s.uid,s.gid,s.size,s.atimeMs/1000,s.mtimeMs/1000,s.ctimeMs/1000]; break;
      }
      case 'listdir': v=fs.readdirSync(p); break;
      case 'readlink': v=fs.readlinkSync(p); break;
      case 'mkdir': fs.mkdirSync(p,{mode:q.mode}); break;
      case 'chmod': fs.chmodSync(p,q.mode); break;
      case 'unlink': fs.unlinkSync(p); break;
      case 'rmdir': fs.rmdirSync(p); break;
      case 'rmtree': fs.rmSync(p,{recursive:true,force:false}); break;
      case 'rename': fs.renameSync(p,checked(q.dst)); break;
      case 'sync': { const fd=fs.openSync(p,'r'); try{fs.fsyncSync(fd);}finally{fs.closeSync(fd);} break; }
      case 'open': { if (fs.existsSync(p) && fs.lstatSync(p).isSymbolicLink()) throw Error('refusing protected file link'); const fd=fs.openSync(p,q.flags,q.mode); v=next++; handles.set(v,fd); break; }
      case 'read': { const b=Buffer.alloc(Math.min(q.size,1048576)); const n=fs.readSync(handles.get(q.handle),b,0,b.length,null); v=b.subarray(0,n).toString('base64'); break; }
      case 'write': { const b=Buffer.from(q.data,'base64'); v=fs.writeSync(handles.get(q.handle),b); break; }
      case 'close': fs.closeSync(handles.get(q.handle)); handles.delete(q.handle); break;
      default: throw Error('unknown filesystem operation');
    }
    console.log(JSON.stringify({ok:true,value:v===undefined?null:v}));
  } catch(e) { console.log(JSON.stringify({ok:false,code:e.code||'EIO',message:e.message})); }
});
'''


class Access:
    def __init__(self, box):
        self.box, self.proc = box, None
        self.roots = [str(HostPath(p).absolute()) for p in
                      (box.runtipi_root / 'app-data', box.home / 'restores', box.home / 'backup-work')]

    def owns(self, p):
        p = str(HostPath(p).absolute())
        return any(p == r or p.startswith(r + '/') for r in self.roots)

    def call(self, op, path=None, **kw):
        if 'dst' in kw and not self.owns(kw['dst']):
            raise PermissionError(f"outside container-owned backup roots: {kw['dst']}")
        if path is not None and not self.owns(path):
            raise PermissionError(f"outside container-owned backup roots: {path}")
        if self.proc is None:
            cmd = ['docker', 'run', '--rm', '-i', '--network', 'none', '-u', '0:0']
            # The restore journal deliberately renames across app-data and its
            # undo staging. Bind their common filesystem ancestors, never /.
            for p in dict.fromkeys((self.box.runtipi_root, self.box.home)):
                cmd += ['-v', f'{p}:{p}']
            cmd += ['--entrypoint', 'node', self.box.appdata.image(), '-e', NODE, json.dumps(self.roots)]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, env=self.box.child_env)
        q = dict(op=op, **kw)
        if path is not None:
            q['path'] = str(HostPath(path).absolute())
        self.proc.stdin.write(json.dumps(q) + '\n'); self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise OSError('container-owned backup filesystem helper exited')
        r = json.loads(line)
        if not r['ok']:
            code = getattr(errno, r['code'], errno.EIO)
            raise OSError(code, r['message'], str(path) if path is not None else None)
        return r['value']

    def close(self):
        if self.proc is not None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()
            self.proc.stdout.close()


@contextmanager
def operation(box):
    existing = _current.get()
    if existing is not None:
        if existing.box is not box:
            raise RuntimeError('nested backup operation uses another box')
        yield
        return
    access = Access(box)
    token = _current.set(access)
    try:
        yield
    finally:
        _current.reset(token)
        access.close()


def with_access(fn):
    @functools.wraps(fn)
    def wrapped(box, *args, **kw):
        with operation(box):
            return fn(box, *args, **kw)
    return wrapped


def _fallback(op, path, **kw):
    access = _current.get()
    if access is None or not access.owns(path):
        raise PermissionError(errno.EACCES, 'backup path is not accessible', str(path))
    return access.call(op, path, **kw)


def stat(path, *, follow_symlinks=True):
    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except PermissionError:
        return os.stat_result(_fallback('stat' if follow_symlinks else 'lstat', path))


def lstat(path):
    try:
        return os.lstat(path)
    except PermissionError:
        return os.stat_result(_fallback('lstat', path))


def listdir(path):
    try:
        return os.listdir(path)
    except PermissionError:
        return _fallback('listdir', path)


def readlink(path):
    try:
        return os.readlink(path)
    except PermissionError:
        return _fallback('readlink', path)


def rename(src, dst):
    try:
        return os.rename(src, dst)
    except PermissionError:
        return _fallback('rename', src, dst=str(dst))


def sync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except PermissionError:
        return _fallback('sync', path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rmtree(path, ignore_errors=False):
    try:
        return shutil.rmtree(path)
    except PermissionError:
        try:
            return _fallback('rmtree', path)
        except OSError:
            if not ignore_errors:
                raise
    except OSError:
        if not ignore_errors:
            raise


class RemoteFile(io.RawIOBase):
    def __init__(self, path, flags, mode):
        self.access = _current.get()
        self.handle = _fallback('open', path, flags=flags, mode=mode)

    def readable(self): return True
    def writable(self): return True
    def readinto(self, b):
        data = base64.b64decode(self.access.call('read', handle=self.handle, size=len(b)))
        b[:len(data)] = data
        return len(data)
    def write(self, b):
        return self.access.call('write', handle=self.handle, data=base64.b64encode(b).decode())
    def close(self):
        if not self.closed and hasattr(self, 'handle'):
            self.access.call('close', handle=self.handle)
        super().close()


def open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
    try:
        return builtins.open(path, mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)
    except PermissionError:
        if mode not in ('r', 'rb', 'w', 'wb'):
            raise ValueError('unsupported protected backup file mode') from None
        raw = RemoteFile(path, 'r' if mode.startswith('r') else 'w', 0o600)
        buffered = io.BufferedReader(raw) if mode.startswith('r') else io.BufferedWriter(raw)
        return buffered if 'b' in mode else io.TextIOWrapper(buffered, encoding=encoding or 'utf-8', errors=errors,
                                                           newline=newline)


class Path(type(HostPath())):
    """Path operations retain permission failures even on Python versions hiding them."""
    def stat(self, *, follow_symlinks=True): return stat(self, follow_symlinks=follow_symlinks)
    def lstat(self): return lstat(self)
    def exists(self, *, follow_symlinks=True):
        try: self.stat(follow_symlinks=follow_symlinks); return True
        except (FileNotFoundError, NotADirectoryError): return False
    def is_dir(self):
        import stat as s
        try: return s.S_ISDIR(self.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError): return False
    def is_file(self):
        import stat as s
        try: return s.S_ISREG(self.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError): return False
    def is_symlink(self):
        import stat as s
        try: return s.S_ISLNK(self.lstat().st_mode)
        except (FileNotFoundError, NotADirectoryError): return False
    def iterdir(self): return (self / n for n in listdir(self))
    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        return open(self, mode, buffering, encoding, errors, newline)
    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        try:
            try: os.mkdir(self, mode)
            except PermissionError: _fallback('mkdir', self, mode=mode)
        except FileNotFoundError:
            if not parents or self.parent == self: raise
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, exist_ok=exist_ok)
        except FileExistsError:
            if not exist_ok or not self.is_dir(): raise
    def chmod(self, mode, *, follow_symlinks=True):
        try: os.chmod(self, mode, follow_symlinks=follow_symlinks)
        except PermissionError:
            if not follow_symlinks: raise
            _fallback('chmod', self, mode=mode)
    def unlink(self, missing_ok=False):
        try:
            try: os.unlink(self)
            except PermissionError: _fallback('unlink', self)
        except FileNotFoundError:
            if not missing_ok: raise
    def rmdir(self):
        try: os.rmdir(self)
        except PermissionError: _fallback('rmdir', self)


def walk(root):
    root = Path(root)
    try: names = sorted(root.iterdir())
    except FileNotFoundError: return
    dirs = [p.name for p in names if p.is_dir()]
    files = [p.name for p in names if p.name not in dirs]
    yield str(root), dirs, files
    for name in dirs:
        child = root / name
        if not child.is_symlink():
            yield from walk(child)
