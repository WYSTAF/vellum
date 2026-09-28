# Vellum

A local reader for your Claude Code conversation archive. Windows-first, one
Python process, no account, no upload — it reads `~/.claude/projects` and
writes nothing but a local index and your exports.

```
python -m vellum
```

Opens a window (via pywebview if installed), otherwise your browser at
`http://127.0.0.1:8733`. The server binds to loopback only.

---

## What it does

- **List** every conversation with its real title, a density ribbon, and the
  facts that matter (turns, tool calls, tokens).
- **Read** it as a document — serif prose, a hard measure cap, tool calls
  folded into cards you can open.
- **Search** every message you've ever sent, with ranked results and snippets.
- **Export** to Markdown, HTML, JSON, text or CSV.
- **Archive** view: totals, token spend, tool frequency, model breakdown.

Keyboard: `/` search · `j`/`k` or arrows move · `Enter` open · `F5` re-scan ·
`T` theme · `E` export · `P` pin · `?` — there is no `?`; press `/`.

---

## The three things that make it work

### 1. Your archive is replicated ~11×, and the previous tools indexed all of it

Measured on one real archive — counts below are rounded, because the
point is the ratio, not anybody's usage:


| | |
|---|---|
| JSONL files | 378 |
| Bytes | 1.6 GB |
| Records | ~800,000 |
| Records carrying a `uuid` | ~780,000 |
| **Distinct UUIDs** | **~73,000** |
| **Duplication** | **10.8×** |
| UUIDs in more than one session | 57% |
| Most sessions sharing one UUID | 56 |

Claude Code writes a **new session file** every time a conversation is
rewound or branched, copying the messages it inherited with their original
UUIDs under a new `sessionId`. So the archive on disk is roughly eleven times
larger than the conversations in it.

All three previous versions of this tool deduplicated by `(mtime, size)` *per
file*, so they indexed all 11× — and search returned the same conversation up
to 56 times.

Vellum treats a session as a **prefix relationship**, not a copy. Two files
that share a long opening and then diverge are a rewind, and only the longest
version is shown. Measured on the real archive, this collapses **234 sessions
into 178 conversations**.

> The obvious implementation — hash each session's UUID set — does not work,
> and the difference is instructive. Set-hashing finds only *identical*
> conversations. On this archive it collapsed **1 of 392** sessions, because
> two sessions of 5,231 and 5,226 messages that match for 5,224 share no
> fingerprint at all. `store.find_rewinds` exists because of that measurement.

### 2. Three quarters of the archive is machine traffic, so the reader is built for it

Content blocks across the archive:

| Block | Count | Share |
|---|---|---|
| `tool_use` | ~250,000 | 31% |
| `tool_result` | ~190,000 | 24% |
| `text` | ~60,000 | 7% |
| `thinking` | ~38,000 | 5% |
| harness attachments | ~220,000 | 28% |

75% of what is stored is the machine talking. The reader's real job is
making that legible, which is why a tool call is a **card with the command in
it** — not `[tool use: Read]`, and not a page of raw JSON.

Harness attachments are not conversation. `total_tokens_reminder` alone is
186,046 rows; with `task_reminder` they are 73% of every attachment row and
84% of their bytes, and none of it is something a reader wants. They are
dropped. Session metadata (`environment`, `date`, `prompt_snapshot`) is real
but is not part of the conversation, so it is recorded on the session and
shown in the header rather than pushed inline — keeping it inline pushed the
first human message five rows down the page.

### 3. The archive knows things the previous tools threw away

- **Full token accounting.** `cache_creation_input_tokens` is routinely the
  *largest* single component of a session. All three previous versions read
  `cache_read` only, so every total they showed was short by the biggest term.
- **Real titles.** 322 of 324 sessions have a `custom-title` or `ai-title`
  record. Using them — and falling back to the first prompt, never to a raw
  UUID — removed 67 sessions whose "title" was a session id.
- **Tool arguments.** 54,000 Bash calls in this archive are stored with their
  arguments under `__unparsedToolInput`. Unhandled, those tool cards had no
  label at all; empty tool labels went from 29% to under 1.5%.

---

## Design

The palette **is** Claude's: the brand coral `#D97757`, pinned into a warm
cream ground with warm ink. Everything else is generated (`vellum/palette.py`)
from a hue angle, a lightness ladder and a chroma cap in OKLCH, gamut-mapped
by bisection rather than clipped — so a hue change moves the whole system
coherently.

**There is no purple.** An earlier draft marked the machine voice in violet,
and it read as a different product: a cool accent on a warm ground is the one
combination that makes a UI look like two designers met. The three voices are
now separated by warmth and weight instead of by hue:

| Voice | Role | Colour |
|---|---|---|
| **You** | your prompts | terracotta |
| **Claude** | the assistant's prose | achromatic ink |
| **Machine** | tool calls, results, attachments | quiet slate |

Claude is deliberately achromatic: it is the default voice, and the moments
that need to pop are the machine's, not the prose's. Coral is reserved for
interactive state and never paints a role.

**Every text/surface pair is audited against WCAG 2 and APCA, and the audit
exits non-zero on failure.** 36 pairs, both themes, all pass. The APCA
implementation is validated against published reference values to within
2.7 Lc — a wrong APCA implementation validates itself against nothing, which
is a lesson the first version of this file learned the hard way.

Regenerate with `python -m vellum.palette`.

---

## Performance, measured

| | Before | After |
|---|---|---|
| First index, 790 MB project | 507 s | 142 s |
| FTS tokenizer | trigram (19.0 s build) | unicode61 (1.7 s) |
| Index size | 1.9 GB | 1.5 GB |
| No-op rescan | 51 s | 0.04 s |

Three decisions, each measured rather than assumed:

- **Word tokenizer, not trigram.** Trigram is genuinely better at finding
  fragments of UUIDs and paths — it found exactly one more hit in a
  representative set of eight. It also builds **11× slower** and makes a
  two-character query match essentially the whole corpus. That trade is not
  worth it for a first-launch index.
- **Only the first 600 characters of a tool payload are indexed.** The full
  text is stored verbatim for the reader; indexing all of it produced an
  807 MB FTS index versus 386 MB for a prefix, and a result snippet only ever
  shows about 14 tokens.
- **Batched commits with WAL checkpoints.** Without them the WAL grew to the
  size of the corpus and the final close paid for all of it at once — a 51 s
  stall on a rescan that had nothing to do.

The reader pages in **250-message chunks**. A 3,000-message conversation is
3.2 MB of DOM, which is seconds of layout; chunked, the first paint is
immediate and `content-visibility` keeps the rest cheap.

---

## Layout

```
vellum/
  palette.py     OKLCH ramps + WCAG/APCA audit   (run: python -m vellum.palette)
  parser.py      JSONL -> normalized messages
  store.py       SQLite schema, dedup, search
  indexer.py     incremental scanning, bounded memory
  export.py      md / html / json / txt / csv
  paths.py       where Claude Code keeps things
  settings.py    preferences
  app.py         HTTP layer
  launcher.py    window, or browser fallback
  ui/            index.html · app.js · css/
tests/           31 tests, each pinning a bug found on real data
```

Run the tests with `python -m pytest tests/ -q`.

---

## Privacy

The server binds to `127.0.0.1` and nothing else. The index lives in
`~/.vellum`, is not encrypted, and is never transmitted. `~/.claude/projects`
is opened read-only; Vellum never writes to it. Exports go only where you
choose, and are written to a temp file and renamed, so a failure or a
collision cannot leave a half-written export behind.
