"""The API loop brains against fake clients: what they send, how they feed results back."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from conftest import FakePage, FakeSession
from lyra_browser.uat.brains import BrainError, BrainSession, ToolSpec
from lyra_browser.uat.brains.anthropic_api import NOT_EXECUTED, NUDGE, AnthropicBrain, estimate_cost
from lyra_browser.uat.brains.openai_compat import OpenAICompatBrain
from lyra_browser.uat.server import build_uat_server
from lyra_browser.uat.spec import BrainSpec, RunSpec

ENTRY = "https://start.example/"


async def make_session(tmp_path: Path, page: FakePage, brain: dict, **persona_extra):
    spec = RunSpec.model_validate(
        {
            "persona": {
                "id": "p1",
                "name": "T",
                "entry_url": ENTRY,
                "goal": "g",
                "step_budget": 5,
                **persona_extra,
            },
            "target": {"trusted_origins": ["start.example"]},
            "brain": brain,
            "out_dir": str(tmp_path),
        }
    )
    mcp, ctx, recorder = await build_uat_server(
        spec, tmp_path / "run", session_factory=lambda cfg: FakeSession(page)
    )
    ctx.config.download_settle_s = 0.0
    page.on_download = ctx.downloads.on_download
    client = Client(mcp)
    await client.__aenter__()
    tools = [
        ToolSpec(t.name, t.description or "", t.inputSchema) for t in await client.list_tools()
    ]
    session = BrainSession(
        client=client,
        tools=tools,
        system_prompt="SYSTEM",
        task_prompt="TASK",
        max_output_chars=12000,
        recorder=recorder,
        spec=spec,
    )
    return session, client, recorder, spec


# --- Anthropic ----------------------------------------------------------------------


def a_use(uid: str, name: str, **inp):
    return SimpleNamespace(type="tool_use", id=uid, name=name, input=inp)


def a_text(text: str):
    return SimpleNamespace(type="text", text=text)


def a_response(*blocks, stop_reason="tool_use", inp=100, out=20, cached=0):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cached),
    )


class FakeAnthropic:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        # A copy: the brain keeps appending to the same messages list, and a
        # recorded reference would show every call its final state.
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("the brain asked for more turns than the script has")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_anthropic_loop_feeds_results_images_and_nudges(tmp_path, page, monkeypatch):
    session, client, recorder, _spec = await make_session(
        tmp_path, page, {"backend": "anthropic", "model": "claude-opus-5-5"}
    )
    fake = FakeAnthropic(
        [
            a_response(a_text("Starting."), a_use("t1", "navigate", url=ENTRY)),
            a_response(a_use("t2", "screenshot"), cached=80),
            a_response(a_text("I think I am done."), stop_reason="end_turn"),
            a_response(a_use("t3", "finish", outcome="reached_goal", summary="All good.")),
        ]
    )
    monkeypatch.setattr(AnthropicBrain, "_client", lambda self: fake)
    try:
        info = await AnthropicBrain(BrainSpec(backend="anthropic", model="claude-opus-5-5")).run(
            session
        )
    finally:
        await client.__aexit__(None, None, None)

    assert recorder.finished
    assert info.turns == 4 and info.input_tokens == 400 and info.output_tokens == 80
    assert info.cache_read_tokens == 80 and info.cost_usd == estimate_cost(
        "claude-opus-5-5", 400, 80, 80
    )
    first = fake.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    names = {t["name"] for t in first["tools"]}
    assert {"navigate", "finish", "report_finding"} <= names and "publish" not in names
    assert first["messages"] == [{"role": "user", "content": "TASK"}]
    # Turn 3 saw the screenshot's tool_result with text and an image block.
    results = fake.calls[2]["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "t2"
    kinds = [c["type"] for c in results[0]["content"]]
    assert kinds == ["text", "image"]
    assert results[0]["content"][1]["source"]["media_type"] == "image/png"
    # Turn 4 followed a nudge, appended after the text-only assistant turn.
    assert fake.calls[3]["messages"][-1] == {"role": "user", "content": NUDGE}
    assert fake.calls[3]["messages"][-2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_anthropic_stops_after_the_first_failed_call_in_a_turn(tmp_path, page, monkeypatch):
    session, client, _recorder, _spec = await make_session(tmp_path, page, {"backend": "anthropic"})
    fake = FakeAnthropic(
        [
            a_response(a_use("t1", "no_such_tool"), a_use("t2", "navigate", url=ENTRY)),
            a_response(a_use("t3", "finish", outcome="blocked", summary="x")),
        ]
    )
    monkeypatch.setattr(AnthropicBrain, "_client", lambda self: fake)
    try:
        await AnthropicBrain(BrainSpec(backend="anthropic")).run(session)
    finally:
        await client.__aexit__(None, None, None)
    results = fake.calls[1]["messages"][-1]["content"]
    assert results[0]["is_error"] is True
    assert results[1] == {
        "type": "tool_result",
        "tool_use_id": "t2",
        "content": NOT_EXECUTED,
        "is_error": True,
    }
    assert page.url == ENTRY or page.url == "https://start.example/"  # navigate did not run twice


@pytest.mark.asyncio
async def test_anthropic_refusal_is_a_brain_error(tmp_path, page, monkeypatch):
    session, client, _recorder, _spec = await make_session(tmp_path, page, {"backend": "anthropic"})
    fake = FakeAnthropic([a_response(stop_reason="refusal")])
    monkeypatch.setattr(AnthropicBrain, "_client", lambda self: fake)
    try:
        with pytest.raises(BrainError, match="refused"):
            await AnthropicBrain(BrainSpec(backend="anthropic")).run(session)
    finally:
        await client.__aexit__(None, None, None)


def test_cost_estimate_knows_current_models_and_admits_ignorance():
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0, 0) == 4.0
    assert estimate_cost("claude-sonnet-5-5", 0, 1_000_000, 0) == 10.0
    assert estimate_cost("claude-opus-5-5", 0, 0, 1_000_000) == 0.2
    assert estimate_cost("some-other-model", 10, 10, 10) is None


# --- OpenAI-compatible ---------------------------------------------------------------


def o_call(cid: str, name: str, arguments: str):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=arguments))


def o_response(*calls, content=None, finish_reason="tool_calls"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=list(calls) or None),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=50, completion_tokens=5, prompt_tokens_details=None, cost=0.01
        ),
    )


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        # A copy: the brain keeps appending to the same messages list, and a
        # recorded reference would show every call its final state.
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("the brain asked for more turns than the script has")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_openai_compat_loop_tool_messages_images_and_bad_json(tmp_path, page, monkeypatch):
    session, client, recorder, _spec = await make_session(
        tmp_path, page, {"backend": "openrouter", "model": "vendor/model"}
    )
    fake = FakeOpenAI(
        [
            o_response(o_call("c1", "navigate", json.dumps({"url": ENTRY}))),
            o_response(o_call("c2", "screenshot", "{}")),
            o_response(
                o_call("c3", "click", "{not json"), o_call("c4", "click", '{"selector":"#a"}')
            ),
            o_response(content="done?", finish_reason="stop"),
            o_response(o_call("c5", "finish", json.dumps({"outcome": "partial", "summary": "ok"}))),
        ]
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(OpenAICompatBrain, "_client", lambda self: fake)
    try:
        info = await OpenAICompatBrain(BrainSpec(backend="openrouter", model="vendor/model")).run(
            session
        )
    finally:
        await client.__aexit__(None, None, None)

    assert recorder.finished and info.turns == 5 and info.input_tokens == 250
    first = fake.calls[0]
    assert first["model"] == "vendor/model" and first["parallel_tool_calls"] is False
    # OpenRouter is asked what each request cost, and the brain adds it up.
    assert first["extra_body"] == {"usage": {"include": True}}
    assert info.cost_usd == pytest.approx(0.05)
    assert first["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert {t["function"]["name"] for t in first["tools"]} >= {"navigate", "finish"}
    # After the screenshot: a tool message, then a user message carrying the image.
    msgs = fake.calls[2]["messages"]
    assert msgs[-2]["role"] == "tool" and msgs[-2]["tool_call_id"] == "c2"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    # Bad JSON arguments fail that call and skip the rest of the turn.
    msgs = fake.calls[3]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert "not valid JSON" in tool_msgs[-2]["content"]
    assert tool_msgs[-1] == {"role": "tool", "tool_call_id": "c4", "content": NOT_EXECUTED}
    assert not any(c[0] == "click" for c in page.calls)
    # A text-only turn without finish got the nudge.
    assert fake.calls[4]["messages"][-1] == {"role": "user", "content": NUDGE}


def test_openai_compat_presets_and_requirements(monkeypatch):
    ollama = OpenAICompatBrain(BrainSpec(backend="ollama", model="gemma4:31b"))
    assert ollama.base_url == "https://ollama.com/v1" and ollama.api_key_env == "OLLAMA_API_KEY"
    assert ollama.preset.parallel_tool_calls_param is False
    router = OpenAICompatBrain(BrainSpec(backend="openrouter", model="x"))
    assert router.base_url == "https://openrouter.ai/api/v1"
    custom = OpenAICompatBrain(
        BrainSpec(
            backend="openai-compat",
            model="m",
            base_url="http://127.0.0.1:1/v1",
            api_key_env="MY_KEY",
        )
    )
    assert custom.api_key_env == "MY_KEY"
    with pytest.raises(BrainError, match="model name is required"):
        OpenAICompatBrain(BrainSpec(backend="openrouter"))
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(BrainError, match="OLLAMA_API_KEY"):
        ollama._client()
