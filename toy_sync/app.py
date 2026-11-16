import argparse
import copy
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QLockFile, QStandardPaths
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QScrollArea, QDialog, QFormLayout, QLineEdit,
    QFileDialog, QComboBox, QSpinBox, QDialogButtonBox, QMessageBox, QCheckBox,
    QSystemTrayIcon, QMenu, QPlainTextEdit, QTabWidget)
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from .engine import CONFIG, DATA, HOME, Engine, Journal, atomic_json, pair, validate
import json

APP_ROOT = Path(__file__).resolve().parent.parent
AUTOSTART = Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config')) / 'autostart/toy-sync.desktop'


def icon(busy=False, angle=0, error=False):
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor('#172a46'))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 18, 18)
    p.translate(32, 32)
    p.rotate(angle)
    p.setPen(QPen(QColor('#f3ba76' if error else '#74e6c2' if busy else '#a8bcff'), 5, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(-18, -18, 36, 36, 25 * 16, 135 * 16)
    p.drawArc(-18, -18, 36, 36, 205 * 16, 135 * 16)
    p.drawLine(-17, -7, -21, -16)
    p.drawLine(-17, -7, -8, -11)
    p.drawLine(17, 7, 21, 16)
    p.drawLine(17, 7, 8, 11)
    p.end()
    return QIcon(pix)


def desktop_text(login=False):
    return ('[Desktop Entry]\nType=Application\nName=Toy_Sync\n'
            'Comment=Keep your folders together\n'
            f'Exec="{APP_ROOT / "run.sh"}"' + (' --tray --login' if login else '') + '\n'
            f'Icon={APP_ROOT / "assets/toy-sync.svg"}\nTerminal=false\nCategories=Utility;FileTools;\n'
            'StartupNotify=false\nX-GNOME-Autostart-enabled=true\n')


class PairDialog(QDialog):
    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.setWindowTitle('Pair settings' if existing else 'Add folder pairing')
        self.setMinimumWidth(650)
        self.value = copy.deepcopy(existing) if existing else pair('', '', '')
        layout = QVBoxLayout(self)
        title = QLabel('Give your folders a connection')
        title.setObjectName('section')
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(18)
        self.name = QLineEdit(self.value['name'])
        form.addRow('Pair name', self.name)
        self.paths = {}
        for key, label in [('source', 'Source folder'), ('target', 'Destination folder')]:
            row = QHBoxLayout()
            edit = QLineEdit(self.value[key])
            edit.setPlaceholderText('Choose an existing folder…')
            button = QPushButton('Browse…')
            button.clicked.connect(lambda checked=False, e=edit: self.browse(e))
            row.addWidget(edit, 1)
            row.addWidget(button)
            form.addRow(label, row)
            self.paths[key] = edit
        self.mode = QComboBox()
        self.mode.addItems(['Two-way', 'One-way', 'Backup'])
        self.mode.setCurrentIndex(['two-way', 'one-way', 'backup'].index(self.value['mode']))
        form.addRow('Sync mode', self.mode)
        self.explain = QLabel()
        self.explain.setWordWrap(True)
        form.addRow('', self.explain)
        self.keep = QSpinBox()
        self.keep.setRange(1, 999)
        self.keep.setValue(self.value['keep'])
        form.addRow('Backups to keep', self.keep)
        self.schedule = QComboBox()
        self.schedule.addItems(['When folders change', 'At an interval'])
        self.schedule.setCurrentIndex(0 if self.value['schedule'] == 'changes' else 1)
        form.addRow('Run automatically', self.schedule)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 10080)
        self.minutes.setSuffix(' minutes')
        self.minutes.setValue(self.value['minutes'])
        form.addRow('Interval', self.minutes)
        self.enabled = QCheckBox('Enable automatic syncing for this pair')
        self.enabled.setChecked(self.value['enabled'])
        form.addRow('', self.enabled)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setStyleSheet('color: #ffbb98')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self.update_fields)
        self.schedule.currentIndexChanged.connect(self.update_fields)
        self.update_fields()

    def browse(self, edit):
        folder = QFileDialog.getExistingDirectory(self, 'Choose folder', edit.text() or str(HOME))
        if folder:
            edit.setText(folder)

    def update_fields(self):
        self.keep.setEnabled(self.mode.currentIndex() == 2)
        self.minutes.setEnabled(self.schedule.currentIndex() == 1)
        self.explain.setText([
            'Changes and deletions travel both ways. Conflicting versions are kept on both sides. Empty folder deletions are not propagated.',
            'Copy new and changed files from source to destination. Destination-only files are kept. Source versions replace matching destination files.',
            'Create complete, dated snapshots of the source inside the destination. Retention applies only to snapshots created by this pairing.'
        ][self.mode.currentIndex()])

    def save(self):
        try:
            if not all(e.text().strip() for e in self.paths.values()):
                raise ValueError('Choose both folders.')
            self.value.update(name=self.name.text().strip() or Path(self.paths['source'].text()).name,
                mode=['two-way', 'one-way', 'backup'][self.mode.currentIndex()],
                schedule=['changes', 'interval'][self.schedule.currentIndex()],
                keep=self.keep.value(), minutes=self.minutes.value(), enabled=self.enabled.isChecked(),
                **{k: e.text().strip() for k, e in self.paths.items()})
            validate(self.value, self.parent().config['pairs'])
            if not all(Path(self.value[k]).is_dir() for k in ('source', 'target')):
                raise ValueError('Choose existing folders. Create a destination with the folder chooser if needed.')
            self.accept()
        except ValueError as e:
            self.error.setText(str(e))


