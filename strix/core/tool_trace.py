"""Runtime ground-truth tool-call trace for scan runs.

Phase 1 of a future structured attack-case engine (AgentDojo declares HOW an
injection is delivered, InjecAgent declares WHAT objective is targeted, this
module records WHAT ACTUALLY HAPPENED — a later phase adds the validator that
decides whether the objective was truly achieved). See the project's
integration plan for the full phase boundary.

Every event here comes from the openai-agents SDK's own ``RunHooks.on_tool_start``
/ ``on_tool_end`` callbacks (wired in :mod:`strix.core.hooks`), which the SDK
invokes for every tool execution path it has — plain ``FunctionTool`` calls
(covers all of Strix's own tools, plus the ``Shell``/``Filesystem`` sandbox
capabilities, which are ``FunctionTool`` subclasses under the hood),
``CustomTool``, ``ShellTool``, ``ComputerTool``, and ``ApplyPatchTool``
(verified against ``agents/run_internal/tool_execution.py`` and
``tool_actions.py`` in the installed SDK — every one of those call sites
passes the same ``hooks`` object through). One hooks instance is shared by the
root agent and every spawned child (see ``strix/core/runner.py``), so this
trace covers the whole agent tree with no extra wiring per agent.

What this module deliberately does NOT do:

- It does not claim a ``status`` (success/blocked/failed) for a tool call.
  The SDK's ``on_tool_end`` callback fires with the call's return value, not
  a verdict — inferring "blocked" or "succeeded" from that would be exactly
  the kind of unearned claim this trace exists to avoid. Read the ``result``
  field yourself, or wait for a future validation phase.
- It does not populate ``attack_id``. There is no structured attack-case
  concept in Strix today (the agent works from free-form natural-language
  instructions, not typed test cases), and bolting a fake ID onto events by
  asking the LLM to declare one would make the ID itself unreliable
  model-generated metadata — the opposite of what this trace is for. The
  field is reserved (always ``None``) so a real orchestration layer can
  populate it later without changing the schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TOOL_TRACE_FILENAME = "tool_trace.jsonl"
TOOL_TRACE_MD_FILENAME = "tool_trace.md"

_MAX_FIELD_CHARS = 2000
_MAX_LIST_ITEMS = 50
_MAX_REDACT_DEPTH = 6
_REDACTED = "[REDACTED]"

# Key names (case-insensitive substring match) whose values are always
# redacted, regardless of content — this catches structured arguments/results
# (dicts) far more reliably than trying to pattern-match every possible
# secret format in free text.
_SENSITIVE_KEY_RE = re.compile(
    r"pass(word)?|secret|token|api[-_]?key|authoriz|credential"
    r"|private[-_]?key|access[-_]?key|cookie|session[-_]?id|bearer",
    re.IGNORECASE,
)

# Best-effort patterns for secrets embedded in free-text strings (not caught
# by the key-based check above, e.g. a raw header line inside a tool result).
# Not exhaustive — documented as a heuristic, not a guarantee.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.~+/]+=*", re.IGNORECASE),
    re.compile(r"(?im)^authorization\s*:\s*\S+.*$"),
    re.compile(r"(?im)^set-cookie\s*:\s*\S+.*$"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),  # Anthropic-style
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub tokens
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),  # Google API key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
)


def _truncate(text: str, limit: int = _MAX_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated, {len(text) - limit} more chars]"


def _redact_string(value: str) -> str:
    out = value
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return _truncate(out)


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys/values and cap size.

    Dict values whose key looks sensitive (password, token, api_key,
    authorization, cookie, ...) are replaced outright. Strings are checked
    against a small set of well-known secret patterns (Bearer tokens, cloud
    API key prefixes, auth/cookie headers) and truncated. Not a guarantee —
    see the module docstring's "what this deliberately does not do".
    """
    if _depth > _MAX_REDACT_DEPTH:
        return "[max depth exceeded]"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_value(val, _depth=_depth + 1)
        return redacted
    if isinstance(value, list):
        items = [redact_value(v, _depth=_depth + 1) for v in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"...[{len(value) - _MAX_LIST_ITEMS} more items truncated]")
        return items
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _redact_string(str(value))


