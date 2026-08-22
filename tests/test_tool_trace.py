"""Tests for the runtime ground-truth tool-call trace (strix.core.tool_trace)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from strix.core.tool_trace import (
    ToolTraceWriter,
    redact_value,
    render_tool_trace_markdown,
    write_tool_trace_summary,
)


if TYPE_CHECKING:
    from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestRedaction:
    def test_sensitive_keys_redacted_regardless_of_value(self) -> None:
        out = redact_value({"password": "hunter2", "api_key": "sk-abc123", "note": "hello world"})
        assert out["password"] == "[REDACTED]"  # noqa: S105
        assert out["api_key"] == "[REDACTED]"
        assert out["note"] == "hello world"

    def test_bearer_token_in_free_text_redacted(self) -> None:
        out = redact_value("curl -H 'Authorization: Bearer sk-ant-abcdefgh12345678' example.com")
        assert "sk-ant-abcdefgh12345678" not in out
        assert "[REDACTED]" in out

    def test_authorization_header_line_redacted(self) -> None:
        out = redact_value("Authorization: Basic dXNlcjpwYXNz\nContent-Type: text/html")
        assert "dXNlcjpwYXNz" not in out
        assert "Content-Type: text/html" in out

    def test_nested_structures_redacted(self) -> None:
        out = redact_value({"headers": {"cookie": "session=deadbeef"}, "ok": True})
        assert out["headers"]["cookie"] == "[REDACTED]"
        assert out["ok"] is True

    def test_long_string_truncated(self) -> None:
        out = redact_value("x" * 5000)
        assert len(out) < 5000
        assert "truncated" in out

    def test_long_list_truncated(self) -> None:
        out = redact_value(list(range(200)))
        assert len(out) <= 51  # 50 items + one truncation marker
        assert "truncated" in out[-1]

    def test_non_json_native_value_stringified_and_redacted(self) -> None:
        class Thing:
            def __str__(self) -> str:
                return "Authorization: Bearer sk-ant-zzzzzzzzzzzzzzzz"

        out = redact_value(Thing())
        assert "sk-ant-zzzzzzzzzzzzzzzz" not in out


class TestToolTraceWriter:
    async def test_start_and_end_events_written(self, tmp_path: Path) -> None:
        path = tmp_path / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_start(
            agent_id="root",
            tool_name="search_email",
            tool_call_id="call-1",
            raw_arguments='{"query": "invoice"}',
        )
        await writer.record_tool_end(
            agent_id="root",
            tool_name="search_email",
            tool_call_id="call-1",
            result="3 messages found",
        )
        events = _read_jsonl(path)
        assert len(events) == 2
        assert events[0]["event"] == "tool_start"
        assert events[0]["arguments"] == {"query": "invoice"}
        assert events[0]["attack_id"] is None
        assert events[0]["run_id"] == "run-1"
        assert events[1]["event"] == "tool_end"
        assert events[1]["result"] == "3 messages found"

    async def test_failed_tool_call_result_captured_verbatim(self, tmp_path: Path) -> None:
        """A tool that errored is still faithfully recorded — no invented status."""
        path = tmp_path / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_end(
            agent_id="root",
            tool_name="send_email",
            tool_call_id="call-2",
            result="Error: recipient domain blocked by egress policy",
        )
        events = _read_jsonl(path)
        assert events[0]["result"] == "Error: recipient domain blocked by egress policy"
        # Deliberately no "status" field — see tool_trace.py's module docstring.
        assert "status" not in events[0]

    async def test_multiple_agents_retain_distinct_identity(self, tmp_path: Path) -> None:
        path = tmp_path / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_start(
            agent_id="root", tool_name="create_agent", tool_call_id="c1", raw_arguments="{}"
        )
        await writer.record_tool_start(
            agent_id="child-abc123",
            tool_name="search_email",
            tool_call_id="c2",
            raw_arguments="{}",
        )
        events = _read_jsonl(path)
        agent_ids = {e["agent_id"] for e in events}
        assert agent_ids == {"root", "child-abc123"}

    async def test_arguments_and_results_are_useful_not_just_present(self, tmp_path: Path) -> None:
        path = tmp_path / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_start(
            agent_id="root",
            tool_name="lookup_order",
            tool_call_id="c1",
            raw_arguments='{"order_id": "ORD-42"}',
        )
        events = _read_jsonl(path)
        assert events[0]["arguments"]["order_id"] == "ORD-42"

    async def test_secrets_not_written_in_plaintext(self, tmp_path: Path) -> None:
        path = tmp_path / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_start(
            agent_id="root",
            tool_name="exec_command",
            tool_call_id="c1",
            raw_arguments='{"cmd": "curl -H \\"Authorization: Bearer sk-ant-supersecrettoken1\\""}',
        )
        raw_text = path.read_text(encoding="utf-8")
        assert "sk-ant-supersecrettoken1" not in raw_text

    async def test_artifact_persists_after_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "tool_trace.jsonl"
        writer = ToolTraceWriter(path, run_id="run-1")
        await writer.record_tool_start(
            agent_id="root", tool_name="x", tool_call_id="c1", raw_arguments=None
        )
        assert path.exists()
        assert len(_read_jsonl(path)) == 1


class TestMarkdownRendering:
    def test_no_trace_file_returns_none(self, tmp_path: Path) -> None:
        assert render_tool_trace_markdown(tmp_path) is None

    async def test_paired_events_render_one_entry(self, tmp_path: Path) -> None:
        writer = ToolTraceWriter(tmp_path / "tool_trace.jsonl", run_id="run-1")
        await writer.record_tool_start(
            agent_id="root", tool_name="lookup_order", tool_call_id="c1", raw_arguments="{}"
        )
        await writer.record_tool_end(
            agent_id="root", tool_name="lookup_order", tool_call_id="c1", result="ok"
        )
        rendered = render_tool_trace_markdown(tmp_path)
        assert rendered is not None
        assert rendered.count("lookup_order") == 1
        assert "Runtime ground truth" in rendered

    async def test_unmatched_start_still_rendered(self, tmp_path: Path) -> None:
        writer = ToolTraceWriter(tmp_path / "tool_trace.jsonl", run_id="run-1")
        await writer.record_tool_start(
            agent_id="root", tool_name="exec_command", tool_call_id="c1", raw_arguments="{}"
        )
        rendered = render_tool_trace_markdown(tmp_path)
        assert rendered is not None
        assert "no tool_end observed" in rendered

    async def test_write_tool_trace_summary_creates_md_file(self, tmp_path: Path) -> None:
        writer = ToolTraceWriter(tmp_path / "tool_trace.jsonl", run_id="run-1")
        await writer.record_tool_start(
            agent_id="root", tool_name="x", tool_call_id="c1", raw_arguments=None
        )
        write_tool_trace_summary(tmp_path)
        assert (tmp_path / "tool_trace.md").exists()

    def test_write_tool_trace_summary_noop_without_jsonl(self, tmp_path: Path) -> None:
        write_tool_trace_summary(tmp_path)
        assert not (tmp_path / "tool_trace.md").exists()
