#!/usr/bin/env python3
"""Install per-user launchers; optionally import and retire the old Trilium setup."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
from toy_sync.app import desktop_text, AUTOSTART
from toy_sync.engine import CONFIG, DATA, HOME, atomic_json, pair, Journal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--migrate-trilium', action='store_true')
    args = parser.parse_args()
    CONFIG.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    config = CONFIG / 'settings.json'
    if not config.exists():
        pairs = []
        if args.migrate_trilium:
            pairs = [
                pair('Trilium notes', HOME / '.local/share/trilium-data', HOME / 'MEGA/Notes/trilium-data'),
                pair('Mochi cards', HOME / '.config/mochi', HOME / 'MEGA/Notes/Mochi'),
                pair('Obsidian vault', HOME / 'Desktop/Obsidian20xx', HOME / 'MEGA/Obsidian20xx'),
                pair('MEGA backup', HOME / 'MEGA', HOME / 'Documents/Backup', 'backup', 'interval', 10, 3),
            ]
        atomic_json(config, {'pairs': pairs, 'sync_login': True})
    if args.migrate_trilium:
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'trilium-sync.timer'], check=False)
        subprocess.run(['systemctl', '--user', 'stop', 'trilium-sync.service'], check=False)
        archive = DATA / ('retired-trilium-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        paths = [HOME / '.config/systemd/user/trilium-sync.service',
                 HOME / '.config/systemd/user/trilium-sync.timer',
                 HOME / '.local/bin/sync-trilium.sh',
                 HOME / '.local/share/applications/trilium-sync.desktop',
                 HOME / '.local/share/applications/sync-trilium.desktop']
        for src in paths:
            if src.exists():
                dst = archive / src.relative_to(HOME)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, dst)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        Journal().add('Imported the Trilium, Mochi, Obsidian and MEGA backup pairings; retired the old Trilium timer and launchers.')
    applications = HOME / '.local/share/applications'
    applications.mkdir(parents=True, exist_ok=True)
    subprocess_result = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, check=False)
    desktop = Path(subprocess_result.stdout.strip()) if subprocess_result.returncode == 0 else HOME / 'Desktop'
    desktop.mkdir(parents=True, exist_ok=True)
    for path in (applications / 'toy-sync.desktop', desktop / 'Toy_Sync.desktop'):
        path.write_text(desktop_text())
        path.chmod(0o755)
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(desktop_text(True))
    if shutil.which('kbuildsycoca6'):
        subprocess.run(['kbuildsycoca6', '--noincremental'], capture_output=True)
    print(f'Toy_Sync installed. Desktop launcher: {desktop / "Toy_Sync.desktop"}')
    print(f'Configuration: {config}')


if __name__ == '__main__':
    main()
