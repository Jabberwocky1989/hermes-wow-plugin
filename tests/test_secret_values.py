"""T-034 acceptance tests for secret values under a restriction (created 2026-11-11).

Nothing here opens ``/Applications``.
"""

from __future__ import annotations

import copy
from pathlib import Path

from wow_addon_forge.apidocs import ApiIndex
from wow_addon_forge.check import check_addon
from wow_addon_forge.headless import World, minimal_api_index

REPO = Path(__file__).resolve().parent.parent


def _secret_index() -> ApiIndex:
    data = copy.deepcopy(minimal_api_index().data)
    data["entries"]["function"]["C_Probe.GetSecret"] = {
        "name": "C_Probe.GetSecret",
        "kind": "function",
        "namespace": "C_Probe",
        "system": None,
        "args": [],
        "returns": [{"type": "string"}],
        "flags": {},
        "c_api": True,
        "restricted": False,
        "secret": {"args": None, "returns": True, "conditions": []},
        "deprecated": False,
        "documented": True,
        "source": "test",
    }
    return ApiIndex(data)


def _write_addon(root: Path, body: str) -> Path:
    addon = root / "SecretProbe"
    addon.mkdir(parents=True)
    (addon / "SecretProbe.toc").write_text(
        "## Interface: 16001\n## Title: SecretProbe\n\nCore.lua\n",
        encoding="utf-8",
    )
    (addon / "Core.lua").write_text(body, encoding="utf-8")
    return addon


def test_secret_ops_are_findings_and_a_setter_is_not(tmp_path: Path) -> None:
    index = _secret_index()
    addon = _write_addon(
        tmp_path,
        "local secret = C_Probe.GetSecret()\n"
        "local frame = CreateFrame('Frame')\n"
        "frame:SetText(secret)\n"
        "local bad = secret + 1\n",
    )
    report = check_addon(addon, index=index, restriction="Combat")
    secrets = [item for item in report["findings"] if item["code"] == "secret-value"]
    assert secrets, report["findings"]
    assert secrets[0]["file"] == "Core.lua"
    assert secrets[0]["line"] == 4
    assert "arithmetic" in secrets[0]["message"]

    concat = _write_addon(
        tmp_path / "concat",
        "local secret = C_Probe.GetSecret()\nlocal bad = secret .. 'x'\n",
    )
    concat_report = check_addon(concat, index=index, restriction="Combat")
    assert any(
        item["code"] == "secret-value" and item["line"] == 2 for item in concat_report["findings"]
    )

    compared = _write_addon(
        tmp_path / "cmp",
        "local secret = C_Probe.GetSecret()\nlocal bad = secret < 1\n",
    )
    compared_report = check_addon(compared, index=index, restriction="Combat")
    compared_findings = compared_report["findings"]
    assert any(item["code"] == "secret-value" and item["line"] == 2 for item in compared_findings)

    open_world = World(index=index)
    client = open_world.client("Player", rank=0)
    client.load_addon(addon)
    assert client.eval("issecretvalue(C_Probe.GetSecret())") is False


def test_guildhall_and_forgeprobe_still_pass() -> None:
    index = minimal_api_index()
    for name in ("Guildhall", "ForgeProbe"):
        report = check_addon(REPO / "addons" / name, index=index)
        assert report["verdict"] == "pass", (name, report["findings"])


# [2026-11-24 T-065] The four unit calls the synced 1.60.1.71091 index marks
# secret-returning. Before T-065 their hand-written simulations returned plain values
# under every restriction, so no test reached the addons' secret-name fallbacks.
_SECRET_UNIT_CALLS = {
    "UnitName": 'UnitName("player")',
    "UnitFullName": 'UnitFullName("player")',
    "UnitGUID": 'UnitGUID("player")',
    "UnitIsUnit": 'UnitIsUnit("player", "player")',
}


def test_hand_written_unit_calls_return_secrets_under_a_restriction() -> None:
    world = World(index=minimal_api_index())
    ann = world.client("Ann", rank=1)
    for name, call in _SECRET_UNIT_CALLS.items():
        assert ann.run_lua(f"return issecretvalue(({call}))") is False, name
    returns_plain = ann.run_lua('return select("#", UnitName("player"))')
    ann.set_restriction("Combat")
    for name, call in _SECRET_UNIT_CALLS.items():
        assert ann.run_lua(f"return issecretvalue(({call}))") is True, name
    # The wrapper hides values; it never changes how many values come back.
    assert ann.run_lua('return select("#", UnitName("player"))') == returns_plain
    ann.set_restriction("Combat", active=False)
    assert ann.run_lua('return (UnitName("player"))') == "Ann"
    assert ann.errors == []


def test_hand_written_secrecy_follows_the_index_entry() -> None:
    # An index whose UnitName entry has no secret returns keeps UnitName plain under
    # a restriction, while UnitGUID (still secret-returning) turns secret: the rule
    # reads the index, not a fixed list of names in the simulator.
    data = copy.deepcopy(minimal_api_index().data)
    data["entries"]["function"]["UnitName"]["secret"] = {
        "args": None,
        "returns": False,
        "conditions": [],
    }
    world = World(index=ApiIndex(data))
    ann = world.client("Ann", rank=1)
    ann.set_restriction("Combat")
    assert ann.run_lua('return issecretvalue((UnitName("player")))') is False
    assert ann.run_lua('return issecretvalue((UnitGUID("player")))') is True
    assert ann.errors == []


def test_guildhall_board_refuses_a_post_while_its_own_name_is_secret() -> None:
    # Guildhall's self_name falls back to "" for a secret name; Board.post then
    # refuses with "no-self" instead of authoring a record with an empty author.
    world = World(index=minimal_api_index(), rng_seed=65065)
    ann = world.client("Ann", rank=1)
    ann.load_addon(REPO / "addons" / "Guildhall")
    ann.set_restriction("Combat")
    assert ann.run_lua('return select(2, Guildhall.Board.post("Raid Friday"))') == "no-self"
    assert ann.run_lua("return #Guildhall.Board.rows()") == 0
    ann.set_restriction("Combat", active=False)
    assert isinstance(ann.run_lua('return Guildhall.Board.post("Raid Friday")'), str)
    assert ann.run_lua("return #Guildhall.Board.rows()") == 1
    assert ann.errors == []