def _parse_tool_arguments(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ToolTraceWriter:
    """Appends redacted ``tool_start``/``tool_end`` events to a JSONL file.

    One instance is created per scan (see ``strix/core/runner.py``) and
    shared by the root agent and every spawned child via the single
    ``ReportUsageHooks`` instance they all use.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._lock = asyncio.Lock()

    async def record_tool_start(
        self,
        *,
        agent_id: str | None,
        tool_name: str,
        tool_call_id: str | None,
        raw_arguments: str | None,
    ) -> None:
        await self._append(
            {
                "timestamp": _now_iso(),
                "run_id": self._run_id,
                "agent_id": agent_id,
                "attack_id": None,
                "event": "tool_start",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": redact_value(_parse_tool_arguments(raw_arguments)),
            }
        )

    async def record_tool_end(
        self,
        *,
        agent_id: str | None,
        tool_name: str,
        tool_call_id: str | None,
        result: Any,
    ) -> None:
        await self._append(
            {
                "timestamp": _now_iso(),
                "run_id": self._run_id,
                "agent_id": agent_id,
                "attack_id": None,
                "event": "tool_end",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "result": redact_value(result),
            }
        )

    async def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        async with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                logger.exception("tool trace append failed (non-fatal)")


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return _truncate(text, limit)


def _render_entry(start: dict[str, Any] | None, end: dict[str, Any] | None) -> str:
    base = end or start
    if base is None:
        raise ValueError("_render_entry requires at least one of start/end")
    timestamp = base.get("timestamp", "")
    agent_id = base.get("agent_id") or "unknown"
    tool_name = base.get("tool_name", "unknown")
    lines = [f"### {timestamp} — agent `{agent_id}` → `{tool_name}`"]
    if start is not None:
        lines.append(f"- Arguments: `{_short(start.get('arguments'))}`")
    else:
        lines.append("- Arguments: _(tool_start event not recorded)_")
    if end is not None:
        lines.append(f"- Result: `{_short(end.get('result'))}`")
    else:
        lines.append(
            "- Result: _(no tool_end observed — call may still be running, "
            "or execution was cancelled before completion)_"
        )
    return "\n".join(lines)


def render_tool_trace_markdown(run_dir: Path) -> str | None:
    """Render ``tool_trace.jsonl`` as a human-readable markdown trace.

    Pairs ``tool_start``/``tool_end`` events by the SDK's own ``tool_call_id``
    where available. Returns ``None`` if no trace file exists or it has no
    events (nothing to render) rather than writing an empty file.
    """
    trace_path = run_dir / TOOL_TRACE_FILENAME
    if not trace_path.exists():
        return None

    starts: dict[str, dict[str, Any]] = {}
    entries: list[tuple[str, str]] = []  # (timestamp, rendered) for stable ordering
    try:
        with trace_path.open(encoding="utf-8") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                call_id = event.get("tool_call_id")
                if event.get("event") == "tool_start":
                    if call_id:
                        starts[call_id] = event
                    else:
                        entries.append((event.get("timestamp", ""), _render_entry(event, None)))
                elif event.get("event") == "tool_end":
                    start = starts.pop(call_id, None) if call_id else None
                    entries.append((event.get("timestamp", ""), _render_entry(start, event)))
    except OSError:
        logger.exception("failed reading tool trace for markdown rendering")
        return None

    # Unmatched starts: a tool_start with no corresponding tool_end — the
    # call was still in flight, cancelled, or crashed before completing.
    entries.extend(
        (start.get("timestamp", ""), _render_entry(start, None)) for start in starts.values()
    )

    if not entries:
        return None

    entries.sort(key=lambda item: item[0])
    header = [
        "# Tool Execution Trace",
        "",
        "Runtime ground truth from the SDK's `on_tool_start`/`on_tool_end` callbacks — "
        "independent of anything an agent says about its own intentions. A `tool_end` "
        "entry means the SDK returned a result for that call; it is not itself a claim "
        "that the underlying action succeeded, was authorized, or was blocked. Read the "
        "result content below, or the raw `tool_trace.jsonl` in this directory.",
        "",
    ]
    return "\n".join([*header, *(rendered for _, rendered in entries)]) + "\n"


def write_tool_trace_summary(run_dir: Path) -> None:
    """Regenerate ``tool_trace.md`` from ``tool_trace.jsonl``, if present.

    Safe to call repeatedly (e.g. once per ``ReportState._save_artifacts``
    call, like the SARIF emitter) — always overwrites with the current full
    trace. A rendering failure never raises; callers isolate this the same
    way the SARIF emitter is isolated.
    """
    rendered = render_tool_trace_markdown(run_dir)
    if rendered is None:
        return
    (run_dir / TOOL_TRACE_MD_FILENAME).write_text(rendered, encoding="utf-8")
