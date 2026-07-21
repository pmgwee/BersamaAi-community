"""Safe live backup of bersama.db using SQLite's online-backup API.

`Connection.backup()` is WAL-safe — it can run while the bot is actively writing
(leveling XP etc.) without corruption or locking the bot out. Keeps the last KEEP
daily snapshots, prunes older ones.

Run from cron, e.g. daily at 04:00 (crontab -e):
    0 4 * * * cd $HOME/BersamaAi-community/bersama-bot && .venv/bin/python backup_db.py >> backups.log 2>&1

Restore: copy a snapshot back over bersama.db while the bot is stopped:
    sudo systemctl stop bersama && cp backups/bersama-20260721-040000.db bersama.db && sudo systemctl start bersama
"""
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "bersama.db"
BACKUP_DIR = HERE / "backups"
KEEP = 14  # daily snapshots to retain


def main() -> None:
    if not SRC.exists():
        print("no bersama.db yet — nothing to back up")
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"bersama-{stamp}.db"

    src = sqlite3.connect(str(SRC))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # online backup; safe under concurrent writes
    finally:
        dst.close()
        src.close()
    print(f"{time.strftime('%F %T')} backed up -> {dest.name} ({dest.stat().st_size} bytes)")

    # Prune everything older than the most recent KEEP snapshots.
    snapshots = sorted(BACKUP_DIR.glob("bersama-*.db"))
    for old in snapshots[:-KEEP]:
        old.unlink()
        print(f"  pruned {old.name}")


if __name__ == "__main__":
    main()
