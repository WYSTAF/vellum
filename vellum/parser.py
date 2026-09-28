"""Turn Claude Code's JSONL into a normalized message model.

The on-disk format is undocumented and noisy: user turns carry injected
``<system-reminder>`` blocks, tool results arrive as ``user`` entries, and the
interesting parts (thinking, tool calls, sub-agent transcripts) are mixed in
with plumbing. This module decides once, at index time, what each entry really
is — everything downstream (reading, searching, exporting, stats) reads the
normalized rows instead of re-deciding.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

try:  # ~2.2x faster than the stdlib decoder on this data
    import orjson

    def _loads(raw: bytes | str) -> Any:
        return orjson.loads(raw)
except ImportError:  # pragma: no cover
    def _loads(raw: bytes | str) -> Any:
        return json.loads(raw)


# tool output is capped: a single `cat` can be 2 MB and is not worth indexing
TOOL_TEXT_CAP = 6000
THINKING_CAP = 20000
PREVIEW_LEN = 220

# Attachment types that are harness bookkeeping rather than conversation.
#
# The first group is pure noise: 210k rows on this archive, none of it readable
# conversation. The second group is *real* but is metadata about the session,
# not part of it -- an environment snapshot, the date, a system-prompt copy.
# Keeping those in the transcript pushed the first human message five rows
# down the page, which is the difference between opening a conversation and
# scrolling through Claude Code's scaffolding to find one. They are still
# parsed, and still recorded on the session row, just not rendered inline.
NOISE_ATTACHMENTS = frozenset({
    "total_tokens_reminder", "task_reminder", "skill_listing",
    "agent_listing_delta", "mcp_instructions_delta", "command_permissions",
    "hook_success", "hook_additional_context", "read_truncation_notice",
    "remote_session_change", "date_change", "task_status",
})

# Session metadata: kept out of the message stream, surfaced as facts instead.
META_ATTACHMENTS = frozenset({
    "environment", "date", "session_context", "prompt_snapshot",
    "plan_mode", "plan_mode_exit", "file", "instructions", "model",
    "compact_file_reference",
})

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I)
_INSTRUCTIONS = re.compile(r"<instructions>.*?</instructions>", re.S | re.I)
_COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S | re.I)
_COMMAND_MESSAGE = re.compile(r"<command-message>.*?</command-message>", re.S | re.I)
_COMMAND_STDOUT = re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S | re.I)
_COMMAND_CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S | re.I)
_TOOL_RESULT_HEADER = re.compile(r"<command-args>.*?</command-args>", re.S | re.I)
_STRAY_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:_-]*\s*/?>")
_INTERRUPTED = re.compile(r"\[Request interrupted[^\]]*\]")
_LEAD_INJECTION = re.compile(r"^(?:This session is being continued|Caveat:)", re.I)

USER_INJECT_PREFIXES = (
    "<system-reminder>", "<task-notification>", "<command-name>", "<local-command",
    "<instructions>", "<>user-prompt-submit-hook",
)


def clean_user_text(text: str) -> str:
    """Strip harness injections from a human turn; keep real input and slash commands."""
    if not text:
        return ""
    t = _COMMAND_CAVEAT.sub(" ", text)
    t = _COMMAND_STDOUT.sub(" ", t)
    # /command-name -> keep the command itself, it is part of what you typed
    names = [m.group(1).strip() for m in _COMMAND_NAME.finditer(t) if m.group(1).strip()]
    t = _COMMAND_NAME.sub(" ", t)
    t = _COMMAND_MESSAGE.sub(" ", t)
    t = _COMMAND_STDOUT.sub(" ", t)
    t = _TOOL_RESULT_HEADER.sub(" ", t)
    t = _REMINDER.sub(" ", t)
    t = _INSTRUCTIONS.sub(" ", t)
    t = _INTERRUPTED.sub(" ", t)
    if "<" in t:  # leftover or unterminated tags: degrade, never leak markup
        t = _STRAY_TAG.sub(" ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if names:
        t = "  \n".join(dict.fromkeys(names) if t else names) + ("\n" + t if t else "")
    if t.startswith("Caveat: "):
        return ""
    if _LEAD_INJECTION.match(t):
        return ""
    return t


def _blocks(content: Any) -> Iterator[dict]:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                yield item


def _flatten(content: Any, cap: int = TOOL_TEXT_CAP) -> tuple[str, int]:
    """Text + image count for tool_result-ish content (string, block list, nested)."""
    if isinstance(content, str):
        return content.strip()[:cap], 0
    parts: list[str] = []
    images = 0
    for blk in _blocks(content):
        ty = blk.get("type")
        if ty == "text":
            parts.append(blk.get("text") or "")
        elif ty == "image":
            images += 1
        elif ty == "tool_result":
            sub, im = _flatten(blk.get("content"), cap)
            if sub:
                parts.append(sub)
            images += im
        elif ty == "document":
            parts.append("[document]")
    text = "\n".join(p for p in parts if p).strip()
    if images and not text:
        text = f"[{images} image{'s' if images > 1 else ''}]"
    elif images:
        text += f"\n[{images} image{'s' if images > 1 else ''}]"
    return text[:cap], images


# --- tool call summaries ---------------------------------------------------
def _summarize(name: str, args: Any) -> str:
    """One line describing what a tool call did — shown in the collapsed card.

    A card with no label at all reads as a rendering failure, so this always
    returns something: the tool's own name is better than a blank.
    """
    if not isinstance(args, dict):
        return name or ""
    # Claude Code records a call it could not parse under a private key and
    # leaves the real arguments as a JSON string, sometimes wrapped in a
    # {"raw": ...} envelope. Measured on this archive: 54,000 Bash calls
    # landed here, which is why so many tool cards had no label.
    if "__unparsedToolInput" in args and len(args) == 1:
        unparsed = args["__unparsedToolInput"]
        if isinstance(unparsed, dict):
            unparsed = unparsed.get("raw", unparsed)
        if isinstance(unparsed, str):
            try:
                unparsed = json.loads(unparsed)
            except ValueError:
                return " ".join(unparsed.split())[:160]
        if isinstance(unparsed, dict):
            args = unparsed
    def s(key: str, n: int = 140) -> str:
        v = args.get(key)
        return str(v).replace("\n", " ").strip()[:n] if isinstance(v, (str, int, float)) else ""

    if name == "Bash":
        cmd = s("command", 200)
        if cmd:
            return cmd.split(" && ")[0]
        return s("description", 120) or name
    if name in {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}:
        p = s("file_path", 200)
        base = os.path.basename(p.replace("\\", "/").rstrip("/")) or p
        extra = ""
        if name in {"Read"} and ("offset" in args or "limit" in args):
            # offset/limit come from JSON and are not guaranteed numeric --
            # "limit": "20" raised TypeError here and cost the whole session.
            def _i(key: str) -> int:
                try:
                    return int(args.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
            start_line = _i("offset") or 1
            lim = _i("limit")
            extra = f"  · lines {start_line}-{start_line + lim}" if lim                 else f"  · from line {start_line}"
        elif name in {"Edit", "MultiEdit", "Write"}:
            rep = args.get("replace_all")
            extra = "  · replace all" if rep else ""
        return base + extra
    if name in {"Glob", "find"}:
        return s("pattern", 160)
    if name == "Grep":
        pat = s("pattern", 120)
        glob = s("glob", 60) or s("path", 60)
        return f"{pat}" + (f"  ·  {glob}" if glob else "")
    if name in {"WebFetch"}:
        return s("url", 160)
    if name in {"WebSearch"}:
        return s("query", 160)
    if name in {"Task", "Agent"}:
        t = s("subagent_type", 40)
        d = s("description", 100) or s("prompt", 100)
        return (t + (" · " if t and d else "") + d).strip()
    if name == "Skill":
        return s("skill", 80)
    if name in {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}:
        todos = args.get("todos")
        if isinstance(todos, list):
            return f"{len(todos)} items"
        return s("subject", 120) or s("status", 30) or s("description", 120)
    # Task/agent lifecycle tools. These carry only an id, which is useless in
    # a list; naming what was run is the point of the label.
    if name in {"TaskOutput", "TaskStop", "Task", "Agent"}:
        return (s("description", 110) or s("subagent_type", 60)
                or s("taskId", 40) or s("prompt", 110))
    if name in {"CronCreate", "CronList", "CronDelete", "ScheduleWakeup"}:
        return s("cron", 60) or s("prompt", 120) or s("action", 40)
    if name == "AskUserQuestion":
        return s("question", 160)
    if name == "SendMessage":
        return s("to", 40)
    if name == "SendUserFile":
        return (s("caption", 110) or s("files", 110) or s("file_path", 110))
    if name in {"EnterPlanMode", "ExitPlanMode", "ExitWorktree"}:
        return s("plan", 120)
    if name == "NotebookRead" or name == "NotebookEdit":
        return s("notebook_path", 120)
    if name.startswith("mcp__"):
        for k in ("path", "url", "query", "prompt", "message", "name", "session_id",
                  "taskId", "selector", "expression", "content", "title", "command"):
            v = s(k, 120)
            if v:
                return v
        # An MCP tool with only opaque arguments still deserves *a* label; an
        # empty card reads as a rendering failure.
        keys = ", ".join(sorted(str(k) for k in args)[:3])
        return f"({keys})" if keys else ""
    for key in ("prompt", "text", "message", "content", "command", "path", "name", "query"):
        v = s(key, 160)
        if v:
            return v
    keys = ", ".join(sorted(str(k) for k in args)[:3])
    return f"({keys})" if keys else (name or "")


def summarize_tool(name: str, args: Any) -> str:
    """One line describing what a tool call did -- shown in the collapsed card.

    Every branch above can return an empty string for a tool called with no
    usable arguments, and an empty label reads as a rendering failure rather
    than as "this tool took no arguments". So the name is the floor.

    The whole thing is wrapped because the format is undocumented: a Read with
    ``"limit": "20"`` raised TypeError inside the f-string, and because that
    escapes ``_consume`` into ``parse_file`` (which only catches OSError), a
    single malformed field cost the entire conversation. A label is never worth
    a file.
    """
    try:
        return _summarize(name, args) or name or ""
    except Exception:  # noqa: BLE001 -- a label is never worth losing a file
        return name or ""


# --- normalized model ------------------------------------------------------
@dataclass
class Msg:
    ordinal: int
    role: str            # user | assistant | thinking | tool | result | system | attachment | meta
    text: str
    ts: str | None = None
    model: str | None = None
    tool: str | None = None       # tool name (role tool) or 'error'/'' (role result)
    label: str = ""               # one-line summary for tool cards
    in_tok: int = 0
    out_tok: int = 0
    cache_tok: int = 0
    sidechain: bool = False
    images: int = 0
    duration_s: float | None = None  # assistant: seconds since the turn it answers
    subtype: str | None = None
    uuid: str | None = None
    parent_uuid: str | None = None


@dataclass
class ParsedSession:
    id: str
    kind: str                     # main | subagent
    project: str
    parent_dir: str
    path: str
    parent_id: str | None = None
    title: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    last_prompt: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    os_version: str | None = None
    shell: str | None = None
    version: str | None = None
    entrypoint: str | None = None
    model: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    bytes: int = 0
    mtime: float = 0.0
    messages: list[Msg] = field(default_factory=list)
    compact_count: int = 0
    error_count: int = 0

    # derived, filled by finalize()
    n_turns: int = 0
    n_user: int = 0
    n_assistant: int = 0
    n_tool: int = 0
    in_tok: int = 0
    out_tok: int = 0
    cache_tok: int = 0
    cache_read: int = 0
    cache_create: int = 0
    thinking_tok: int = 0
    preview: str = ""
    ribbon: list = field(default_factory=list)
    duration_s: float = 0.0

    def finalize(self) -> "ParsedSession":
        prev_u: Msg | None = None
        for m in self.messages:
            if m.role == "user":
                self.n_user += 1
                prev_u = m
            elif m.role == "assistant":
                self.n_assistant += 1
                self.in_tok += m.in_tok
                self.out_tok += m.out_tok
                self.cache_tok += m.cache_tok
                if prev_u is not None and m.duration_s is None:
                    m.duration_s = _span(prev_u.ts, m.ts)
            elif m.role == "tool":
                self.n_tool += 1
                self.in_tok += m.in_tok
                self.out_tok += m.out_tok
        self.n_turns = self.n_user
        # The preview must be derived *before* the title falls back to it --
        # computing the title first made every untitled session fall back to
        # its session id, which is what put 67 raw uuids in the list.
        if not self.preview:
            for m in self.messages:
                if m.role == "user" and m.text.strip():
                    self.preview = " ".join(m.text.split())[:PREVIEW_LEN]
                    break
        if not self.preview and self.messages:
            self.preview = " ".join((self.messages[0].text or "").split())[:PREVIEW_LEN]
        if not self.title:
            # last_prompt is the real first prompt and beats a truncated
            # preview; the id is the last resort and is never a good title.
            self.title = _fallback_title(
                self.last_prompt or self.preview or self.id
            )
        self.ribbon = build_ribbon(self.messages)
        self.duration_s = _span(self.first_ts, self.last_ts) or 0.0
        return self


def _span(a: str | None, b: str | None) -> float | None:
    from datetime import datetime

    if not a or not b:
        return None
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (db - da).total_seconds())


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(:[\w-]+)?$",
    re.I,
)
_URL_PREFIX_RE = re.compile(r"^https?://\S+\s+", re.I)


def _fallback_title(text: str) -> str:
    """A readable title from the first thing the user said.

    A session id is never an acceptable title -- 17% of this archive has no
    custom or AI title, and showing a raw uuid in the list is worse than
    showing the prompt that started it.
    """
    t = " ".join((text or "").split())
    if not t or _UUID_RE.match(t):
        return "Untitled conversation"
    # A pasted URL is a title like "https://github.com/x.git" says nothing;
    # the sentence after it is what the conversation is actually about.
    stripped = _URL_PREFIX_RE.sub("", t)
    if stripped:
        t = stripped
    if len(t) > 90:
        # Cut on a word boundary so the title does not end mid-word.
        cut = t[:90].rsplit(" ", 1)[0]
        t = (cut or t[:90]) + "…"
    return t


RIBBON_BINS = 28


def build_ribbon(messages: list[Msg], bins: int = RIBBON_BINS) -> list:
    """Exchange density over time: per bin, (you, claude, machine) counts.

    One small array per conversation drives both the list-row strip and the
    reader minimap, so the same data is drawn twice and never recomputed.
    """
    stamps = [m.ts for m in messages if m.ts]
    if len(stamps) < 2:
        counts = {"y": 0, "c": 0, "m": 0}
        for m in messages:
            counts["y" if m.role == "user" else "c" if m.role == "assistant" else "m"] += 1
        total = sum(counts.values()) or 1
        half = bins // 2
        out = []
        for i in range(bins):
            if i < half:
                out.append({"y": 0, "c": 0, "m": 0})
            elif i == half:
                out.append({"y": min(counts["y"], 9), "c": min(counts["c"], 9), "m": min(counts["m"], 9)})
            else:
                out.append({"y": 0, "c": 0, "m": 0})
        return out
    from datetime import datetime

    def epoch(s: str) -> float:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    t0, t1 = epoch(stamps[0]), epoch(stamps[-1])
    span = max(1.0, t1 - t0)
    grid = [[0, 0, 0] for _ in range(bins)]
    for m in messages:
        if not m.ts:
            continue
        idx = int(((epoch(m.ts) - t0) / span) * (bins - 1) + 0.5)
        idx = min(bins - 1, max(0, idx))
        if m.role == "user":
            grid[idx][0] += 1
        elif m.role == "assistant":
            grid[idx][1] += 1
        else:
            grid[idx][2] += 1
    return [{"y": g[0], "c": g[1], "m": g[2]} for g in grid]


def session_id_for(path: str, kind: str = "main") -> tuple[str, str | None, str]:
    """(session_id, parent_id, project) for a transcript path.

    Both the parser and the indexer have to agree on this. They did not: the
    indexer derived the id from the filename while the parser prefixed a
    sub-agent with its parent's, so every sub-agent row was "missing" on every
    scan -- deleted, then re-parsed. 74 of 411 files re-parsed each time, at
    2.3-3.7s each because deleting from an FTS table by an UNINDEXED column is
    a full scan. Roughly five minutes of waste per no-op rescan.
    """
    fname = os.path.basename(path)
    stem = fname[:-6] if fname.endswith(".jsonl") else fname
    if kind == "subagent":
        parent_id = os.path.basename(os.path.dirname(os.path.dirname(path)))
        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        return f"{parent_id}:{stem}", parent_id, project
    return stem, None, os.path.basename(os.path.dirname(path))


def parse_file(path: str, kind: str = "main") -> ParsedSession | None:
    """Parse one JSONL transcript. Returns None for empty/unreadable files."""
    session_id, parent_id, project = session_id_for(path, kind)
    d1 = os.path.basename(os.path.dirname(path))
    try:
        st = os.stat(path)
    except OSError:
        return None
    ps = ParsedSession(
        id=session_id, kind=kind, project=project,
        parent_dir=d1 if kind != "subagent" else os.path.basename(os.path.dirname(os.path.dirname(path))),
        path=path, parent_id=parent_id, bytes=st.st_size, mtime=st.st_mtime,
    )
    ordn = 0
    pending_tool: dict[str, Msg] = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    e = _loads(raw)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                ordn = _consume(ps, e, ordn, pending_tool)
    except OSError:
        if not ps.messages:
            return None
    if not ps.messages and not ps.custom_title and not ps.ai_title:
        return None
    ps.messages.sort(key=lambda m: (m.ts or "", m.ordinal))
    for i, m in enumerate(ps.messages):
        m.ordinal = i
    return ps.finalize()


def _consume(ps: ParsedSession, e: dict, ordn: int, pending_tool: dict) -> int:
    ty = e.get("type")
    ts = e.get("timestamp")
    if isinstance(ts, str) and ts:
        if not ps.first_ts:
            ps.first_ts = ts
        ps.last_ts = ts
    if ps.cwd is None:
        ps.cwd = e.get("cwd") or None
    if ps.git_branch is None:
        ps.git_branch = e.get("gitBranch") or None
    if ps.version is None:
        ps.version = e.get("version") or None
    if ps.entrypoint is None:
        ps.entrypoint = e.get("entrypoint") or None
    side = bool(e.get("isSidechain"))
    uuid = e.get("uuid")
    puuid = e.get("parentUuid")

    if ty == "custom-title":
        v = e.get("customTitle")
        if isinstance(v, str) and v.strip():
            ps.custom_title = v.strip()
            ps.title = ps.custom_title
        return ordn
    if ty == "ai-title":
        v = e.get("aiTitle")
        if isinstance(v, str) and v.strip():
            ps.ai_title = v.strip()
            ps.title = ps.title or ps.ai_title
        return ordn
    if ty == "last-prompt":
        v = e.get("lastPrompt")
        if isinstance(v, str) and v.strip():
            ps.last_prompt = " ".join(v.split())[:400]
        return ordn
    if ty in ("mode", "queue-operation", "atis-latch", "pr-link", "handoff", "goal-status"):
        return ordn

    msg = e.get("message")
    if ty == "user":
        if not isinstance(msg, dict):
            return ordn
        content = msg.get("content")
        blocks = list(_blocks(content))
        only_results = bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)
        if only_results:
            for b in blocks:
                text, imgs = _flatten(b.get("content"))
                # Carry the tool's name from its call onto its result, so the
                # reader can say "Bash" rather than an anonymous "result".
                called = None
                if b.get("tool_use_id"):
                    call = pending_tool.pop(str(b["tool_use_id"]), None)
                    called = call.tool if call else None
                is_err = bool(b.get("is_error"))
                first_line = text.split("\n", 1)[0] if text else ""
                ps.messages.append(Msg(
                    ordinal=ordn, role="result", text=text, ts=ts, images=imgs,
                    tool=("error" if is_err else (called or "")),
                    label=first_line,
                    sidechain=side, uuid=uuid, parent_uuid=puuid,
                ))
                ordn += 1
            return ordn
        if isinstance(content, str):
            raw_text, imgs = content, 0
        else:
            raw_text, imgs = _flatten(content, cap=200000)
        text = clean_user_text(raw_text)
        if not text:
            return ordn
        if isinstance(content, list):
            imgs = sum(1 for b in blocks if b.get("type") == "image")
        ps.messages.append(Msg(ordinal=ordn, role="user", text=text, ts=ts, images=imgs,
                               sidechain=side, uuid=uuid, parent_uuid=puuid))
        return ordn + 1

    if ty == "assistant":
        if not isinstance(msg, dict):
            return ordn
        model = msg.get("model") or None
        if model:
            ps.model = ps.model or model
        usage = msg.get("usage")
        it = ot = ct = cr = cc = th = 0
        if isinstance(usage, dict):
            # The format is undocumented, so a counter may arrive as a string,
            # a float, or a null. A crash here loses the whole session, so
            # anything unparseable counts as zero rather than raising.
            def num(key: str) -> int:
                try:
                    return int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    return 0

            it = num("input_tokens")
            ot = num("output_tokens")
            # Both cache counters matter. cache_creation is routinely the
            # largest single component of a long session, and all three
            # previous versions of this tool read cache_read only -- so every
            # token total they showed was short by the biggest term.
            cr = num("cache_read_input_tokens")
            cc = num("cache_creation_input_tokens")
            ct = cr + cc
            details = usage.get("output_tokens_details")
            if isinstance(details, dict):
                try:
                    th = int(details.get("thinking_tokens") or 0)
                except (TypeError, ValueError):
                    th = 0
        content = msg.get("content")
        added = False
        for blk in _blocks(content):
            btype = blk.get("type")
            if btype == "text":
                t = (blk.get("text") or "").strip()
                if not t:
                    continue
                ps.messages.append(Msg(ordn, "assistant", t, ts=ts, model=model,
                                       in_tok=it if not added else 0,
                                       out_tok=ot if not added else 0,
                                       cache_tok=ct if not added else 0,
                                       sidechain=side, uuid=uuid, parent_uuid=puuid))
                if not added:
                    # Usage belongs to the whole assistant turn, so it is
                    # recorded on the first text block and accumulated here --
                    # one assistant record can carry several blocks.
                    ps.cache_read += cr
                    ps.cache_create += cc
                    ps.thinking_tok += th
                ordn += 1
                added = True
            elif btype == "thinking":
                t = (blk.get("thinking") or "").strip()
                if t:
                    ps.messages.append(Msg(ordn, "thinking", t[:THINKING_CAP], ts=ts, model=model,
                                           sidechain=side, uuid=uuid, parent_uuid=puuid))
                    ordn += 1
            elif btype == "tool_use":
                name = str(blk.get("name") or "tool")
                args = blk.get("input")
                try:
                    body = json.dumps(args, ensure_ascii=False)
                except Exception:
                    body = str(args)
                if len(body) > TOOL_TEXT_CAP:
                    body = body[:TOOL_TEXT_CAP] + "…"
                label = summarize_tool(name, args)
                m = Msg(ordn, "tool", body, ts=ts, tool=name, label=label, sidechain=side,
                        uuid=uuid, parent_uuid=puuid)
                ps.messages.append(m)
                if blk.get("id"):
                    pending_tool[str(blk["id"])] = m
                ordn += 1
                added = True
        return ordn

    if ty == "system":
        sub = e.get("subtype")
        ps.model = ps.model
        if sub == "compact_boundary":
            ps.compact_count += 1
        if sub in ("api_error", "local_command", "informational", "compact_boundary", "stop_hook_summary"):
            payload = e.get("message")
            if isinstance(payload, dict):
                payload = payload.get("content") or payload.get("message")
            err = e.get("error")
            if isinstance(err, dict):
                payload = err.get("formatted") or err.get("message") or payload
            if sub == "api_error":
                ps.error_count += 1
                text = str(payload or "API error")[:TOOL_TEXT_CAP]
                ps.messages.append(Msg(ordn, "system", text, ts=ts, subtype=sub,
                                       tool="error", sidechain=side, uuid=uuid, parent_uuid=puuid))
                return ordn + 1
            text = " ".join(str(payload or "").split())[:TOOL_TEXT_CAP]
            if text:
                ps.messages.append(Msg(ordn, "system", text, ts=ts, subtype=sub,
                                       sidechain=side, uuid=uuid, parent_uuid=puuid))
                return ordn + 1
        return ordn

    if ty == "attachment":
        att = e.get("attachment")
        if not isinstance(att, dict):
            return ordn
        atype = str(att.get("type") or "attachment")
        # Harness plumbing, not conversation. Measured on this archive:
        # total_tokens_reminder is 186,046 rows / 9 MB and task_reminder is
        # 24,100 rows / 64 MB -- together 73% of every attachment row and 84%
        # of their bytes, and none of it is something a reader ever wants to
        # see. Skill listings, command permissions and hook success are the
        # same category for a different reason.
        if atype in NOISE_ATTACHMENTS:
            return ordn
        if atype in META_ATTACHMENTS:
            # Recorded where it is useful -- on the session -- and not inline.
            if atype == "environment" and isinstance(att.get("snapshot"), dict):
                snap = att["snapshot"]
                ps.cwd = ps.cwd or snap.get("workingDirectory") or None
                ps.git_branch = ps.git_branch or snap.get("gitBranch") or None
                ps.os_version = snap.get("osVersion") or None
                ps.shell = snap.get("shell") or None
            elif atype == "model" and att.get("model"):
                ps.model = ps.model or str(att["model"])
            return ordn
        text = str(att.get("content") or att.get("text") or json.dumps(att, ensure_ascii=False))[:TOOL_TEXT_CAP]
        ps.messages.append(Msg(ordn, "attachment", text, ts=ts, tool=atype,
                               label=atype, sidechain=side, uuid=uuid, parent_uuid=puuid))
        return ordn + 1

    return ordn


def discover(root: str) -> list[tuple[str, str]]:
    """(path, kind) for every transcript under a Claude projects dir."""
    out: list[tuple[str, str]] = []
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"memory", "node_modules", ".git"}]
        base = os.path.basename(dirpath)
        for nm in names:
            if not nm.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, nm)
            out.append((fp, "subagent" if base == "subagents" else "main"))
    return out
