"""A guarded name is scanned, never skipped: ``ownvitals.ecg`` has to be a package.

Split out of ``test_analysis_purity.py`` (which holds the scanner and ``present``) to keep both
files under 500 lines. The tests are unchanged.
"""

from pathlib import Path

import pytest

from test_analysis_purity import SRC, present, violations

# --- the ecg checks cannot be skipped by a module ---------------------------------------------


def ownvitals_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "ownvitals"
    directory.mkdir()
    return directory


def never_skipped(name: str, src: Path) -> Path:
    """``present`` for a package that exists: a skip here is a failure, not a silent pass."""
    try:
        return present(name, src)
    except pytest.skip.Exception:
        pytest.fail(f"ownvitals.{name} exists but the purity guard skipped it")


def test_the_real_analysis_package_is_never_skipped() -> None:
    assert never_skipped("analysis", SRC) == SRC / "ownvitals" / "analysis"


def test_a_guarded_name_that_does_not_exist_yet_is_skipped(tmp_path: Path) -> None:
    ownvitals_dir(tmp_path)
    with pytest.raises(pytest.skip.Exception, match=r"ownvitals\.ecg does not exist yet"):
        present("ecg", tmp_path)


@pytest.mark.parametrize("with_init", [True, False])
def test_an_ecg_package_is_scanned_not_skipped(tmp_path: Path, with_init: bool) -> None:
    package = ownvitals_dir(tmp_path) / "ecg"
    package.mkdir()
    if with_init:
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text("from ownvitals.db import engine\n", encoding="utf-8")
    assert never_skipped("ecg", tmp_path) == package
    assert violations(package, tmp_path) == [
        "ownvitals/ecg/core.py:1: from ownvitals.db import engine"
    ]


def test_an_ecg_module_fails_the_guard_instead_of_being_skipped(tmp_path: Path) -> None:
    (ownvitals_dir(tmp_path) / "ecg.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match=r"ecg\.py exists.*must be a package"):
        present("ecg", tmp_path)


def test_an_ecg_module_next_to_an_ecg_package_still_fails(tmp_path: Path) -> None:
    directory = ownvitals_dir(tmp_path)
    (directory / "ecg").mkdir()
    (directory / "ecg.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="must be a package"):
        present("ecg", tmp_path)


def test_an_analysis_module_cannot_replace_the_analysis_package_either(tmp_path: Path) -> None:
    (ownvitals_dir(tmp_path) / "analysis.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="must be a package"):
        present("analysis", tmp_path)
