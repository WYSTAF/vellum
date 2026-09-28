"""Writing a conversation out.

Five formats, one rule each:

*   **Markdown** is the primary output, because a transcript *is* a document.
    The one thing that breaks it is a fenced code block inside a fenced code
    block, so :func:`_fence` always emits a longer fence than any run of
    backticks in the content.  That is the bug in the older version, and it
    corrupts exactly the transcripts worth exporting.
*   **HTML** is escaped and rendered server-side; nothing from a transcript
    is ever emitted as live markup.
*   **JSON** is lossless -- every field the index has.
*   **Text** is for pasting.
*   **CSV** is one row per message, for spreadsheets.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from datetime import datetime

FORMATS = ("md", "html", "json", "txt", "csv")

_ROLE_LABEL = {
    "user": "You", "assistant": "Claude", "thinking": "Thinking",
    "tool": "Tool", "result": "Result", "system": "System",
    "attachment": "Attachment", "meta": "Meta",
}

# Windows reserves these regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(title: str, ext: str, session_id: str = "") -> str:
    """A filename that is legal on Windows and does not silently collide.

    Two conversations with the same title exported on the same day produced
    the same path, and the second silently overwrote the first. The session
    id is folded in as a short hash so distinct conversations stay distinct
    while the name still reads like the conversation.
    """
    base = _ILLEGAL.sub("", title or "").strip().rstrip(". ")
    base = " ".join(base.split())[:80]
    if not base:
        base = "conversation"
    # Windows reserves these names *with any extension*, so "CON (2026-01-01)"
    # is unopenable -- the check has to be on the stem, not the whole name.
    if base.split(".")[0].upper() in _RESERVED:
        base = f"conversation {base}"
    stem = f"{base} ({datetime.now():%Y-%m-%d})"
    if session_id:
        digest = hashlib.blake2b(
            session_id.encode("utf-8"), digest_size=3
        ).hexdigest()
        stem = f"{stem} {digest}"
    return f"{stem}.{ext}"


def _fence(text: str) -> str:
    """A backtick fence longer than any run inside the content.

    `````` (5) is needed for content containing ```` (3); computing it rather
    than assuming 3 is what keeps a nested block from ending the fence early
    and dumping raw markup into the rest of the document.
    """
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    return "`" * max(3, longest + 1)


def _when(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _body(m) -> str:
    """The text of a message, with tool payloads clearly fenced."""
    text = m["text"] or ""
    if m["role"] in ("tool", "result") and m.get("label"):
        head = f"{m['label']}"
        if text:
            return f"{head}\n\n{text}"
        return head
    return text


def _md_message(m) -> list[str]:
    out: list[str] = []
    role = m["role"]
    who = _ROLE_LABEL.get(role, role)
    stamp = _when(m["ts"])
    if role == "tool":
        head = f"**{m.get('tool') or 'tool'}**"
    elif role == "result":
        head = "**result**" + (f" · {m['tool']}" if m.get("tool") else "")
    else:
        head = f"**{who}**"
    if stamp:
        head += f" · {stamp}"
    out += [f"### {head}", ""]
    body = _body(m)
    if body.strip():
        # Tool output and thinking are fenced, and the fence is computed from
        # the content so a transcript containing its own code block cannot end
        # the fence early and spill raw markup into the rest of the document.
        if role in ("tool", "result", "thinking"):
            fence = _fence(body)
            out += [f"{fence}text", body, fence, ""]
        else:
            # Prose is indented instead: a stray ``` in what someone typed
            # cannot break an indented block at all.
            out += ["    " + ln if ln.strip() else "" for ln in body.splitlines()]
            out.append("")
    if m.get("sidechain"):
        out += ["_sidechain_", ""]
    return out


def to_markdown(session, messages) -> str:
    lines = [
        f"# {session['title']}",
        "",
        f"_{session.get('project') or ''}_ · {_when(session.get('first_ts'))}"
        f" – {_when(session.get('last_ts'))}",
        "",
    ]
    for k, label in (("cwd", "Directory"), ("git_branch", "Branch"),
                     ("model", "Model"), ("version", "Claude Code")):
        if session.get(k):
            lines.append(f"- **{label}:** {session[k]}")
    lines += ["", f"_{len(messages):,} messages_", "", "---", ""]
    for m in messages:
        lines += _md_message(m)
    return "\n".join(lines)


def to_text(session, messages) -> str:
    lines = [session["title"], "=" * len(session["title"] or ""), ""]
    for m in messages:
        who = _ROLE_LABEL.get(m["role"], m["role"])
        stamp = _when(m["ts"])
        lines.append(f"[{stamp}] {who}" + (f" ({m['tool']})" if m.get("tool") else ""))
        body = _body(m)
        if body.strip():
            lines.append(body)
        lines.append("")
    return "\n".join(lines)


def to_json(session, messages) -> str:
    # 0 is a real token count, so the filter drops only None and "" -- never
    # falsy values.
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if x is not None and x != ""}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    payload = {
        "session": clean(dict(session)),
        "messages": [clean(dict(m)) for m in messages],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def to_csv(session, messages) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["ordinal", "role", "tool", "timestamp", "model", "sidechain",
                "in_tokens", "out_tokens", "text"])
    for m in messages:
        w.writerow([
            m["ordinal"], m["role"], m.get("tool") or "", m.get("ts") or "",
            m.get("model") or "", int(bool(m.get("sidechain"))),
            m.get("in_tok", 0), m.get("out_tok", 0),
            " ".join((m["text"] or "").split()),
        ])
    return buf.getvalue()


_HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#F6F5F2; color:#1B1A17;
    font:16.5px/1.62 Georgia, 'Times New Roman', serif; }}
  main {{ max-width: 74ch; margin: 0 auto; padding: 48px 24px 96px; }}
  h1 {{ font-size: 28px; line-height:1.2; margin:0 0 8px; }}
  .meta {{ color:#4C4A45; font-size:14px; font-family: system-ui, sans-serif;
    border-bottom:1px solid #DBD8D2; padding-bottom:20px; margin-bottom:32px; }}
  h3 {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em;
    font-family: system-ui, sans-serif; color:#4C4A45; margin:32px 0 8px; }}
  p {{ white-space: pre-wrap; margin:0 0 12px; }}
  pre {{ background:#EFEDE9; border:1px solid #E4E1DB; border-radius:6px;
    padding:12px 14px; overflow-x:auto; font:12.5px/1.5 Consolas, monospace;
    white-space:pre-wrap; }}
  code {{ font-family: Consolas, monospace; font-size:.92em; }}
</style></head><body><main>
"""

