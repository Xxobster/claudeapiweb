import sqlite3

import pytest
from sqlalchemy import text

from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.recovery.backup import (
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
)


@pytest.fixture
def db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    url = f"sqlite:///{(data / 'cpm.db').as_posix()}"
    upgrade(url)
    return data, data / "cpm.db"


def marker(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT key FROM settings ORDER BY key")]
    finally:
        con.close()


def put(path, key):
    eng = make_engine(path)
    with eng.begin() as c:
        c.execute(text("INSERT INTO settings(key, value) VALUES (:k, '1')"), {"k": key})
    eng.dispose()


def test_backup_is_consistent_and_restore_brings_it_back(db):
    data, path = db
    put(path, "before")
    b = create_backup(path, data)
    put(path, "after")
    assert marker(b) == ["before"]
    saved = restore_backup(b, path)
    assert marker(path) == ["before"]
    assert marker(saved) == ["after", "before"]  # the replaced database is kept


def test_backups_are_pruned_oldest_first(db):
    data, path = db
    made = [create_backup(path, data, keep=3) for _ in range(5)]
    left = list_backups(data)
    assert len(left) == 3 and left[0] == made[-1]


def test_restore_refuses_a_corrupt_or_foreign_file(db, tmp_path):
    data, path = db
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(BackupError):
        restore_backup(junk, path)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x(a)").connection.close()
    with pytest.raises(BackupError):
        restore_backup(other, path)
    with pytest.raises(BackupError):
        restore_backup(tmp_path / "missing.db", path)
    assert marker(path) == []  # untouched


def test_list_ignores_foreign_files(db):
    data, path = db
    create_backup(path, data)
    (data / "backups" / "notes.txt").write_text("x")
    (data / "backups" / "cpm-evil.db").write_text("x")
    assert len(list_backups(data)) == 1
