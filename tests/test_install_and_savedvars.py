"""T-010 acceptance tests for ``wow_savedvars`` and ``wow_install`` (created 2026-11-11).

The install lives under ``tmp_path``. Nothing here opens ``/Applications``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from wow_addon_forge.apidocs import IndexStore
from wow_addon_forge.env import RealFileSystem
from wow_addon_forge.headless import minimal_api_index
from wow_addon_forge.install import is_junction
from wow_addon_forge.tools import ForgeTools

_CLEAN_TOC = "## Interface: 16001\n## Title: Clean\n\nCore.lua\n"
_CLEAN_LUA = "local ready = true\n"
_EXTRA_TOC = "## Interface: 16001\n## Title: Clean\n\nCore.lua\nExtra.lua\n"
_FAIL_LUA = "goto not_lua_51\n"
_NESTED = (
    "CleanDB = {\n"
    '  ["nested"] = { ["deep"] = "value", ["neg"] = -42 },\n'
    '  ["note"] = [[line one\nline two]],\n'
    "}\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _wow(tmp_path: Path) -> Path:
    root = tmp_path / "WoW"
    (root / "_forever_" / "Interface" / "AddOns").mkdir(parents=True)
    return root


def _clean(tmp_path: Path, lua: str = _CLEAN_LUA, toc: str = _CLEAN_TOC) -> Path:
    addon = tmp_path / "src" / "Clean"
    _write(addon / "Clean.toc", toc)
    _write(addon / "Core.lua", lua)
    if "Extra.lua" in toc:
        _write(addon / "Extra.lua", "local extra = 1\n")
    return addon


def _tools(tmp_path: Path, wow: Path) -> ForgeTools:
    IndexStore(tmp_path / "index").save(minimal_api_index().data)
    return ForgeTools(
        fs=RealFileSystem(),
        default_roots=[str(wow)],
        cache_dir=tmp_path / "index",
        data_dir=tmp_path / "backups",
        clock=lambda: 1_700_000_000.0,
    )


def _install(tools: ForgeTools, **kwargs: Any) -> dict[str, Any]:
    return json.loads(tools.handle_install(kwargs))


def test_savedvars_nested_long_string_negative_and_dotted_path(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    saved = wow / "_forever_" / "WTF" / "Account" / "ACC1" / "SavedVariables" / "Clean.lua"
    _write(saved, _NESTED)
    tools = _tools(tmp_path, wow)
    payload = json.loads(tools.handle_savedvars({"addon": "Clean"}))
    assert payload["ok"] is True
    assert payload["scope"] == "account"
    assert payload["data"]["CleanDB"]["nested"]["neg"] == -42
    assert payload["data"]["CleanDB"]["note"] == "line one\nline two"
    # [2026-11-11 T-010] The account folder name is personal data and must not leak.
    assert "ACC1" not in json.dumps(payload)
    picked = json.loads(tools.handle_savedvars({"addon": "Clean", "path": "CleanDB.nested.deep"}))
    assert picked["ok"] is True
    assert picked["data"] == "value"


def test_install_copies_new_addon_and_reports_restart(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    source = _clean(tmp_path)
    tools = _tools(tmp_path, wow)
    payload = _install(tools, path=str(source))
    assert payload["ok"] is True
    assert payload["action"] == "restart"
    assert payload["dry_run"] is False
    assert payload["backup"] is None
    assert payload["forced"] is False
    dest = Path(payload["dest"])
    assert dest == wow / "_forever_" / "Interface" / "AddOns" / "Clean"
    assert (dest / "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA
    assert not str(payload["dest"]).startswith(str(tmp_path / "backups"))


def test_install_backs_up_outside_the_game_and_reports_reload(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    dest_dir = wow / "_forever_" / "Interface" / "AddOns" / "Clean"
    _write(dest_dir / "Clean.toc", _CLEAN_TOC)
    _write(dest_dir / "Core.lua", "local old = true\n")
    source = _clean(tmp_path)
    tools = _tools(tmp_path, wow)
    before = (dest_dir / "Core.lua").read_text(encoding="utf-8")
    payload = _install(tools, path=str(source))
    assert payload["ok"] is True
    assert payload["action"] == "reload"
    backup = Path(payload["backup"])
    assert backup == tmp_path / "backups" / "Clean-1700000000"
    assert wow not in backup.parents or backup.is_relative_to(tmp_path / "backups")
    assert not str(backup).startswith(str(wow))
    assert (backup / "Core.lua").read_text(encoding="utf-8") == before
    assert (dest_dir / "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA


def test_install_reports_restart_when_the_toc_gains_a_file(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    dest_dir = wow / "_forever_" / "Interface" / "AddOns" / "Clean"
    _write(dest_dir / "Clean.toc", _CLEAN_TOC)
    _write(dest_dir / "Core.lua", _CLEAN_LUA)
    source = _clean(tmp_path, toc=_EXTRA_TOC)
    payload = _install(_tools(tmp_path, wow), path=str(source))
    assert payload["ok"] is True
    assert payload["action"] == "restart"
    assert "Extra.lua" in payload["note"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    source = _clean(tmp_path)
    addons = wow / "_forever_" / "Interface" / "AddOns"
    before = sorted(p.name for p in addons.iterdir())
    payload = _install(_tools(tmp_path, wow), path=str(source), dry_run=True)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["action"] == "restart"
    assert payload["backup"] is None
    assert sorted(p.name for p in addons.iterdir()) == before
    assert not (tmp_path / "backups").exists()


def test_failed_check_refuses_unless_force(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    source = _clean(tmp_path, lua=_FAIL_LUA)
    addons = wow / "_forever_" / "Interface" / "AddOns"
    tools = _tools(tmp_path, wow)
    refused = _install(tools, path=str(source))
    assert refused["ok"] is False
    assert refused["error"]["code"] == "check-failed"
    assert list(addons.iterdir()) == []
    forced = _install(tools, path=str(source), force=True)
    assert forced["ok"] is True
    assert forced["forced"] is True
    assert forced["check_verdict"] == "fail"
    assert (addons / "Clean" / "Core.lua").is_file()


def test_install_refuses_traversal_and_absolute_names(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    tools = _tools(tmp_path, wow)
    addons = wow / "_forever_" / "Interface" / "AddOns"
    # [2026-11-24 T-061] The drive-relative name is appended as text: on Windows,
    # Path(...) / "C:Evil" silently drops "C:" when the base is on drive C:, so the
    # hostile name never reached addon_name() and the test failed on Windows with
    # bad-path instead of bad-argument (the guard itself was fine).
    drive_relative = str(tmp_path / "src") + os.sep + "C:Evil"
    for hostile in ("../Evil", str(tmp_path / ".." / "Evil"), drive_relative):
        payload = _install(tools, path=hostile)
        assert payload["ok"] is False, hostile
        assert payload["error"]["code"] == "bad-argument", hostile
    assert list(addons.iterdir()) == []


def test_link_mode_symlinks_and_second_install_does_not_delete_source(tmp_path: Path) -> None:
    wow = _wow(tmp_path)
    source = _clean(tmp_path)
    tools = _tools(tmp_path, wow)
    linked = _install(tools, path=str(source), mode="link")
    assert linked["ok"] is True
    dest = Path(linked["dest"])
    # [2026-11-24 T-061] Windows without Developer Mode gets a junction instead.
    assert dest.is_symlink() or is_junction(str(dest))
    assert dest.resolve() == source.resolve()
    # [2026-11-11 T-010] Replacing the link must unlink it, not rmtree the source.
    copied = _clean(tmp_path / "other")
    replaced = _install(tools, path=str(copied), mode="copy")
    assert replaced["ok"] is True
    assert source.is_dir()
    assert (source / "Core.lua").is_file()
    assert not dest.is_symlink()
    assert (dest / "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_link_mode_falls_back_to_junction_without_symlink_privilege(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # [2026-11-24 T-061] Simulates a Windows account without Developer Mode: os.symlink
    # fails with ERROR_PRIVILEGE_NOT_HELD, so link mode must make a junction, and a later
    # install over that junction must remove only the junction, never the source tree.
    def refuse_symlink(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(None, "A required privilege is not held by the client", None, 1314)

    monkeypatch.setattr(os, "symlink", refuse_symlink)
    wow = _wow(tmp_path)
    source = _clean(tmp_path)
    tools = _tools(tmp_path, wow)
    linked = _install(tools, path=str(source), mode="link")
    assert linked["ok"] is True, linked
    dest = Path(linked["dest"])
    assert is_junction(str(dest))
    assert (dest / "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA
    copied = _clean(tmp_path / "other")
    replaced = _install(tools, path=str(copied), mode="copy")
    assert replaced["ok"] is True
    assert (source / "Core.lua").is_file()
    assert not is_junction(str(dest))
    assert Path(replaced["backup"], "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA


def test_link_mode_reraises_other_symlink_errors(tmp_path: Path, monkeypatch: Any) -> None:
    # [2026-11-24 T-061] Only the missing-privilege error falls back to a junction.
    def fail_symlink(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("disk is read-only")

    monkeypatch.setattr(os, "symlink", fail_symlink)
    wow = _wow(tmp_path)
    source = _clean(tmp_path)
    tools = _tools(tmp_path, wow)
    payload = _install(tools, path=str(source), mode="link")
    assert payload["ok"] is False
    assert not (wow / "_forever_" / "Interface" / "AddOns" / "Clean").exists()


def test_install_replaces_an_addon_holding_read_only_files(tmp_path: Path) -> None:
    # [2026-11-24 T-061] A developer's addon clone holds read-only git object files. On
    # Windows shutil.rmtree stopped with PermissionError on them (half-deleted addon,
    # install-failed); now the read-only bit is cleared and the replace completes.
    wow = _wow(tmp_path)
    tools = _tools(tmp_path, wow)
    first = _install(tools, path=str(_clean(tmp_path)))
    assert first["ok"] is True
    dest = Path(first["dest"])
    locked = dest / ".git" / "objects" / "ab" / "cdef"
    _write(locked, "blob\n")
    locked.chmod(0o444)
    replaced = _install(tools, path=str(_clean(tmp_path / "other")))
    assert replaced["ok"] is True, replaced
    assert not (dest / ".git").exists()
    assert (dest / "Core.lua").read_text(encoding="utf-8") == _CLEAN_LUA
    assert Path(replaced["backup"], ".git", "objects", "ab", "cdef").is_file()


def test_is_junction_is_false_for_plain_entries(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert is_junction(str(tmp_path / "dir")) is False
    assert is_junction(str(tmp_path / "file.txt")) is False
    assert is_junction(str(tmp_path / "missing")) is False


def test_savedvars_rejects_binary_chunk_and_default_data_dir_follows_profile(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import sys
    import types

    import pytest

    from wow_addon_forge.install import default_data_dir
    from wow_addon_forge.savedvars import SavedVarsReadError, parse_savedvars_lua

    # [2026-11-11 T-060] Binary chunks (\x1bLua) must fail before SavedVariables VM execution.
    with pytest.raises(SavedVarsReadError) as exc:
        parse_savedvars_lua("\x1bLuaQ\x00\x01\x04\x08\x04\x08\x00")
    assert exc.value.code == "bad-savedvars"
    assert "binary chunks are not allowed" in exc.value.message

    profile_home = tmp_path / "profiles" / "officer"
    fake_constants = types.ModuleType("hermes_constants")
    fake_constants.get_hermes_home = lambda: profile_home  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)
    assert default_data_dir() == profile_home / "cache" / "wow-addon-forge" / "backups"
