"""Exercise real inotify, worker threads and Qt controls in isolated temporary folders."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    os.environ['XDG_CONFIG_HOME'] = str(root / 'config')
    os.environ['XDG_DATA_HOME'] = str(root / 'data')
    from argparse import Namespace
    from PySide6.QtWidgets import QApplication
    from toy_sync.app import Window, PairDialog
    from toy_sync.engine import CONFIG, atomic_json, pair
    a, b = root / 'a', root / 'b'
    a.mkdir()
    b.mkdir()
    p = pair('Smoke test', a, b)
    atomic_json(CONFIG / 'settings.json', {'pairs': [p], 'sync_login': False})
    app = QApplication([])
    w = Window(Namespace(login=False))
    w.show()
    def until(condition, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if condition():
                return
            time.sleep(.02)
        raise AssertionError('Timed out waiting for background sync')
    try:
        # Check the delay without waiting, including new events and continuous activity.
        pid = p['id']
        with patch('toy_sync.app.time.monotonic') as clock, \
                patch.object(w, 'enqueue') as enqueue, patch.object(w, 'start_next'):
            clock.return_value = 100
            w.mark_changed(pid)
            clock.return_value = 114
            w.tick()
            enqueue.assert_not_called()
            w.mark_changed(pid)
            clock.return_value = 128
            w.tick()
            enqueue.assert_not_called()
            clock.return_value = 129
            w.tick()
            enqueue.assert_called_once_with(pid)
            enqueue.reset_mock()
            clock.return_value = 200
            w.mark_changed(pid)
            clock.return_value = 259
            w.mark_changed(pid)
            w.tick()
            enqueue.assert_not_called()
            clock.return_value = 260
            w.tick()
            enqueue.assert_called_once_with(pid)
            enqueue.reset_mock()
            clock.return_value = 300
            w.mark_changed(pid)
            w.sync_all()
            enqueue.assert_called_once_with(pid)
        w.pending.clear()
        d = PairDialog(w, p)
        assert not d.keep.isEnabled()
        d.mode.setCurrentIndex(2)
        assert d.keep.isEnabled()
        d.schedule.setCurrentIndex(1)
        assert d.minutes.isEnabled()
        d.reject()
        (a / 'watched.txt').write_text('hello from inotify')
        until(lambda: (b / 'watched.txt').exists() and w.worker is None)
        assert (b / 'watched.txt').read_text() == 'hello from inotify'
        (b / 'watched.txt').write_text('edited at destination')
        until(lambda: (a / 'watched.txt').read_text() == 'edited at destination' and w.worker is None)
        (a / 'watched.txt').unlink()
        until(lambda: not (b / 'watched.txt').exists() and w.worker is None)
        w.config['pairs'][0]['schedule'] = 'interval'
        w.pending.clear()
        w.rewatch()
        (a / 'interval.txt').write_text('interval works')
        w.last_run[p['id']] = time.monotonic() - 61 * p['minutes']
        w.tick()
        until(lambda: (b / 'interval.txt').exists() and w.worker is None)
        (a / 'manual.txt').write_text('manual works')
        w.sync_all()
        until(lambda: (b / 'manual.txt').exists() and w.worker is None)
        w.refresh_logs()
        assert 'sync started' in w.logs.toPlainText()
        print('GUI, live folder watching, two-way edits/deletion, interval, manual sync and logs passed')
    finally:
        if w.worker:
            w.worker.wait()
            app.processEvents()
        w.quit_app()
