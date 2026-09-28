# Vellum — design notes

*Windows desktop app to read, search and export your Claude Code conversation
archive. The README covers what it does and why it is built this way; this is
the shorter statement of intent.*

## Subject & point of view

The product is about a personal record: hundreds of conversations, each with a
shape — long stretches of talking, bursts of tool use, a wall of output, a
compact boundary. Nothing about that is SaaS. So the app is built as an
**archive reader**: a ledger on the left, a document in the middle, marginal
data beneath the title. The opening screen is the ledger itself — no hero, no
marketing.

The memorable element is the **conversation ribbon**: every session row draws
its exchange density as a time-coded micro-strip split by voice. The shape of
a conversation is legible before you open it. One idea, used once — adding a
minimap that reuses it was cut, because it made the reader narrower than the
document it was supposed to frame.

## The three voices

The unresolved question in the three previous attempts was speaker colour:
each version picked a different mapping, and each was internally consistent.
This one settles it on a principle — **the default voice is the one that
should be quiet.**

| Voice | Colour | Why |
|---|---|---|
| **You** | terracotta | your prompts; the thing you are looking for |
| **Claude** | achromatic ink | the default voice; prose should not compete with itself |
| **Machine** | quiet slate | 75% of the archive; present, but never loud |

Coral stays reserved for interactive state and never paints a role. That
separation — accent for "you can act on this", hue for "who is speaking" — is
the rule the earlier token file documented and then broke by making the accent
do double duty.

## Color

Base: Claude's own palette — the brand coral `#D97757` pinned into a warm
cream ground, layered warm charcoal in dark. Ink: paper-white. Both themes
are designed, not derived from each other.

There is **no purple anywhere**. An earlier draft used violet for the machine
voice and it read as a different product: a cool accent against a warm ground
is the one combination that makes a UI look like two designers met. The
voices are separated by warmth and weight instead of by hue.

Every value is **generated** in OKLCH from a hue angle, a lightness ladder and
a chroma cap, gamut-mapped by bisection (`palette.py`). No hex was picked by
hand, so a hue change moves the whole system coherently.

Every text/surface pair the UI renders is audited against WCAG 2 and APCA,
and the audit **exits non-zero on failure** so it can gate a build. Every
pair, in both themes. The APCA implementation is checked against published
reference values — a contrast implementation that is not itself tested
validates nothing.

## Type

- **UI** Segoe UI Variable / Segoe UI at 13.5px.
- **Reading** Georgia at 15.5px / 1.64, measure capped at ~68ch. The record is
  a document, so it reads like one, and a line length cap is the single most
  common reader-app failure when it is missing.
- **Data** Cascadia Mono / Consolas at 11–11.5px, tabular numerals, on
  timestamps, token counts, sizes and ids. Monospace here encodes machine
  data; it is not a stylistic label.

## Layout

```
┌────────┬──────────────────┬──────────────────────────────┐
│ rail   │ search + sort    │ title · facts · tools        │
│        │ ──────────────── │ 1 2 3 4 5 …  (turn outline)  │
│Convers.│ ▮ribbon row      │                              │
│Search  │ ▮ribbon row      │   You      ▏ prose          │
│Pinned  │ ▮ribbon row      │   Claude   ▏ prose          │
│Archive │ ▮ribbon row      │   Machine  ▏ [Bash ▾ card]  │
│        │  …virtual…       │                              │
│Projects│                  │                              │
├────────┴──────────────────┴──────────────────────────────┤
│ status: conversations · rewinds collapsed · turns      index 1.4 GB│
└───────────────────────────────────────────────────────────────┘
```

4px spacing scale. Radii stay small and functional. Min window 980×620.

The reader is **the widest column** — in the previous version it was the
narrowest, which is why the reading surface wrapped at 180px.

## Interaction

Keyboard-first: `/` search · `j`/`k` or arrows move · `Enter` open · `F5`
re-scan · `T` theme · `E` export · `P` pin · `Esc` clear. Every action is also
reachable by mouse; every control shows `:focus-visible`.

Two rules the previous versions broke, both now enforced:

- **A click always produces visible feedback within one frame.** Loading
  states are set before awaiting, not after.
- **A stale response is discarded, not painted.** Every async action is keyed
  by request id, so a slow reply cannot overwrite a newer one.

## States

A cold index runs in the background with real progress and the list stays
usable. Empty states name the situation and the action. Errors name the fact
and the fix. No emoji narration.

## Efficiency

1. SQLite (WAL) + FTS5 word index, incremental by `(mtime, size)` — a rescan
   of an unchanged archive is a metadata pass (measured: 0.04s).
2. Never hold a 10 MB transcript in the UI: the reader pages 250 messages at a
   time and `content-visibility` skips layout for the rest.
3. Rewinds are collapsed at index time, so search returns each conversation
   once rather than up to 56 times.
4. Search is debounced and keyed, so a fast typist does not queue 40 queries.

## Non-negotiables

Read-only against `~/.claude/projects`. Export writes only where the user
chose, and is renamed into place so a failure cannot leave a partial file.
The server binds to loopback. Both themes pass the contrast audit. The test
suite must pass, and each test pins a bug that was actually found on real
data.
