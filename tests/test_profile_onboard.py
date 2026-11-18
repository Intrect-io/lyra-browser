"""The first-run import offer and the opt-in sync on open (profile_onboard.py).

Real Chrome, real cookies and a real elicitation round trip are
``scripts/verify_profile_import_e2e.py`` (sections first_run_prompt, sync_on_open);
these pin who is asked, what an answer does and what is remembered.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lyra_browser import bypass, profile_import, profile_onboard
from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.profile_import import SIDECAR_NAME, ImportError_, SourceProfile


class _Session:
    def __init__(self, live: bool = False) -> None:
        self.live = live


class _McpSession:
    def __init__(self, answer, *, capable: bool = True) -> None:
        self.answer = answer
        self.capable = capable
        self.asked: list[dict] = []

    def check_client_capability(self, _caps) -> bool:
        return self.capable

    async def elicit(self, *, message, requestedSchema, related_request_id):  # noqa: N803
        self.asked.append({"message": message, "schema": requestedSchema})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _answer(action: str, content=None):
    return SimpleNamespace(action=action, content=content)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A lyra data dir, one source profile on 'this machine', and a fake request."""
    source_root = tmp_path / "chrome"
    (source_root / "Default").mkdir(parents=True)
    profiles = [SourceProfile("chrome", source_root, "Default", name="Me", account="me@x.test")]
    monkeypatch.setattr(profile_import, "discover_profiles", lambda: list(profiles))
    imports: list[tuple] = []

    def fake_import(config, source, *, sites=()):
        imports.append((source.browser, source.directory, list(sites)))
        if getattr(fake_import, "fail", None):
            raise ImportError_(fake_import.fail)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        (config.data_dir / SIDECAR_NAME).write_text(
            json.dumps(
                {
                    "browser": source.browser,
                    "user_data_dir": str(source.user_data_dir),
                    "profile": source.directory,
                    "sites": list(sites),
                    "source_stamp": profile_import.source_stamp(source),
                }
            )
        )
        return SimpleNamespace(cookies=SimpleNamespace(imported=3))

    monkeypatch.setattr(profile_onboard, "import_profile", fake_import)
    monkeypatch.setattr(profile_onboard, "_lock_held_by_other", lambda _c: False)
    state = SimpleNamespace(mcp=None, imports=imports, fake_import=fake_import)

    def get_context():
        if state.mcp is None:
            raise RuntimeError("no request")
        return SimpleNamespace(session=state.mcp, request_id="r1")

    import fastmcp.server.dependencies as deps

    monkeypatch.setattr(deps, "get_context", get_context)

    def ctx(**fields):
        cfg = Config(data_dir=tmp_path / "lyra", **fields)
        return SimpleNamespace(config=cfg, session=_Session(), audit=AuditLog(cfg.audit_path))

    state.ctx = ctx
    state.source = profiles[0]
    return state


async def test_the_offer_lists_the_profiles_and_skip_is_the_default(world):
    world.mcp = _McpSession(_answer("accept", {"value": "skip"}))
    ctx = world.ctx(consent_channel="elicit")
    got = await profile_onboard.offer_first_run(ctx)
    assert got == {"status": "skipped", "offered": 1}
    asked = world.mcp.asked[0]
    field = asked["schema"]["properties"]["value"]
    assert field["enum"] == ["skip", "chrome/Default"] and field["default"] == "skip"
    assert not asked["message"].startswith("Allow")
    assert world.imports == []
    # Remembered: the next open does not ask again.
    assert await profile_onboard.offer_first_run(ctx) is None
    assert len(world.mcp.asked) == 1


async def test_choosing_a_profile_imports_it_and_the_reply_holds_no_account(world):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    got = await profile_onboard.offer_first_run(world.ctx(consent_channel="auto"))
    assert got == {"status": "imported", "browser": "chrome", "profile": "Default", "cookies": 3}
    assert world.imports == [("chrome", "Default", [])]
    assert "me@x.test" not in json.dumps(got)
    assert "me@x.test" in json.dumps(world.mcp.asked[0]["schema"])  # the user sees it


@pytest.mark.parametrize(
    "answer",
    [
        _answer("accept", None),  # an approval-only client (Hermes): accept, no content
        _answer("accept", {}),
        _answer("accept", {"value": "chrome/Profile 9"}),  # not offered
        _answer("decline"),
        _answer("cancel"),
    ],
)
async def test_anything_but_an_offered_choice_is_a_skip(world, answer):
    world.mcp = _McpSession(answer)
    got = await profile_onboard.offer_first_run(world.ctx(consent_channel="elicit"))
    assert got["status"] == "skipped"
    assert world.imports == []


@pytest.mark.parametrize("channel", ["autonomous", "legacy", "off"])
async def test_channels_where_nobody_answers_never_ask(world, channel):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    assert await profile_onboard.offer_first_run(world.ctx(consent_channel=channel)) is None
    assert world.mcp.asked == [] and world.imports == []


