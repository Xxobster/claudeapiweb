"""`python -m cpm.recovery backup | list | restore FILE` (restore only while CPM is stopped)."""

from __future__ import annotations

import sys
from pathlib import Path

from cpm.config import load_settings
from cpm.recovery.backup import BackupError, create_backup, list_backups, restore_backup


def main(argv: list[str]) -> int:
    s = load_settings()
    try:
        if argv[:1] == ["backup"]:
            print(create_backup(s.db_path, s.data_dir))
        elif argv[:1] == ["list"]:
            for p in list_backups(s.data_dir):
                print(p)
        elif len(argv) == 2 and argv[0] == "restore":
            src = Path(argv[1]).resolve()
            if src.parent != (s.data_dir / "backups").resolve():
                print("restore only accepts files from the data/backups folder", file=sys.stderr)
                return 2
            print("previous database kept as", restore_backup(src, s.db_path))
        else:
            print("usage: python -m cpm.recovery backup | list | restore FILE", file=sys.stderr)
            return 2
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
