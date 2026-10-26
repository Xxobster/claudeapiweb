"""Pure text edits of the job board (WP11)."""

import pytest

from cpm.locks import boardwriter as bw
from cpm.locks.adapters import jobboard

BOARD = """# Job board

| Job | Owner | Files | Status | Since |
|---|---|---|---|---|
| Voice pass | desktop | `Assets/Audio/` | active | 2026-10-01 |
| Old thing | desktop | `docs/old.md` | done | 2026-09-01 |

Notes below the table stay as they are.
"""


def claim(text=BOARD, files=("Assets/Game/UI/",), task_id=7, title="Menu polish"):
    return bw.add_claim(text, task_id=task_id, title=title, files=list(files), today="2026-10-26")


def test_claim_appends_one_row_after_the_table_and_changes_nothing_else():
    e = claim()
    assert e.changed
    old, new = BOARD.split("\n"), e.text.split("\n")
    assert len(new) == len(old) + 1
    assert [ln for ln in new if ln not in old] == [e.row]
    assert e.row == "| CPM #7: Menu polish | cpm | `Assets/Game/UI/` | claimed | 2026-10-26 |"
    assert (
        new.index(e.row)
        == old.index("| Old thing | desktop | `docs/old.md` | done | 2026-09-01 |") + 1
    )
    assert e.text.endswith("Notes below the table stay as they are.\n")


def test_claim_is_idempotent_and_the_adapter_ignores_cpm_rows():
    first = claim()
    again = claim(first.text)
    assert not again.changed and again.text == first.text
    # CPM's own row never blocks CPM, and the other owner's row is still seen
    assert {c.label for c in jobboard.parse(first.text)} == {"Assets/Audio/"}


def test_claim_refuses_overlap_with_another_owners_active_claim():
    with pytest.raises(bw.BoardError, match="overlaps an active claim"):
        claim(files=("Assets/Audio/voice/line1.wav",))
    claim(files=("docs/old.md",))  # the row is done: free


def test_claim_after_release_can_be_made_again():
    released = bw.release_claim(claim().text, task_id=7)
    again = claim(released.text)
    assert again.changed and again.text.count("CPM #7:") == 2


def test_release_marks_the_row_and_keeps_it():
    e = bw.release_claim(claim().text, task_id=7)
    assert e.changed and "| released |" in e.row
    assert jobboard.parse(e.text)[0].label == "Assets/Audio/"
    again = bw.release_claim(e.text, task_id=7)
    assert not again.changed and again.text == e.text


def test_release_without_a_row_is_refused():
    with pytest.raises(bw.BoardError, match="no CPM claim row"):
        bw.release_claim(BOARD, task_id=7)
    other = claim(task_id=8).text
    with pytest.raises(bw.BoardError, match="no CPM claim row"):
        bw.release_claim(other, task_id=7)  # "CPM #8:" must not match #7 (or #70)


def test_task_70_is_not_task_7():
    text = claim(task_id=70).text
    with pytest.raises(bw.BoardError):
        bw.release_claim(text, task_id=7)
    assert claim(text, task_id=7, files=("a/b.txt",)).changed


def test_no_suitable_table_fails_closed():
    for text in ("", "# only prose\n", "| a | b |\n|---|---|\n| 1 | 2 |\n"):
        with pytest.raises(bw.BoardError, match="no table"):
            claim(text)


@pytest.mark.parametrize(
    "bad", ["", "/etc/passwd", "C:/x", "../up", "a|b", "a`b", "a\nb", "a\x00b"]
)
def test_unsafe_scope_paths_are_refused(bad):
    with pytest.raises(bw.BoardError):
        claim(files=(bad,))


def test_no_scope_is_refused():
    with pytest.raises(bw.BoardError, match="no declared scope"):
        claim(files=())


def test_title_cannot_break_the_table_or_inject_cells():
    e = claim(title="x | cpm | `evil` | released\nnew row")
    assert e.row.count("|") == BOARD.split("\n")[2].count("|")
    assert "\n" not in e.row and "`evil`" not in e.row


def test_paths_with_spaces_and_unicode_round_trip_through_the_parser():
    e = claim(files=("Assets/Mon Dossier/é.txt",), task_id=3)
    assert "`Assets/Mon Dossier/é.txt`" in e.row


def test_crlf_is_preserved():
    raw = BOARD.replace("\n", "\r\n").encode()
    text, eol = bw.decode(raw)
    assert eol == "\r\n" and "\r" not in text
    out = bw.encode(claim(text).text, eol)
    assert out.count(b"\r\n") == out.count(b"\n")


def test_invalid_utf8_is_refused():
    with pytest.raises(bw.BoardError, match="UTF-8"):
        bw.decode(b"| a |\xff\xfe")
