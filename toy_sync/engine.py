"""Filesystem synchronization, independent of Qt. No notifications or network services."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid

HOME = Path.home()
CONFIG = Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config')) / 'toy-sync'
DATA = Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share')) / 'toy-sync'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def pair(name, source, target, mode='two-way', schedule='changes', minutes=10, keep=3):
    return dict(id=uuid.uuid4().hex, name=name, source=str(source), target=str(target),
                mode=mode, schedule=schedule, minutes=minutes, keep=keep, enabled=True)


def validate(p, others=()):
    a, b = (Path(p[k]).expanduser().resolve() for k in ('source', 'target'))
    if a == b or a in b.parents or b in a.parents:
        raise ValueError('Choose separate folders; neither may contain the other.')
    for root in (a, b):
        if root == Path('/') or root == HOME:
            raise ValueError('Choose a specific folder, rather than your entire home or filesystem.')
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError('Both paths must be folders.')
        if any(root == internal or root in internal.parents or internal in root.parents
               for internal in (CONFIG.resolve(), DATA.resolve())):
            raise ValueError('Toy_Sync configuration and state cannot be synchronized.')
    for other in others:
        if other['id'] == p['id']:
            continue
        # A backup may read folders also used in a sync, but all writable roots must be isolated.
        writes = [Path(other['target']).resolve()]
        if other['mode'] == 'two-way':
            writes.append(Path(other['source']).resolve())
        mine = [b] + ([a] if p['mode'] == 'two-way' else [])
        for x in mine:
            for y in writes:
                if x == y or x in y.parents or y in x.parents:
                    raise ValueError('Writable folders overlap another pairing. Choose separate destinations.')
    p.update(source=str(a), target=str(b))


class Journal:
    def __init__(self, directory=DATA):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'activity.sqlite3'
        self.prune()

    def prune(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity (at REAL, level TEXT, message TEXT)')
            db.execute('DELETE FROM activity WHERE at < ?', (time.time() - 86400,))
            db.execute('PRAGMA secure_delete=ON')
        # Reclaim deleted records rather than leaving old log text in free pages.
        with sqlite3.connect(self.path) as db:
            db.execute('VACUUM')

    def add(self, message, level='INFO'):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO activity VALUES (?, ?, ?)', (time.time(), level, message))

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT at, level, message FROM activity WHERE at >= ? ORDER BY at DESC',
                              (time.time() - 86400,)).fetchall()


def scan(root):
    """Never traverse links or special files. Failure aborts before reconciliation."""
    result = {}
    def walk(folder):
        with os.scandir(folder) as entries:
            for entry in entries:
                p = Path(entry.path)
                if entry.is_symlink():
                    raise RuntimeError(f'Symbolic link found: {p}. Remove it from the pairing before syncing.')
                if entry.is_dir(follow_symlinks=False):
                    walk(p)
                elif entry.is_file(follow_symlinks=False):
                    s = entry.stat(follow_symlinks=False)
                    result[p.relative_to(root).as_posix()] = [s.st_size, s.st_mtime_ns]
                else:
                    raise RuntimeError(f'Unsupported special file: {p}')
    walk(root)
    return result


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.digest()


def equal(a, b, sa, sb):
    return sa == sb or (sa and sb and sa[0] == sb[0] and digest(a) == digest(b))


def copy_file(src, dst, expected):
    before = src.stat()
    if [before.st_size, before.st_mtime_ns] != expected:
        raise RuntimeError(f'File changed during sync: {src}; will retry.')
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / ('.toy-sync-' + uuid.uuid4().hex)
    try:
        shutil.copy2(src, tmp)
        after = src.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f'File changed during sync: {src}; will retry.')
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


class Engine:
    def __init__(self, directory=DATA, log=None):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.log = log or (lambda message, level='INFO': None)

    def run(self, p):
        validate(p)
        a, b = Path(p['source']), Path(p['target'])
        if not a.is_dir() or not b.is_dir():
            raise RuntimeError('A folder is missing or unavailable. Reconnect it or create it before syncing.')
        # Preserve the old setup's protection for the active Trilium database.
        for root in (a, b):
            db = root / 'document.db'
            if db.exists():
                if not shutil.which('lsof'):
                    raise RuntimeError('Install lsof to check whether the Trilium database is open.')
                if subprocess.run(['lsof', '-t', str(db)], capture_output=True).returncode == 0:
                    raise RuntimeError('Trilium is using document.db. Close Trilium before syncing this pair.')
        if p['mode'] == 'backup':
            return self.backup(p, a, b)
        sa, sb = scan(a), scan(b)
        state = self.directory / (p['id'] + '.json')
        previous = json.loads(state.read_text()) if state.exists() else {'a': {}, 'b': {}}
        count = 0
        for name in sorted(sa.keys() | sb.keys()):
            x, y = a / name, b / name
            va, vb = sa.get(name), sb.get(name)
            if va and vb and equal(x, y, va, vb):
                continue
            if p['mode'] == 'one-way':
                if va:
                    copy_file(x, y, va)
                    sb[name] = va
                    self.log(f'{p["name"]}: copied {name} → destination')
                    count += 1
                continue
            olda, oldb = previous['a'].get(name), previous['b'].get(name)
            ca, cb = va != olda, vb != oldb
            if va and vb and (ca and cb or not state.exists()):
                # Keep both versions on both sides. Never silently choose a winner.
                conflict = name + '.conflict-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
                copy_file(y, a / conflict, vb)
                copy_file(y, b / conflict, vb)
                copy_file(x, y, va)
                sa[conflict] = sb[conflict] = vb
                sb[name] = va
                self.log(f'{p["name"]}: preserved both versions of {name} as {conflict}', 'CONFLICT')
            elif va and not vb:
                if oldb and not ca:
                    if [x.stat().st_size, x.stat().st_mtime_ns] != va:
                        raise RuntimeError(f'File changed before deletion: {x}')
                    x.unlink()
                    sa.pop(name, None)
                    self.log(f'{p["name"]}: removed {name} (deleted at destination)')
                else:
                    copy_file(x, y, va)
                    sb[name] = va
                    self.log(f'{p["name"]}: copied {name} → destination')
            elif vb and not va:
                if olda and not cb:
                    if [y.stat().st_size, y.stat().st_mtime_ns] != vb:
                        raise RuntimeError(f'File changed before deletion: {y}')
                    y.unlink()
                    sb.pop(name, None)
                    self.log(f'{p["name"]}: removed {name} (deleted at source)')
                else:
                    copy_file(y, x, vb)
                    sa[name] = vb
                    self.log(f'{p["name"]}: copied {name} → source')
            elif ca:
                copy_file(x, y, va)
                sb[name] = va
                self.log(f'{p["name"]}: updated {name} → destination')
            elif cb:
                copy_file(y, x, vb)
                sa[name] = vb
                self.log(f'{p["name"]}: updated {name} → source')
            count += 1
        # Create empty folders, too; empty directory deletion is intentionally not propagated.
        for src, dst in ((a, b), (b, a)) if p['mode'] == 'two-way' else ((a, b),):
            for folder, dirs, files in os.walk(src, followlinks=False):
                (dst / Path(folder).relative_to(src)).mkdir(parents=True, exist_ok=True)
        atomic_json(state, {'a': sa, 'b': sb})
        return f'{count} file change(s)'

    def backup(self, p, a, b):
        scan(a)
        # Namespaced by pairing ID so retention never touches unrelated backups.
        base = b / ('Toy_Sync-' + p['id'])
        base.mkdir(exist_ok=True)
        stamp = time.strftime('%Y-%m-%d_%H-%M-%S') + '-' + str(time.time_ns())
        pending = base / ('.partial-' + stamp)
        try:
            shutil.copytree(a, pending, symlinks=True)
            # Do not publish a snapshot containing links or unstable files.
            before, after = scan(a), scan(pending)
            if before != after:
                raise RuntimeError('Source changed during backup; snapshot discarded and will retry.')
            pending.rename(base / stamp)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
        snapshots = sorted(x for x in base.iterdir() if x.is_dir() and not x.name.startswith('.'))
        for old in snapshots[:-p['keep']]:
            shutil.rmtree(old)
        return f'Backup created; keeping {p["keep"]} snapshot(s)'
