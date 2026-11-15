"""The half-added-language guard (G7, ADR 0015 section 9): a code in ``locales.json`` fails CI
until its catalogue, mail tables, ``CHECK`` constraints, picker name and golden files exist.

The proof adds a fake code (``xx``) to a temporary copy of the repository's language files and
wires every piece but one: the guard names exactly that piece. No database, no network.
"""

import json
import shutil
from pathlib import Path

import pytest

from localeguard import GOLDEN, I18N, PIECES, SRC, missing_pieces

REPO = Path(__file__).resolve().parents[2]
FAKE = "xx"
COPIED = (
    I18N,
    SRC / "notify/templates",
    SRC / "auth/mail_texts",
    SRC / "auth/tables.py",
    Path("backend/migrations/versions"),
    GOLDEN,
)


def _copy(dst: Path) -> None:
    for rel in COPIED:
        src = REPO / rel
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(
                src, target, ignore=shutil.ignore_patterns("__pycache__", "node_modules")
            )
        else:
            shutil.copy2(src, target)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} no longer holds {old!r}; update this test"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _widening_migration(root: Path, names: tuple[str, ...]) -> None:
    text = "".join(
        f'# DROP CONSTRAINT {n}; ADD CONSTRAINT {n} CHECK (locale IN ("en", "sv", "{FAKE}"))\n'
        for n in names
    )
    (root / "backend/migrations/versions" / "9999_locale_xx.py").write_text(text, encoding="utf-8")


def _wire(root: Path, piece: str) -> None:
    i18n = root / I18N
    if piece == "catalogue":
        shutil.copytree(i18n / "sv", i18n / FAKE)
        _edit(i18n / "store.ts", "  sv: () =>", f"  {FAKE}: () => import('./{FAKE}'),\n  sv: () =>")
    elif piece == "mail":
        for table in (SRC / "notify/templates", SRC / "auth/mail_texts"):
            shutil.copy2(root / table / "sv.py", root / table / f"{FAKE}.py")
            _edit(root / table / "__init__.py", '"sv":', f'"{FAKE}": sv.TABLE, "sv":')
    elif piece == "checks":
        _edit(root / SRC / "auth/tables.py", "'sv')", f"'sv', '{FAKE}')")
        names = ("users_locale", "pending_signups_locale", "oauth_flows_locale")
        _widening_migration(root, names)
    elif piece == "picker":
        _edit(i18n / "locale.ts", "{ en: 'en-GB',", f"{{ {FAKE}: 'sv-SE', en: 'en-GB',")
        _edit(i18n / "locale.ts", "{ en: 'English',", f"{{ {FAKE}: 'Fake', en: 'English',")
    else:
        for family in ("outbox", "auth"):
            shutil.copytree(root / GOLDEN / family / "sv", root / GOLDEN / family / FAKE)


def _fake_copy(tmp_path: Path, wired: tuple[str, ...]) -> Path:
    _copy(tmp_path)
    manifest = tmp_path / I18N / "locales.json"
    manifest.write_text(json.dumps([*json.loads(manifest.read_text()), FAKE]), encoding="utf-8")
    for piece in wired:
        _wire(tmp_path, piece)
    return tmp_path


def test_the_repository_has_no_half_added_language() -> None:
    assert {code: lacks for code, lacks in missing_pieces(REPO).items() if lacks} == {}


def test_a_fake_code_with_nothing_wired_lacks_every_piece(tmp_path: Path) -> None:
    root = _fake_copy(tmp_path, ())
    assert missing_pieces(root)[FAKE] == PIECES
    assert missing_pieces(root)["sv"] == ()


@pytest.mark.parametrize("piece", PIECES)
def test_the_guard_names_the_one_missing_piece(tmp_path: Path, piece: str) -> None:
    root = _fake_copy(tmp_path, tuple(p for p in PIECES if p != piece))
    assert missing_pieces(root)[FAKE] == (piece,)


def test_a_fully_wired_fake_code_passes(tmp_path: Path) -> None:
    assert missing_pieces(_fake_copy(tmp_path, PIECES))[FAKE] == ()


@pytest.mark.parametrize(
    "left_out", ["users_locale", "pending_signups_locale", "oauth_flows_locale"]
)
def test_a_migration_that_widens_only_two_of_the_three_tables_fails(
    tmp_path: Path, left_out: str
) -> None:
    root = _fake_copy(tmp_path, tuple(p for p in PIECES if p != "checks"))
    _edit(root / SRC / "auth/tables.py", "'sv')", f"'sv', '{FAKE}')")
    names = tuple(
        n for n in ("users_locale", "pending_signups_locale", "oauth_flows_locale") if n != left_out
    )
    _widening_migration(root, names)
    assert missing_pieces(root)[FAKE] == ("checks",)
