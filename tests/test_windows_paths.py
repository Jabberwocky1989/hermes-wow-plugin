"""T-061 tests for the Windows-facing path helpers in ``env`` (created 2026-11-24).

Every case passes ``platform`` explicitly, so the Windows rules are exercised on macOS and
Linux CI too (AGENTS.md: a Windows regression must fail on every OS). Nothing here opens a
real install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wow_addon_forge.check import run_addon_tests
from wow_addon_forge.env import DEFAULT_ROOTS, example_root, path_is_under, replace_file
from wow_addon_forge.headless import minimal_api_index


def test_example_root_names_the_platform_default() -> None:
    assert example_root("win32") == "C:\\Program Files (x86)\\World of Warcraft"
    assert example_root("darwin") == "/Applications/World of Warcraft"
    assert example_root("linux") == "/Applications/World of Warcraft"
    assert example_root() in DEFAULT_ROOTS


@pytest.mark.parametrize(
    ("path", "base", "expected"),
    [
        ("C:\\WoW\\AddOns\\Mine\\Tests\\a.lua", "C:\\WoW\\AddOns\\Mine", True),
        ("c:/wow/addons/mine/Tests/a.lua", "C:\\WoW\\AddOns\\Mine\\", True),
        ("C:\\WoW\\AddOns\\Mine", "C:\\WoW\\AddOns\\Mine", True),
        # A sibling whose name merely starts with the base is not inside it.
        ("C:\\WoW\\AddOns\\Mine2\\a.lua", "C:\\WoW\\AddOns\\Mine", False),
        ("D:\\WoW\\AddOns\\Mine\\a.lua", "C:\\WoW\\AddOns\\Mine", False),
        ("Tests/a.lua", "C:\\WoW\\AddOns\\Mine", False),
    ],
)
def test_path_is_under_on_windows(path: str, base: str, expected: bool) -> None:
    assert path_is_under(path, base, platform="win32") is expected


def test_path_is_under_is_case_sensitive_elsewhere() -> None:
    assert path_is_under("/wow/Mine/a.lua", "/wow/Mine", platform="linux") is True
    assert path_is_under("/wow/mine/a.lua", "/wow/Mine", platform="linux") is False
    assert path_is_under("/wow/Mine2/a.lua", "/wow/Mine", platform="darwin") is False


def test_wow_test_accepts_an_absolute_test_path_in_the_other_separator_style(
    fake_fs_factory: Any,
) -> None:
    # [2026-11-24 T-061] Regression: the addon folder arrives with backslashes and the
    # test path with forward slashes (both are normal on Windows). The old startswith()
    # saw no match, joined the absolute path onto the addon folder, and raised
    # "Test path not found". Fails on the old code on every OS.
    base = "C:\\WoW\\_forever_\\Interface\\AddOns\\Mine"
    files = {
        f"{base}\\Mine.toc": "## Interface: 16001\n## Title: Mine\n\nCore.lua\n",
        f"{base}\\Core.lua": "MineLoaded = true\n",
        f"{base}\\Tests\\suite.lua": 'test("runs", function(ctx) end)\n',
    }
    report = run_addon_tests(
        base,
        index=minimal_api_index(),
        fs=fake_fs_factory(files),
        tests="C:/WoW/_forever_/Interface/AddOns/Mine/Tests/suite.lua",
    )
    assert report["verdict"] == "pass", report
    assert report["passed"] == 1
    assert report["results"][0]["file"] == "Tests/suite.lua"


def test_replace_file_retries_a_briefly_locked_destination() -> None:
    calls: list[tuple[Any, Any]] = []
    waits: list[float] = []

    def flaky_replace(source: Any, dest: Any) -> None:
        calls.append((source, dest))
        if len(calls) < 3:
            raise PermissionError("[WinError 5] Access is denied")

    replace_file("a.tmp", "a", replace=flaky_replace, sleep=waits.append, delay=0.01)
    assert len(calls) == 3
    assert waits == [0.01, 0.02]


def test_replace_file_gives_up_after_the_last_attempt() -> None:
    waits: list[float] = []

    def locked(_source: Any, _dest: Any) -> None:
        raise PermissionError("[WinError 32] The file is in use by another process")

    with pytest.raises(PermissionError):
        replace_file("a.tmp", "a", attempts=3, replace=locked, sleep=waits.append)
    assert len(waits) == 2


def test_replace_file_does_not_retry_other_errors() -> None:
    waits: list[float] = []

    def missing(_source: Any, _dest: Any) -> None:
        raise FileNotFoundError("a.tmp")

    with pytest.raises(FileNotFoundError):
        replace_file("a.tmp", "a", replace=missing, sleep=waits.append)
    assert waits == []


def test_replace_file_replaces_a_real_file(tmp_path: Path) -> None:
    dest = tmp_path / "Inbox.lua"
    dest.write_text("old\n", encoding="utf-8")
    temporary = tmp_path / "Inbox.lua.tmp"
    temporary.write_text("new\n", encoding="utf-8")
    replace_file(temporary, dest)
    assert dest.read_text(encoding="utf-8") == "new\n"
    assert not temporary.exists()
