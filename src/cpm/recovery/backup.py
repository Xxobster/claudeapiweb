"""Consistent SQLite backups (`Connection.backup`, the API behind `sqlite3 .backup`) and restore.

A backup is taken while the database is in use; a restore only happens while CPM is stopped
(`python -m cpm.recovery restore FILE`), after the backup is verified, and the current database is
kept next to it as `cpm.db.before-restore-<stamp>` so a mistaken restore can be undone.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

KEEP_DEFAULT = 10
_NAME = re.compile(r"^cpm-\d{8}T\d{6}Z-\d{3}\.db$")


class BackupError(RuntimeError):
    pass


def backup_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def _check(path: Path) -> None:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise BackupError(f"integrity check failed for {path.name}")
        if con.execute("SELECT count(*) FROM sqlite_master WHERE name='tasks'").fetchone()[0] != 1:
            raise BackupError(f"{path.name} is not a CPM database")
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name} is not a readable SQLite database") from exc
    finally:
        con.close()


def create_backup(db_path: Path, data_dir: Path, keep: int = KEEP_DEFAULT) -> Path:
    """Write `backups/cpm-<UTC stamp>.db`, verify it, prune to the newest `keep` files."""
    dest_dir = backup_dir(data_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # same-second backups sort correctly because the counter is zero-padded and never reused
    same = [int(p.stem.rsplit("-", 1)[1]) for p in list_backups(data_dir) if f"-{stamp}-" in p.name]
    dest = dest_dir / f"cpm-{stamp}-{max(same, default=0) + 1:03d}.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    try:
        _check(dest)
    except BackupError:
        dest.unlink(missing_ok=True)
        raise
    for old in list_backups(data_dir)[max(1, keep) :]:
        old.unlink(missing_ok=True)
    return dest


def list_backups(data_dir: Path) -> list[Path]:
    """Newest first. Only files CPM named itself."""
    d = backup_dir(data_dir)
    if not d.is_dir():
        return []
    return sorted(
        (p for p in d.iterdir() if _NAME.match(p.name)), key=lambda p: p.name, reverse=True
    )


def restore_backup(source: Path, db_path: Path) -> Path:
    """Replace `db_path` by a verified copy of `source`. CPM must not be running."""
    if not source.is_file():
        raise BackupError("backup file not found")
    _check(source)
    saved = db_path.with_name(f"{db_path.name}.before-restore-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    if db_path.exists():
        # not a file copy: in WAL mode recent commits live in the -wal file
        src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        out = sqlite3.connect(saved)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    for ext in ("-wal", "-shm"):  # stale journal files would be replayed onto the restored file
        db_path.with_name(db_path.name + ext).unlink(missing_ok=True)
    tmp = db_path.with_name(db_path.name + ".restoring")
    shutil.copy2(source, tmp)
    tmp.replace(db_path)
    return saved
