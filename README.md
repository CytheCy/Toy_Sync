# Toy_Sync

A local folder sync app for Fedora KDE, written in Python and Qt 6 (PySide6).
Runs quietly in the tray, with no desktop notifications. The tray icon rotates
and turns mint while a sync runs; amber means an error needs attention.

## Run and install

On this PC, PySide6, watchdog and lsof are already installed for `/usr/bin/python3`.

```sh
./run.sh
python3 install.py                    # application menu, desktop and login launcher
python3 install.py --migrate-trilium  # additionally import and retire the old sync setup
```

For another Fedora installation, install Python 3, lsof, PySide6 and watchdog
(e.g. `python3 -m pip install --user -r requirements.txt` in a Python environment
that permits user packages). `run.sh` uses `/usr/bin/python3`. Keep this project
at its installed path, or rerun the installer after moving it.

Closing or minimizing the window hides it to the tray. Launching again brings the
existing window forward. The tray menu offers Open, Sync Now and Quit. Quit waits
for the current sync to finish. The app runs while the desktop session is active.

## Pairings

Add pairing selects two existing folders. The gear button changes its settings;
Remove deletes only the pairing and its state, leaving files and backups intact.
Sync Now runs all enabled pairs. Disable a pairing in its settings to pause it.

* **Two-way:** merges both folders, then propagates edits and file deletions.
  Concurrent edits preserve both versions on both sides in `.conflict-*` files.
  An edit wins over a concurrent deletion. On first sync, differing files also
  preserve both versions. Empty folders are copied but their deletion is not
  propagated. State persists across restarts; changing a pair's paths or mode
  resets its deletion baseline.
* **One-way:** source replaces matching destination files. New source files are
  copied; destination-only files stay. This mode does not mirror deletions.
* **Backup:** makes full dated snapshots in `destination/Toy_Sync-<pair ID>/`.
  Only completed snapshots belonging to that pairing count toward retention.
  Existing backups made by the retired shell script are left alone. Backup
  snapshots require space for a complete new copy before old copies are pruned.

Each pairing runs on folder changes or at a chosen interval in minutes. Folder
watching uses Linux inotify through watchdog; events are grouped after 15 quiet
seconds with a 60-second maximum wait. Interval timing starts when the app starts
and resets on completion; it is not a wall-clock schedule. A new pairing waits
for a change, its interval, or Sync Now. Failed runs retry after a minute.
Login launch and login sync are separate preferences; both must be checked for
an immediate sync at login. Manual launch does not automatically sync.

## Data handling

Copies use temporary files beside the destination and atomic replacement. File
content, modes and timestamps are copied with Python `copy2`; symlinks and special
files stop a pairing with a logged error. Folder permissions, ACLs, hardlinks and
ownership are not replicated. Size and nanosecond mtime identify changes; differing
metadata with equal size is checked by SHA-256. Tools which alter file contents
while preserving size and mtime can evade change detection. Do not edit both roots
with another sync tool simultaneously.

Missing roots stop the pairing rather than treating the missing root as deletions.
An existing but empty root is a real deletion after the first baseline. Avoid
using a mount point that remains as an empty directory when its disk is unplugged.
Writable roots may not overlap other pairs, though a backup can read a synced root.
Trilium pairs skip while `document.db` is open, preserving the old script's lsof
safeguard. The error identifies the database path and the process names and PIDs
holding it open. This can include a cloud sync client briefly reading the destination
after Trilium has closed; failed runs retry after a minute. Close database applications
before syncing their data. This is a file sync tool, not a transactional database
backup utility.

Configuration is in `~/.config/toy-sync/settings.json`, state and activity are in
`~/.local/share/toy-sync/`. Old Trilium service files and installed launchers are
archived there under `retired-trilium-*`. The original scripts in the MEGA source
directory remain as reference and are no longer scheduled. Box sync is independent.
Activity is kept for 24 hours and pruned on startup and every minute while running.

## Verify

```sh
python3 -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py
```
