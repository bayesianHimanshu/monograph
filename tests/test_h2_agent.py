"""The H2 agent loop and arms, with a scripted fake client (CI never calls the API)."""

from __future__ import annotations

from typing import Any

from monograph.agent.arms import ARMS, SUBMIT, Arm, free_arm, load_prompt, system_prompt, tools_arm
from monograph.agent.llm import AGENT_SETTINGS, FakeToolClient, scripted, tool_use
from monograph.agent.loop import NUDGE, AgentLoop
from monograph.tools.freeform import CypherRunner, SqlRunner
from monograph.tools.registry import ToolError
from monograph.tools.service import ToolService

THINK = {"type": "thinking", "thinking": "plan", "signature": "sig-1"}


def _arm(execute: Any = None) -> Arm:
    def default(name: str, args: dict[str, Any]) -> str:
        if args.get("fail"):
            raise ToolError("query error: boom")
        return '{"rows": [[1]]}'

    return Arm(
        "S-free",
        "sql",
        "free",
        ({"name": "run_sql", "description": "", "input_schema": {}}, SUBMIT),
        execute or default,
    )


def _loop(turns: list[Any]) -> tuple[AgentLoop, FakeToolClient]:
    client = FakeToolClient(scripted(turns))
    return AgentLoop(client, _arm(), "claude-haiku-5-5", "system", AGENT_SETTINGS), client


def test_answers_and_replays_content_blocks_unchanged() -> None:
    first = [THINK, tool_use("run_sql", {"query": "SELECT 1"}, "t1")]
    loop, client = _loop([first, [tool_use("submit_answer", {"kind": "number", "number": 1, "evidence": [1]}, "t2")]])
    t = loop.run("q1", "How many?")
    assert t.outcome == "answered"
    assert t.answer == {"kind": "number", "ids": [], "number": 1, "evidence": [1]}
    assert [c.name for c in t.calls] == ["run_sql"]
    second = client.requests[1].messages
    assert second[1] == {"role": "assistant", "content": first}
    assert second[2]["content"][0]["tool_use_id"] == "t1"
    assert t.turns == 2
    assert t.usage["output_tokens"] == 600


def test_tool_errors_go_back_to_the_model() -> None:
    loop, client = _loop(
        [[tool_use("run_sql", {"query": "x", "fail": True}, "a")], [tool_use("submit_answer", {"kind": "none"}, "b")]]
    )
    t = loop.run("q", "?")
    assert t.calls[0].is_error
    assert client.requests[1].messages[2]["content"][0]["is_error"] is True
    assert t.answer is not None
    assert t.answer["kind"] == "none"


def test_the_thirteenth_data_call_is_refused() -> None:
    calls = [tool_use("run_sql", {"query": f"SELECT {i}"}, f"c{i}") for i in range(13)]
    loop, client = _loop([calls, [tool_use("submit_answer", {"kind": "none"}, "s")]])
    t = loop.run("q", "?")
    assert len(t.calls) == 12
    last = client.requests[1].messages[2]["content"][-1]
    assert last["is_error"] is True
    assert "budget exhausted" in last["content"]
    assert t.outcome == "answered"


def test_refusal_is_recorded_not_retried() -> None:
    loop, client = _loop([([{"type": "text", "text": "no"}], "refusal")])
    t = loop.run("q", "?")
    assert t.outcome == "refusal"
    assert len(client.requests) == 1


def test_one_nudge_then_no_answer() -> None:
    loop, client = _loop([[{"type": "text", "text": "I think 3"}], [{"type": "text", "text": "3"}]])
    t = loop.run("q", "?")
    assert t.outcome == "no_answer"
    assert client.requests[1].messages[-1] == {"role": "user", "content": NUDGE}


def test_an_invalid_answer_gets_an_error_and_another_turn() -> None:
    loop, _ = _loop(
        [[tool_use("submit_answer", {"kind": "maybe"}, "a")], [tool_use("submit_answer", {"kind": "none"}, "b")]]
    )
    t = loop.run("q", "?")
    assert t.outcome == "answered"
    assert t.turns == 2


def test_turn_budget() -> None:
    turns = [[tool_use("run_sql", {"query": "SELECT 1"}, f"c{i}")] for i in range(AGENT_SETTINGS.max_turns)]
    loop, _ = _loop(turns)
    assert loop.run("q", "?").outcome == "budget_exhausted"


def test_every_arm_gets_the_same_base_prompt() -> None:
    base = load_prompt("base", "v1").rstrip()
    for arm in ARMS:
        assert system_prompt(arm).startswith(base)


def test_tools_arms_have_identical_tool_definitions() -> None:
    sql = tools_arm("S-tools", ToolService(None))  # type: ignore[arg-type]
    graph = tools_arm("G-tools", ToolService(None))  # type: ignore[arg-type]
    assert sql.tools == graph.tools
    assert sql.tools_hash() == graph.tools_hash()
    assert sql.tools[-1] == SUBMIT


def test_free_arms_differ_only_in_the_query_tool() -> None:
    s = free_arm("S-free", SqlRunner(None, ""))  # type: ignore[arg-type]
    g = free_arm("G-free", CypherRunner(None, None))  # type: ignore[arg-type]
    assert [t["name"] for t in s.tools] == ["describe_schema", "run_sql", "submit_answer"]
    assert [t["name"] for t in g.tools] == ["describe_schema", "run_cypher", "submit_answer"]
    assert s.tools[0] == g.tools[0]
    assert s.tools[1]["input_schema"] == g.tools[1]["input_schema"]


def test_arms_draw_independent_samples() -> None:
    from monograph.agent.llm import ToolRequest

    a = ToolRequest("m", "s", (), (), AGENT_SETTINGS, "S-tools")
    b = ToolRequest("m", "s", (), (), AGENT_SETTINGS, "G-tools")
    assert a.key() != b.key()