class Watcher(FileSystemEventHandler):
    def __init__(self, signal, pair_id):
        self.signal, self.pair_id = signal, pair_id

    def on_any_event(self, event):
        if event.event_type in ('opened', 'closed_no_write'):
            return
        if any(Path(p).name.startswith('.toy-sync-') for p in (event.src_path, getattr(event, 'dest_path', ''))):
            return
        self.signal.emit(self.pair_id)


class Worker(QThread):
    result = Signal(str, str, bool)

    def __init__(self, p, journal):
        super().__init__()
        self.p = copy.deepcopy(p)
        self.journal = journal

    def run(self):
        name = self.p['name']
        try:
            self.journal.add(f'{name}: sync started')
            detail = Engine(log=self.journal.add).run(self.p)
            self.journal.add(f'{name}: {detail}')
            self.result.emit(self.p['id'], detail, True)
        except Exception as e:
            self.journal.add(f'{name}: {e}', 'ERROR')
            self.result.emit(self.p['id'], str(e), False)


class Window(QMainWindow):
    changed = Signal(str)

    def __init__(self, args):
        super().__init__()
        self.setWindowTitle('Toy_Sync')
        self.setWindowIcon(icon())
        self.resize(1060, 760)
        self.setMinimumSize(800, 560)
        self.config_path = CONFIG / 'settings.json'
        self.config = json.loads(self.config_path.read_text()) if self.config_path.exists() else {'pairs': [], 'sync_login': False}
        self.journal = Journal()
        self.queue = []
        self.worker = None
        self.running_id = None
        self.pending = {}
        self.last_run = {p['id']: time.monotonic() for p in self.config['pairs']}
        self.statuses = {}
        self.failed = set()
        self.observer = None
        self.angle = 0
        self.quitting = False
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(36, 30, 36, 22)
        layout.setSpacing(20)
        header = QHBoxLayout()
        header.addStretch()
        self.sync_button = QPushButton('↻  Sync Now')
        self.sync_button.setObjectName('primary')
        self.sync_button.clicked.connect(self.sync_all)
        header.addWidget(self.sync_button)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 20, 0, 0)
        toolbar = QHBoxLayout()
        self.summary = QLabel()
        toolbar.addWidget(self.summary, 1)
        self.add_button = QPushButton('+  Add pairing')
        self.add_button.clicked.connect(lambda: self.edit_pair())
        toolbar.addWidget(self.add_button)
        pl.addLayout(toolbar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.cards = QWidget()
        self.card_layout = QVBoxLayout(self.cards)
        self.card_layout.setContentsMargins(0, 0, 6, 0)
        self.card_layout.setSpacing(14)
        scroll.setWidget(self.cards)
        pl.addWidget(scroll)
        self.tabs.addTab(page, 'Folder pairings')
        activity = QWidget()
        al = QVBoxLayout(activity)
        al.addWidget(QLabel('Activity from the last 24 hours · older entries are removed automatically'))
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        al.addWidget(self.logs)
        self.tabs.addTab(activity, 'Activity log')
        settings = QWidget()
        sl = QVBoxLayout(settings)
        sl.setSpacing(20)
        lab = QLabel('Make yourself at home')
        lab.setObjectName('section')
        sl.addWidget(lab)
        self.launch = QCheckBox('Launch at login (start quietly in the system tray)')
        self.launch.setChecked(AUTOSTART.exists())
        self.launch.toggled.connect(self.set_launch)
        sl.addWidget(self.launch)
        self.login_sync = QCheckBox('Sync at login')
        self.login_sync.setChecked(self.config.get('sync_login', False))
        self.login_sync.toggled.connect(self.set_login_sync)
        sl.addWidget(self.login_sync)
        note = QLabel('Sync at login runs enabled pairs when Toy_Sync is launched by your desktop session.\n'
                      'Enable Launch at login to use it. Closing or minimizing this window keeps syncing\n'
                      'in the tray. Choose Quit in the tray menu to stop Toy_Sync.\n\n'
                      'Folder changes are grouped after 3 quiet seconds, with a 30-second maximum wait.\n'
                      'No desktop notifications are sent. The tray icon turns mint and rotates while syncing.')
        note.setWordWrap(True)
        sl.addWidget(note)
        sl.addStretch()
        self.tabs.addTab(settings, 'Preferences')
        self.footer = QLabel('●  Ready · working quietly in the background')
        self.footer.setObjectName('muted')
        layout.addWidget(self.footer)
        self.tray = QSystemTrayIcon(icon(), self)
        self.tray.setToolTip('Toy_Sync · Ready')
        menu = QMenu()
        menu.addAction('Open Toy_Sync', self.reveal)
        self.tray_sync = menu.addAction('Sync Now', self.sync_all)
        menu.addSeparator()
        self.quit_action = menu.addAction('Quit Toy_Sync', self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal() if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()
        self.changed.connect(self.mark_changed)
        self.rebuild()
        self.rewatch()
        self.refresh_logs()
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(1000)
        self.animation = QTimer(self)
        self.animation.timeout.connect(self.animate)
        self.animation.start(110)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start(4000)
        self.prune_timer = QTimer(self)
        self.prune_timer.timeout.connect(self.journal.prune)
        self.prune_timer.start(60000)
        if args.login and self.config.get('sync_login'):
            QTimer.singleShot(1500, self.sync_all)

    def persist(self):
        atomic_json(self.config_path, self.config)

    def set_launch(self, checked):
        try:
            if checked:
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART.write_text(desktop_text(True))
            else:
                AUTOSTART.unlink(missing_ok=True)
        except OSError as e:
            QMessageBox.warning(self, 'Could not save preference', str(e))

    def set_login_sync(self, checked):
        self.config['sync_login'] = checked
        self.persist()

    def rebuild(self):
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        pairs = self.config['pairs']
        self.summary.setText(f'{len(pairs)} connections · {sum(p["enabled"] for p in pairs)} enabled')
        if not pairs:
            empty = QLabel('Your folders, together.\n\nAdd a pairing to start syncing.')
            empty.setAlignment(Qt.AlignCenter)
            self.card_layout.addWidget(empty)
        for p in pairs:
            card = QFrame()
            card.setObjectName('card')
            cl = QVBoxLayout(card)
            cl.setContentsMargins(22, 18, 22, 18)
            row = QHBoxLayout()
            label = QLabel(p['name'])
            label.setObjectName('section')
            row.addWidget(label, 1)
            badge = QLabel({'two-way': 'TWO-WAY', 'one-way': 'ONE-WAY', 'backup': 'BACKUP'}[p['mode']])
            badge.setObjectName('badge')
            row.addWidget(badge)
            gear = QPushButton('⚙')
            gear.setToolTip('Settings for ' + p['name'])
            gear.setAccessibleName('Settings for ' + p['name'])
            gear.setFixedWidth(40)
            gear.clicked.connect(lambda checked=False, value=p: self.edit_pair(value))
            gear.setEnabled(self.worker is None)
            row.addWidget(gear)
            delete = QPushButton('Remove')
            delete.clicked.connect(lambda checked=False, value=p: self.remove_pair(value))
            delete.setEnabled(self.worker is None)
            row.addWidget(delete)
            cl.addLayout(row)
            paths = QLabel(f'{p["source"]}\n{"↔" if p["mode"] == "two-way" else "↓"}\n{p["target"]}')
            paths.setTextFormat(Qt.PlainText)
            paths.setWordWrap(True)
            paths.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cl.addWidget(paths)
            schedule = 'On folder changes' if p['schedule'] == 'changes' else f'Every {p["minutes"]} minutes'
            if p['mode'] == 'backup':
                schedule += f' · Keep {p["keep"]} backups'
            status = self.statuses.get(p['id'], 'Ready' if p['enabled'] else 'Automatic sync paused')
            detail = QLabel(schedule + '   ·   ' + status)
            detail.setWordWrap(True)
            detail.setObjectName('muted')
            cl.addWidget(detail)
            self.card_layout.addWidget(card)
        self.card_layout.addStretch()

    def edit_pair(self, p=None):
        dialog = PairDialog(self, p)
        if dialog.exec():
            if p:
                self.config['pairs'] = [dialog.value if x['id'] == p['id'] else x for x in self.config['pairs']]
                # Changing paths or mode invalidates the old deletion baseline.
                if any(p[k] != dialog.value[k] for k in ('source', 'target', 'mode')):
                    (DATA / (p['id'] + '.json')).unlink(missing_ok=True)
            else:
                self.config['pairs'].append(dialog.value)
            self.last_run[dialog.value['id']] = time.monotonic()
            self.persist()
            self.rebuild()
            self.rewatch()
            self.journal.add(f'{dialog.value["name"]}: pairing settings saved')

    def remove_pair(self, p):
        if QMessageBox.question(self, 'Remove pairing?', f'Remove “{p["name"]}”?\nYour folders and existing backups will be kept.') != QMessageBox.Yes:
            return
        self.config['pairs'] = [x for x in self.config['pairs'] if x['id'] != p['id']]
        self.queue = [x for x in self.queue if x != p['id']]
        self.pending.pop(p['id'], None)
        self.failed.discard(p['id'])
        self.persist()
        (DATA / (p['id'] + '.json')).unlink(missing_ok=True)
        self.journal.add(f'{p["name"]}: pairing removed; files kept')
        self.rebuild()
        self.rewatch()

    def rewatch(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
        self.observer = Observer()
        for p in self.config['pairs']:
            if not p['enabled'] or p['schedule'] != 'changes':
                continue
            roots = [p['source']] + ([p['target']] if p['mode'] == 'two-way' else [])
            for root in roots:
                try:
                    self.observer.schedule(Watcher(self.changed, p['id']), root, recursive=True)
                except OSError as e:
                    self.journal.add(f'{p["name"]}: could not watch {root}: {e}', 'ERROR')
                    self.failed.add(p['id'])
        try:
            self.observer.start()
        except (OSError, RuntimeError) as e:
            self.journal.add(f'Folder watching unavailable: {e}. Interval and manual sync remain available.', 'ERROR')

    def mark_changed(self, pid):
        now = time.monotonic()
        first = self.pending.get(pid, (now, now))[0]
        self.pending[pid] = (first, now)

    def tick(self):
        now = time.monotonic()
        for p in self.config['pairs']:
            if not p['enabled']:
                continue
            pid = p['id']
            changed = pid in self.pending and (now - self.pending[pid][1] >= 3 or now - self.pending[pid][0] >= 30)
            interval = p['schedule'] == 'interval' and now - self.last_run.get(pid, now) >= p['minutes'] * 60
            retry = pid in self.failed and now - self.last_run.get(pid, now) >= 60
            if (changed or interval or retry) and pid != self.running_id:
                self.enqueue(pid)
                self.pending.pop(pid, None)
        self.start_next()

    def enqueue(self, pid):
        if pid not in self.queue:
            self.queue.append(pid)

    def sync_all(self):
        for p in self.config['pairs']:
            if p['enabled']:
                self.enqueue(p['id'])
        self.start_next()

    def start_next(self):
        if self.worker or not self.queue:
            return
        pid = self.queue.pop(0)
        p = next((x for x in self.config['pairs'] if x['id'] == pid and x['enabled']), None)
        if not p:
            return
        self.running_id = pid
        self.pending.pop(pid, None)
        self.statuses[pid] = 'Syncing…'
        self.footer.setText(f'↻  Syncing {p["name"]}…')
        self.tray.setToolTip(f'Toy_Sync · Syncing {p["name"]}')
        self.add_button.setEnabled(False)
        self.sync_button.setEnabled(False)
        self.tray_sync.setEnabled(False)
        self.worker = Worker(p, self.journal)
        self.worker.result.connect(self.completed)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()
        self.rebuild()

    def completed(self, pid, detail, ok):
        self.statuses[pid] = time.strftime('%H:%M') + ' · ' + detail
        self.last_run[pid] = time.monotonic()
        if ok:
            self.failed.discard(pid)
        else:
            self.failed.add(pid)

    def worker_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.running_id = None
        self.add_button.setEnabled(True)
        self.sync_button.setEnabled(True)
        self.tray_sync.setEnabled(True)
        self.footer.setText('●  Attention needed · see Activity log' if self.failed else '●  Up to date · working quietly in the background')
        self.tray.setToolTip('Toy_Sync · Attention needed' if self.failed else 'Toy_Sync · Ready')
        self.rebuild()
        self.refresh_logs()
        if self.quitting:
            self.finish_quit()
        else:
            self.start_next()

    def animate(self):
        self.angle = (self.angle + 14) % 360 if self.worker else 0
        self.tray.setIcon(icon(bool(self.worker), self.angle, bool(self.failed) and not self.worker))

    def refresh_logs(self):
        text = '\n'.join(f'{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))}  {level:8}  {message}' for at, level, message in self.journal.rows())
        if self.logs.toPlainText() != text:
            self.logs.setPlainText(text or 'No activity yet. Sync events will appear here.')

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
        else:
            event.ignore()
            self.quit_app()

    def changeEvent(self, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.WindowStateChange and self.isMinimized() and QSystemTrayIcon.isSystemTrayAvailable():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def quit_app(self):
        self.quitting = True
        self.queue.clear()
        self.tick_timer.stop()
        if self.worker:
            self.footer.setText('Finishing the current sync before quitting…')
            self.quit_action.setEnabled(False)
        else:
            self.finish_quit()

    def finish_quit(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
        self.tray.hide()
        QApplication.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--login', action='store_true')
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName('Toy_Sync')
    app.setDesktopFileName('toy-sync')
    app.setQuitOnLastWindowClosed(False)
    CONFIG.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(CONFIG / 'instance.lock'))
    if not lock.tryLock(100):
        if not args.login:
            sock = QLocalSocket()
            sock.connectToServer('toy-sync-' + str(os.getuid()))
            if sock.waitForConnected(1500):
                sock.write(b'show')
                sock.waitForBytesWritten(1000)
        return
    app.setStyle('Fusion')
    app.setStyleSheet('''
        QWidget { background: #101b2d; color: #e5ecf9; font-family: "Noto Sans"; font-size: 14px; }
        QLabel#section { font-size: 18px; font-weight: 600; }
        QLabel#muted { color: #9fadc5; font-size: 12px; }
        QLabel#badge { color: #8ee8cd; background: #203e45; border-radius: 7px; padding: 5px 10px; font-size: 11px; font-weight: 600; }
        QFrame#card { background: #1a2940; border: 1px solid #2b3e59; border-radius: 14px; }
        QFrame#card QLabel { background: transparent; border: none; }
        QPushButton { background: #283b59; border: 1px solid #3c5275; border-radius: 8px; padding: 9px 15px; font-weight: 600; }
        QPushButton:hover { background: #354d70; border-color: #819de5; }
        QPushButton:disabled { color: #8390a7; background: #202e43; }
        QPushButton#primary { background: #a8bcff; color: #13213c; border: none; padding: 14px 24px; }
        QPushButton#primary:hover { background: #c0ceff; }
        QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #18263c; border: 1px solid #3a4f70; border-radius: 7px; padding: 8px; selection-background-color: #50669a; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { padding: 12px 22px; color: #a3b2cc; border-bottom: 2px solid #263852; }
        QTabBar::tab:selected { color: #bbcbff; border-bottom: 2px solid #a8bcff; }
        QScrollBar:vertical { width: 9px; background: #101b2d; }
        QScrollBar::handle:vertical { background: #3a4e6d; border-radius: 4px; min-height: 24px; }
        QCheckBox { spacing: 12px; }
        QCheckBox::indicator { width: 20px; height: 20px; }
        QToolTip { background: #283b59; color: white; border: 1px solid #526a91; }
    ''')
    window = Window(args)
    server = QLocalServer()
    QLocalServer.removeServer('toy-sync-' + str(os.getuid()))
    server.listen('toy-sync-' + str(os.getuid()))
    def received():
        socket = server.nextPendingConnection()
        if socket:
            socket.deleteLater()
        window.reveal()
    server.newConnection.connect(received)
    if not args.tray or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    return app.exec()