async def test_bypass_mode_never_asks(world):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    token = bypass._bypass.set(True)
    try:
        assert await profile_onboard.offer_first_run(world.ctx(consent_channel="elicit")) is None
    finally:
        bypass._bypass.reset(token)
    assert world.mcp.asked == []


async def test_no_request_no_capability_or_switched_off_never_asks(world):
    assert await profile_onboard.offer_first_run(world.ctx(consent_channel="elicit")) is None
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}), capable=False)
    assert await profile_onboard.offer_first_run(world.ctx(consent_channel="elicit")) is None
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    ctx = world.ctx(consent_channel="elicit", first_run_import=False)
    assert await profile_onboard.offer_first_run(ctx) is None
    assert world.mcp.asked == []


async def test_a_used_or_imported_profile_is_not_empty(world):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    ctx = world.ctx(consent_channel="elicit")
    (ctx.config.profile_dir / "Default").mkdir(parents=True)
    (ctx.config.profile_dir / "Default" / "Cookies").write_bytes(b"")
    assert await profile_onboard.offer_first_run(ctx) is None
    (ctx.config.profile_dir / "Default" / "Cookies").unlink()
    ctx.session.live = True  # a window already open: too late to import into it
    assert await profile_onboard.offer_first_run(ctx) is None
    assert world.mcp.asked == []


async def test_a_failed_import_is_reported_and_offered_again(world):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    world.fake_import.fail = "the login keyring is locked"
    ctx = world.ctx(consent_channel="elicit")
    got = await profile_onboard.offer_first_run(ctx)
    assert got["status"] == "failed" and "keyring" in got["reason"]
    world.fake_import.fail = None
    assert (await profile_onboard.offer_first_run(ctx))["status"] == "imported"


async def test_an_unanswered_prompt_is_not_remembered(world):
    world.mcp = _McpSession(TimeoutError())
    ctx = world.ctx(consent_channel="elicit")
    assert (await profile_onboard.offer_first_run(ctx))["status"] == "unanswered"
    world.mcp = _McpSession(_answer("accept", {"value": "skip"}))
    assert (await profile_onboard.offer_first_run(ctx))["status"] == "skipped"


async def test_sync_is_off_unless_the_operator_turns_it_on(world):
    world.mcp = _McpSession(_answer("accept", {"value": "chrome/Default"}))
    ctx = world.ctx(consent_channel="elicit")
    await profile_onboard.offer_first_run(ctx)
    (world.source.path / "Cookies").write_bytes(b"changed")
    assert await profile_onboard.sync_on_open(ctx) is None
    assert len(world.imports) == 1


async def test_sync_reimports_only_when_the_source_changed(world):
    ctx = world.ctx(profile_sync=True)
    world.fake_import(ctx.config, world.source, sites=["a.test"])
    world.imports.clear()
    assert await profile_onboard.sync_on_open(ctx) == {"status": "unchanged"}
    (world.source.path / "Cookies").write_bytes(b"new login")
    got = await profile_onboard.sync_on_open(ctx)
    assert got == {"status": "synced", "cookies": 3}
    assert world.imports == [("chrome", "Default", ["a.test"])]  # the same sites
    assert await profile_onboard.sync_on_open(ctx) == {"status": "unchanged"}


async def test_a_sync_that_cannot_run_says_why(world):
    ctx = world.ctx(profile_sync=True)
    world.fake_import(ctx.config, world.source)
    (world.source.path / "Cookies").write_bytes(b"new login")
    world.fake_import.fail = "keyring locked"
    got = await profile_onboard.sync_on_open(ctx)
    assert got == {"status": "failed", "reason": "keyring locked"}


def test_the_stamp_follows_the_files_an_import_reads(tmp_path):
    source = SourceProfile("chrome", tmp_path, "Default")
    (tmp_path / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
    first = profile_import.source_stamp(source)
    assert profile_import.source_stamp(source) == first
    (tmp_path / "Default" / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"x")
    second = profile_import.source_stamp(source)
    assert second != first
    (tmp_path / "Default" / "Preferences").write_text("{}")  # not imported: no change
    assert profile_import.source_stamp(source) == second


async def test_open_browser_carries_what_the_offer_did(make_ctx, tools_of, monkeypatch):
    from lyra_browser.tools import navigation

    async def offered(_ctx):
        return {"status": "skipped", "offered": 2}

    async def synced(_ctx):
        return None

    monkeypatch.setattr(navigation, "offer_first_run", offered)
    monkeypatch.setattr(navigation, "sync_on_open", synced)
    result = await (await tools_of(make_ctx()))["open_browser"]()
    assert result["status"] == "ok"
    assert result["profile_import"] == {"status": "skipped", "offered": 2}
    assert "profile_sync" not in result