_HTML_TAIL = "</main></body></html>\n"


def to_html(session, messages) -> str:
    e = html.escape
    out = [_HTML_HEAD.format(title=e(session["title"] or "Conversation"))]
    out.append(f"<h1>{e(session['title'] or 'Conversation')}</h1>")
    bits = [b for b in (session.get("project"), _when(session.get("first_ts"))) if b]
    out.append(f'<div class="meta">{e(" · ".join(bits))} · {len(messages):,} messages</div>')
    for m in messages:
        who = e(_ROLE_LABEL.get(m["role"], m["role"]))
        if m.get("tool"):
            who += f" · {e(m['tool'])}"
        stamp = _when(m.get("ts"))
        out.append(f"<h3>{who}{' · ' + e(stamp) if stamp else ''}</h3>")
        body = _body(m)
        if body.strip():
            # Everything is escaped: a transcript is untrusted input, and a
            # <script> inside a tool result must stay text.
            out.append(f"<pre>{e(body)}</pre>" if m["role"] in ("tool", "result", "thinking")
                       else f"<p>{e(body)}</p>")
    out.append(_HTML_TAIL)
    return "\n".join(out)


_RENDERERS = {
    "md": to_markdown, "markdown": to_markdown,
    "html": to_html,
    "json": to_json,
    "txt": to_text, "text": to_text,
    "csv": to_csv,
}


def render(fmt: str, session, messages) -> str:
    """Render a conversation in one of :data:`FORMATS`.

    Rows arrive as ``sqlite3.Row``, which has no ``.get()``. Normalising here
    means every renderer can be written against plain dicts -- and it is the
    only place that knows about the row type.
    """
    fn = _RENDERERS.get((fmt or "md").lower())
    if fn is None:
        raise ValueError(f"Unsupported format: {fmt}")
    return fn(dict(session), [dict(m) for m in messages])
