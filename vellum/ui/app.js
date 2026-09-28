/* Vellum front end.
 *
 * No framework, no build step: the whole UI is a few hundred lines against
 * the DOM, which keeps the shipped app a single Python process plus static
 * files and makes every state transition traceable in one place.
 *
 * The rule the previous versions broke: a click must always produce visible
 * feedback within one frame. Every async action here sets a loading state
 * before it awaits, and a stale response is discarded by request id rather
 * than allowed to paint over a newer one.
 */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const el = {
  q: $('#q'), qClear: $('#q-clear'), list: $('#list'), listFoot: $('#list-foot'),
  more: $('#btn-more'), result: $('#result-line'), sort: $('#sort'),
  projs: $('#projs'), reader: $('#reader'), empty: $('#reader-empty'),
  doc: $('#doc'), title: $('#doc-title'), facts: $('#doc-facts'),
  body: $('#body'), foot: $('#reader-foot'), outline: $('#outline'),
  tTools: $('#t-tools'), tThinking: $('#t-thinking'),
  fmt: $('#export-fmt'), btnExport: $('#btn-export'), btnCopy: $('#btn-copy'),
  btnPin: $('#btn-pin'), status: $('#status-text'), statusDot: $('#status-dot'),
  statusRight: $('#status-right'), cTotal: $('#c-total'), cPinned: $('#c-pinned'),
  toast: $('#toast'),
};

const state = {
  view: 'library', project: null, q: '', sort: 'recent',
  sessions: [], total: 0, shown: 0, page: 200,
  open: null,           // session id
  pinned: false,        // is the open conversation pinned
  pinnedCount: 0,
  hits: [],
  includeTools: true, includeThinking: false,
  theme: localStorage.getItem('vellum.theme') || 'dark',
  msgs: [], msgTotal: 0, chunk: 0,
  filterNote: '', indexSize: 0,
};

/* ── api ──────────────────────────────────────────────────────────── */
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

/* ── helpers ──────────────────────────────────────────────────────── */
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const nfmt = n => (n ?? 0).toLocaleString();

function bytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function when(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return '';
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 6) return `${Math.floor(diff / 86400)}d ago`;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
  });
}

function clock(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return isNaN(d) ? '' : d.toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

let toastTimer;
function toast(msg) {
  el.toast.textContent = msg;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2800);
}

function setStatus(text, kind) {
  el.status.textContent = text;
  el.statusDot.className = 'status__dot' + (kind ? ' is-' + kind : '');
}

/* ── theme ────────────────────────────────────────────────────────── */
function applyTheme(t) {
  state.theme = t;
  document.documentElement.dataset.theme = t;
  localStorage.setItem('vellum.theme', t);
}

/* ── rail ─────────────────────────────────────────────────────────── */
function setView(v) {
  state.view = v;
  $$('.rail__item').forEach(b => {
    const on = b.dataset.view === v;
    b.classList.toggle('is-on', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (v === 'stats') { renderStats(); return; }
  if (v === 'pinned') { loadPinned(); return; }
  loadList();
}

async function loadProjects() {
  try {
    const { projects } = await api('/api/projects');
    el.projs.innerHTML =
      `<li><button class="proj is-on" data-proj=""><span class="proj__name">All projects</span>
         <span class="proj__n">${nfmt(projects.reduce((a, p) => a + p.n, 0))}</span></button></li>` +
      projects.map(p => `<li><button class="proj" data-proj="${esc(p.project)}">
          <span class="proj__name" title="${esc(p.project)}">${esc(p.project)}</span>
          <span class="proj__n">${nfmt(p.n)}</span></button></li>`).join('');
  } catch (e) { /* the list still works without the rail */ }
}

el.projs.addEventListener('click', e => {
  const b = e.target.closest('.proj');
  if (!b) return;
  state.project = b.dataset.proj || null;
  $$('.proj', el.projs).forEach(x => x.classList.toggle('is-on', x === b));
  loadList();
});

/* ── list ─────────────────────────────────────────────────────────── */
/* The ribbon is one strip per conversation showing exchange density over
   time, split by voice. The indexer writes [{y, c, m}] — named fields, not a
   positional tuple, so adding a voice later cannot silently shift them. */
function ribbon(r) {
  if (!r || !r.length) return '';
  const total = (b) => (b.y || 0) + (b.c || 0) + (b.m || 0);
  const max = Math.max(1, ...r.map(total));
  let out = '<div class="ribbon" aria-hidden="true">';
  for (const b of r) {
    for (const [k, cls] of [['y', 'r-you'], ['c', 'r-cl'], ['m', 'r-ma']]) {
      const v = b[k] || 0;
      if (v) out += `<i class="${cls}" style="flex:${v / max}"></i>`;
    }
  }
  return out + '</div>';
}

function rowHtml(s) {
  let r = [];
  try { r = JSON.parse(s.ribbon || '[]'); } catch { r = []; }
  const facts = [
    `${nfmt(s.n_turns)} turns`,
    s.n_tool ? `${nfmt(s.n_tool)} tools` : null,
    s.in_tok ? `${nfmt(s.in_tok + s.out_tok)} tokens` : null,
  ].filter(Boolean).join(' · ');
  // The pinned class has to be on the row from the start: the pin toggle used
  // to read it to decide whether to pin or unpin, and always got false --
  // so pinning an already-pinned conversation took two clicks to undo.
  return `<button class="row${s.pinned ? ' is-pinned' : ''}" data-id="${esc(s.id)}" role="option" aria-selected="false">
    <div class="row__top">
      <span class="row__title" title="${esc(s.title)}">${esc(s.title)}</span>
      <span class="row__when">${esc(when(s.last_ts))}</span>
    </div>
    <div class="row__sub" title="${esc(s.cwd || s.project || '')}">${esc(s.project || '')}${s.git_branch ? ' · ' + esc(s.git_branch) : ''}</div>
    <div class="row__facts">${esc(facts)}</div>
    ${ribbon(r)}
  </button>`;
}

let listId = 0;
async function loadList(append = false) {
  const id = ++listId;
  if (!append) { state.shown = 0; el.list.innerHTML = ''; }
  el.result.textContent = 'Loading…';
  try {
    const params = new URLSearchParams({ limit: state.page, offset: state.shown });
    if (state.project) params.set('project', state.project);
    if (state.sort !== 'recent') params.set('sort', state.sort);
    const data = await api('/api/sessions?' + params);
    // Two project clicks in quick succession put two requests in flight; the
    // slower one resolving second used to append the wrong project's rows and
    // leave state.shown inconsistent with the offset that was sent.
    if (id !== listId) return;
    state.total = data.total;
    state.sessions = append ? state.sessions.concat(data.sessions) : data.sessions;
    state.shown = state.sessions.length;
    el.list.insertAdjacentHTML('beforeend', data.sessions.map(rowHtml).join(''));
    el.result.textContent = state.total
      ? `${nfmt(state.sessions.length)} of ${nfmt(state.total)} conversations`
      : 'No conversations';
    el.listFoot.hidden = state.sessions.length >= state.total;
    el.cTotal.textContent = nfmt(state.total);
    markActiveRow();
  } catch (e) {
    if (id !== listId) return;
    // Distinguish "no index yet" from "the list failed to render" -- a render
    // bug that shows a friendly empty state is very hard to notice.
    const noIndex = /has not been built/i.test(e.message || '');
    el.list.innerHTML = noIndex
      ? `<div class="empty"><strong>Nothing indexed yet</strong>
         Press <kbd>F5</kbd> or “Scan” to read your Claude Code archive.</div>`
      : `<div class="empty"><strong>Could not load conversations</strong>${esc(e.message)}</div>`;
    el.result.textContent = '—';
    console.error('loadList failed', e);
  }
}

el.more.addEventListener('click', () => loadList(true));

el.list.addEventListener('click', e => {
  const row = e.target.closest('.row');
  if (row) openSession(row.dataset.id);
});

function markActiveRow() {
  $$('.row', el.list).forEach(r => {
    const on = r.dataset.id === state.open;
    r.classList.toggle('is-on', on);
    r.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

/* ── search ───────────────────────────────────────────────────────── */
let searchTimer, searchId = 0;
function onQuery() {
  const v = el.q.value.trim();
  el.qClear.hidden = !v;
  state.q = v;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => (v ? doSearch(v) : setView(state.view === 'search' ? 'library' : state.view)), 140);
}

async function doSearch(v) {
  const id = ++searchId;
  el.result.textContent = 'Searching…';
  const params = new URLSearchParams({ q: v, limit: 120 });
  if (state.project) params.set('project', state.project);
  try {
    const data = await api('/api/search?' + params);
    if (id !== searchId) return;           // a newer keystroke won
    state.hits = data.hits;
    renderHits(data);
  } catch (e) {
    if (id === searchId) el.result.textContent = 'Search failed';
  }
}

function renderHits(data) {
  el.list.innerHTML = '';
  if (!data.hits.length) {
    el.list.innerHTML = `<div class="empty"><strong>No matches</strong>
      Nothing in ${nfmt(state.total)} conversations matches “${esc(state.q)}”.</div>`;
    el.result.textContent = '0 matches';
    return;
  }
  el.list.innerHTML = data.hits.map((h, i) => `<button class="hit" data-i="${i}" role="option">
      <div class="hit__top">
        <span class="hit__title" title="${esc(h.title)}">${esc(h.title)}</span>
        <span class="hit__n">${h.hits} ${h.hits === 1 ? 'hit' : 'hits'}</span>
      </div>
      <div class="hit__snip">${hl(h.snippet)}</div>
    </button>`).join('');
  el.result.textContent = `${nfmt(data.count)} conversations · ${data.ms} ms`;
  el.listFoot.hidden = true;
}

/* The server marks snippet hits with \x02 / \x03 (SQLite char(2)/char(3)),
   not with guillemets -- a transcript contains web pages, and a stray '<' in
   the indexed text would otherwise open a <mark> that never closes. The
   replacement is done after escaping, so nothing else can inject markup. */
function hl(s) {
  return esc(s || '')
    .replace(/\u0002/g, '<mark>')
    .replace(/\u0003/g, '</mark>');
}

el.list.addEventListener('click', e => {
  const h = e.target.closest('.hit');
  if (!h) return;
  const hit = state.hits[+h.dataset.i];
  if (hit) openSession(hit.session_id, hit.ordinal);
});

/* ── reader ───────────────────────────────────────────────────────── */
const ROLE = {
  user: 'You', assistant: 'Claude', thinking: 'Thinking',
  tool: 'Tool', result: 'Result', system: 'System', attachment: 'Attachment', meta: 'Meta',
};

function msgHtml(m, i) {
  const role = m.role || 'meta';
  const who = ROLE[role] || role;
  const bits = [clock(m.ts)];
  if (m.model) bits.push(m.model);
  if (m.in_tok || m.out_tok) bits.push(`${nfmt(m.in_tok)}→${nfmt(m.out_tok)}`);
  const meta = bits.filter(Boolean).map(esc).join(' · ');

  if (role === 'tool' || role === 'result') {
    const isErr = m.tool === 'error';
    // A result inherits the name of the call that produced it, so a collapsed
    // card still says what it was. Errors are marked, not just coloured --
    // colour alone is not a signal.
    const name = isErr ? 'error' : (m.tool || (role === 'result' ? 'result' : 'tool'));
    const label = m.label || (m.text || '').split('\n')[0] || '';
    return `<div class="msg msg--${role}${isErr ? ' msg--error' : ''}" data-i="${i}" id="m${i}">
      <div class="msg__rail"></div>
      <div>
        <button class="tool__head" aria-expanded="false">
          <span class="tool__name">${esc(name)}</span>
          <span class="tool__label">${esc(label)}</span>
          <span class="tool__caret">▶</span>
        </button>
        <div class="tool__body" hidden>${esc(m.text || '')}</div>
      </div>
    </div>`;
  }
  return `<div class="msg msg--${esc(role)}" data-i="${i}" id="m${i}">
    <div class="msg__rail"></div>
    <div>
      <div class="msg__head"><span class="msg__who">${esc(who)}</span>
        <span class="msg__meta">${meta}</span></div>
      <div class="msg__text">${mdish(m.text || '')}</div>
    </div>
  </div>`;
}

/* A deliberately small Markdown subset: fenced code, inline code, bold,
   italic, links. Everything is escaped *first*, so no transcript content can
   ever become live markup — a tool result containing <script> stays text. */
function mdish(s) {
  // A NUL in the source would collide with the stash sentinel below and be
  // swapped for whatever HTML happened to be at that index. Transcripts are
  // full of binary-ish junk, so strip control characters that are never
  // meaningful in prose before anything else looks at the string.
  let out = esc(String(s ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, ''));
  const stash = [];
  const keep = html => { stash.push(html); return `\u0000${stash.length - 1}\u0000`; };
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    keep(`<pre><code data-lang="${esc(lang)}">${code.replace(/\n$/, '')}</code></pre>`));
  out = out.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`));
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  out = out.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_, t, u) => `<a href="${u}" rel="noopener noreferrer" target="_blank">${t}</a>`);
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => stash[+i] ?? '');
}

async function openSession(id, jumpTo) {
  state.open = id;
  markActiveRow();
  $$('.hit', el.list).forEach((h, i) => h.classList.toggle('is-on', state.hits[i]?.session_id === id));
  el.doc.hidden = false; el.empty.hidden = true;

  // Visible feedback on the first frame, before the fetch resolves.
  el.title.textContent = 'Loading…';
  el.facts.textContent = '';
  el.body.innerHTML = `<div class="empty">Reading conversation…</div>`;
  el.foot.innerHTML = '';
  el.outline.innerHTML = '';

  try {
    const d = await api('/api/session/' + encodeURIComponent(id));
    if (state.open !== id) return;              // superseded
    const s = d.session;
    state.msgs = d.messages || [];
    state.msgTotal = state.msgs.length;
    state.chunk = 0;
    state.pinned = !!s.pinned;
    el.btnPin.textContent = state.pinned ? '★' : '☆';
    el.btnPin.title = state.pinned ? 'Unpin (P)' : 'Pin (P)';
    el.title.textContent = s.title;
    el.facts.innerHTML = [
      s.project && `<span><b>${esc(s.project)}</b></span>`,
      s.git_branch && `<span>branch <b>${esc(s.git_branch)}</b></span>`,
      s.cwd && `<span title="${esc(s.cwd)}">${esc(shortPath(s.cwd))}</span>`,
      `<span><b>${nfmt(state.msgTotal)}</b> messages</span>`,
      s.in_tok ? `<span>${nfmt(s.in_tok)} in</span>` : '',
      s.out_tok ? `<span>${nfmt(s.out_tok)} out</span>` : '',
      `<span>${bytes(s.bytes)}</span>`,
      d.forks && d.forks.length ? `<span>rewound ×${d.forks.length}</span>` : '',
    ].filter(Boolean).join('');
    renderChunk();
    renderOutline(state.msgs);
    el.reader.scrollTop = 0;
    if (jumpTo) {
      // A hit may sit past the first chunk; reveal chunks until it exists.
      revealToOrdinal(jumpTo);
      const target = document.getElementById('m' + jumpTo);
      if (target) { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); flash(target); }
    }
    setStatus(`${s.title} · ${clock(s.last_ts)}`, 'ok');
    api('/api/read', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: id, read: true }),
    }).catch(() => {});
  } catch (e) {
    // A failure for a session you have already navigated away from must not
    // paint over the one you are now reading.
    if (state.open !== id) return;
    el.body.innerHTML = `<div class="empty"><strong>Could not open this conversation</strong>${esc(e.message)}</div>`;
    setStatus(e.message, 'err');
  }
}

/* A 2,921-message conversation is 3.2 MB of DOM, which is several seconds
   of layout on first paint. The reader renders one chunk at a time and
   appends on demand; `content-visibility` on each message keeps the rest
   cheap once they are in the tree. */
const CHUNK = 250;

function visibleMessages() {
  return state.msgs.filter(showMessage);
}

function showMessage(m) {
  if (!state.includeTools && (m.role === 'tool' || m.role === 'result')) return false;
  if (!state.includeThinking && m.role === 'thinking') return false;
  return true;
}

/* The filtered list is the unit of chunking. Chunking the unfiltered list and
   filtering afterwards -- which is what this did first -- meant the Tools and
   Thinking toggles re-rendered but changed nothing: 179 tool cards before the
   toggle and 179 after it. */
function renderChunk(append = false) {
  const slice = visibleMessages().slice(state.chunk * CHUNK, (state.chunk + 1) * CHUNK);
  const html = slice.map(m => msgHtml(m, m.ordinal)).join('');
  if (append) el.body.insertAdjacentHTML('beforeend', html);
  else el.body.innerHTML = html;
  renderMore();
}

function renderMore() {
  const total = visibleMessages().length;
  const shown = Math.min((state.chunk + 1) * CHUNK, total);
  if (shown >= total) {
    el.foot.innerHTML = `<div class="empty" style="padding:18px">
      End of conversation · ${nfmt(state.msgTotal)} messages</div>`;
    return;
  }
  el.foot.innerHTML = `<div style="text-align:center">
    <button class="btn btn--wide" id="btn-more-msgs">
      Show more · ${nfmt(total - shown)} remaining</button></div>`;
}

el.foot.addEventListener('click', e => {
  if (!e.target.closest('#btn-more-msgs')) return;
  const keep = el.reader.scrollTop;
  state.chunk++;
  renderChunk(true);
  el.reader.scrollTop = keep;   // appending must not move what you were reading
});

function revealToOrdinal(ordinal) {
  // Chunk over the *filtered* list, so a search hit in a tool-heavy turn is
  // reached by the same arithmetic that rendered it.
  const filtered = visibleMessages();
  const idx = filtered.findIndex(m => m.ordinal === ordinal);
  if (idx < 0) return;
  const need = Math.floor(idx / CHUNK);
  while (state.chunk < need) {
    state.chunk++;
    renderChunk(true);
  }
}

function shortPath(p) {
  const parts = String(p).split(/[\\/]/);
  return parts.length > 3 ? '…/' + parts.slice(-2).join('/') : p;
}

/* The turn outline. It used to stop at 60 turns with no indication that more
   existed, so turn 61 of a long conversation simply had no button and no hint
   that one could. It is a *sampling* control, not a complete index: showing
   every turn of a 500-turn conversation would be 500 buttons. So above the
   threshold it samples evenly and says so. */
const OUTLINE_MAX = 40;

function renderOutline(msgs) {
  const turns = msgs
    .map((m, ordinal) => ({ ordinal, role: m.role }))
    .filter(m => m.role === 'user');
  if (turns.length < 2) { el.outline.innerHTML = ''; return; }

  const sampled = turns.length <= OUTLINE_MAX
    ? turns
    : Array.from({ length: OUTLINE_MAX }, (_, k) =>
        turns[Math.round(k * (turns.length - 1) / (OUTLINE_MAX - 1))]);
  const note = turns.length > OUTLINE_MAX
    ? `<span class="outline__note" title="${nfmt(turns.length)} turns in this conversation">
         ${nfmt(turns.length)} turns · every ${Math.round(turns.length / OUTLINE_MAX)}th shown</span>`
    : `<span class="outline__note">${nfmt(turns.length)} turns</span>`;

  el.outline.innerHTML = note + sampled
    .map((j, k) => `<button data-jump="${j.ordinal}" title="Turn ${k + 1}">${k + 1}</button>`)
    .join('');
}

el.outline.addEventListener('click', e => {
  const b = e.target.closest('button[data-jump]');
  if (!b) return;
  $$('button', el.outline).forEach(x => x.classList.toggle('is-on', x === b));
  const t = document.getElementById('m' + b.dataset.jump);
  if (t) { t.scrollIntoView({ block: 'center', behavior: 'smooth' }); flash(t); }
});

function flash(node) {
  node.style.transition = 'background .5s ease';
  node.style.background = 'var(--accent-soft)';
  setTimeout(() => { node.style.background = ''; }, 600);
}

el.body.addEventListener('click', e => {
  const h = e.target.closest('.tool__head');
  if (!h) return;
  const open = h.parentElement.querySelector('.tool__body');
  const isOpen = !open.hidden;
  open.hidden = isOpen;
  h.setAttribute('aria-expanded', String(!isOpen));
  // The wrapper is .msg--tool/.msg--result, not .tool.
  h.closest('.msg')?.classList.toggle('is-open', !isOpen);
});

/* The toggles are the reader's main control, and they re-render the current
   chunk. Scroll position is restored because rebuilding a chunk otherwise
   throws you back to the top of a 250-message document. */
function reRenderKeepingScroll() {
  if (!state.open) return;
  const keep = el.reader.scrollTop;
  state.chunk = 0;
  renderChunk();
  el.reader.scrollTop = keep;
  // Count what is actually on screen, not what passes the filter across the
  // whole session -- otherwise this claims 750 of 1000 messages while 250
  // are rendered and the rest sit behind "Show more".
  const rendered = $$('.msg', el.body).length;
  const passing = visibleMessages().length;
  state.filterNote = passing === state.msgTotal
    ? ''
    : `showing ${nfmt(passing)} of ${nfmt(state.msgTotal)} messages (${nfmt(rendered)} loaded)`;
  paintStatusRight();
}

/* The status bar's right-hand side is shared between the index size and the
   reader's filter note. One function owns it so the 1.2s poll cannot erase a
   message that was just set. */
function paintStatusRight() {
  el.statusRight.textContent = state.filterNote ||
    (state.indexSize ? `index ${bytes(state.indexSize)}` : '');
}

el.tTools.addEventListener('change', () => {
  state.includeTools = el.tTools.checked;
  localStorage.setItem('vellum.tools', String(state.includeTools));
  reRenderKeepingScroll();
});

el.tThinking.addEventListener('change', () => {
  state.includeThinking = el.tThinking.checked;
  localStorage.setItem('vellum.thinking', String(state.includeThinking));
  reRenderKeepingScroll();
});

/* ── actions ──────────────────────────────────────────────────────── */
el.btnExport.addEventListener('click', async () => {
  if (!state.open) return;
  el.btnExport.disabled = true;
  el.btnExport.textContent = 'Writing…';
  try {
    const r = await api('/api/export', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        session_id: state.open, fmt: el.fmt.value,
        include_tools: el.tTools.checked, include_thinking: el.tThinking.checked,
      }),
    });
    toast(`Exported ${r.path.split(/[\\/]/).pop()}`);
  } catch (e) { toast('Export failed: ' + e.message); }
  finally { el.btnExport.disabled = false; el.btnExport.textContent = 'Export'; }
});

el.btnCopy.addEventListener('click', async () => {
  if (!state.open) return;
  // navigator.clipboard is undefined on an insecure origin, and the old code
  // let that surface as a raw TypeError in a toast.
  if (!navigator.clipboard?.writeText) {
    toast('Clipboard unavailable — use Export instead');
    return;
  }
  try {
    // Copy what is on screen, so "copy" means what the user can see -- the
    // tool/thinking toggles apply, and no second round trip is needed.
    const plain = $$('.msg__text, .tool__body', el.body)
      .filter(n => !n.hidden && !n.closest('.tool__body[hidden]'))
      .map(n => n.innerText).join('\n\n');
    await navigator.clipboard.writeText(plain);
    toast('Conversation copied');
  } catch (e) {
    toast('Copy failed: ' + (e.message || 'clipboard blocked'));
  }
});

el.btnPin.addEventListener('click', async () => {
  if (!state.open) return;
  // Toggle from the session's own flag, not from a row's class: the row may
  // not be in the list at all (you can open a conversation from a search
  // hit), and the row never carried the class in the first place.
  const next = !state.pinned;
  el.btnPin.disabled = true;
  try {
    await api('/api/pin', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: state.open, pinned: next }),
    });
    state.pinned = next;
    el.btnPin.textContent = next ? '★' : '☆';
    el.btnPin.title = next ? 'Unpin (P)' : 'Pin (P)';
    const row = $(`.row[data-id="${CSS.escape(state.open)}"]`, el.list);
    row?.classList.toggle('is-pinned', next);
    // Recount rather than increment: the old arithmetic could write -1 and
    // then permanently over-count by one.
    state.pinnedCount = Math.max(0, state.pinnedCount + (next ? 1 : -1));
    el.cPinned.textContent = nfmt(state.pinnedCount);
    toast(next ? 'Pinned' : 'Unpinned');
  } catch (e) {
    toast('Could not pin');
  } finally {
    el.btnPin.disabled = false;
  }
});

async function loadPinned() {
  el.list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const { sessions } = await api('/api/sessions?limit=1000');
    const pins = sessions.filter(s => s.pinned);
    el.cPinned.textContent = nfmt(pins.length);
    el.list.innerHTML = pins.length
      ? pins.map(rowHtml).join('')
      : `<div class="empty"><strong>Nothing pinned</strong>
         Pin a conversation to keep it here — the ☆ button in the reader.</div>`;
    el.result.textContent = pins.length ? `${nfmt(pins.length)} pinned` : '—';
    el.listFoot.hidden = true;
  } catch (e) { el.list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ── archive view ─────────────────────────────────────────────────── */
async function renderStats() {
  el.doc.hidden = true; el.empty.hidden = false;
  el.empty.innerHTML = '<div class="empty">Loading archive…</div>';
  // A search that is still on screen behind the archive view is confusing;
  // the rail says "Archive" and the middle column says otherwise.
  el.q.value = ''; el.qClear.hidden = true; state.q = '';
  el.list.innerHTML = '';
  el.listFoot.hidden = true;
  el.result.textContent = 'Archive';
  try {
    const s = await api('/api/stats');
    const maxModel = (s.models || [])[0]?.[1] || 1;
    el.empty.innerHTML = `<div class="stats" style="width:100%">
      <h2>The archive</h2>
      <p>${nfmt(s.sessions)} conversations${s.forks ? `, with ${nfmt(s.forks)} rewinds collapsed into them` : ''}.</p>
      <div class="tiles">
        ${tile('Conversations', nfmt(s.sessions), `${bytes(s.bytes_unique)} of content`)}
        ${tile('Turns', nfmt(s.turns), `${nfmt(s.tools)} tool calls`)}
        ${tile('Input tokens', nfmt(s.in_tok), `${nfmt(s.cache_create)} cache writes`)}
        ${tile('Output tokens', nfmt(s.out_tok), `${nfmt(s.cache_read)} cache reads`)}
        ${tile('On disk', bytes(s.bytes_on_disk), `${s.forks ? nfmt(s.forks) + ' rewinds' : 'no rewinds'}`)}
        ${tile('Models', nfmt(s.models.length), 'distinct')}
      </div>
      <div class="bars"><h3>Most used tools</h3>
        ${(s.tools || []).slice(0, 12).map(t => {
          const max = s.tools[0]?.n || 1;
          return `<div class="bar"><span class="bar__k" title="${esc(t.tool)}">${esc(t.tool)}</span>
            <span class="bar__t"><i style="width:${(t.n / max * 100).toFixed(1)}%"></i></span>
            <span class="bar__v">${nfmt(t.n)}</span></div>`;
        }).join('')}
      </div>
      <div class="bars"><h3>Models</h3>
        ${(s.models || []).slice(0, 12).map(m => {
          // store.stats returns models as [name, count] tuples, not objects.
          const [name, n] = m;
          return `<div class="bar"><span class="bar__k" title="${esc(name)}">${esc(name)}</span>
            <span class="bar__t"><i style="width:${(n / maxModel * 100).toFixed(1)}%"></i></span>
            <span class="bar__v">${nfmt(n)}</span></div>`;
        }).join('')}
      </div>
    </div>`;
    el.result.textContent = 'Archive';
  } catch (e) { el.empty.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
const tile = (k, v, note) =>
  `<div class="tile"><div class="tile__k">${esc(k)}</div>
   <div class="tile__v">${esc(v)}</div><div class="tile__note">${esc(note || '')}</div></div>`;

/* ── status polling ───────────────────────────────────────────────── */
async function pollStatus() {
  try {
    const s = await api('/api/status');
    if (s.running) { setStatus(s.message, 'busy'); return; }
    if (s.error) { setStatus(s.error, 'err'); return; }
    setStatus(s.indexed ? s.message : 'Press F5 to index your archive', s.indexed ? 'ok' : '');
    state.indexSize = s.index_size || 0;
    paintStatusRight();
    // Load the library once, as soon as there is an index to read -- whether
    // this process built it or a previous run did. Guarded on the view so a
    // search typed during the first index is not wiped by it.
    if (s.indexed && !state._loaded) {
      state._loaded = true;
      await loadProjects();
      if (state.view === 'library' && !state.q) loadList();
    }
  } catch (e) { setStatus('Waiting for the server…', ''); }
}

async function rescan(full = false) {
  try {
    await api('/api/index' + (full ? '?full=true' : ''), { method: 'POST' });
    setStatus('Indexing…', 'busy');
  } catch (e) { setStatus(e.message, 'err'); }
}

/* ── keyboard ─────────────────────────────────────────────────────── */
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === '/' && !typing) { e.preventDefault(); el.q.focus(); el.q.select(); return; }
  if (e.key === 'Escape') {
    if (typing && el.q.value) { el.q.value = ''; onQuery(); }
    else el.q.blur();
    return;
  }
  if (typing) return;
  if (e.key === 'f' || e.key === 'F5') { e.preventDefault(); rescan(e.shiftKey); return; }
  if (e.key === 't') { applyTheme(state.theme === 'dark' ? 'light' : 'dark'); return; }
  if (e.key === 'p' && state.open) { el.btnPin.click(); return; }
  if (e.key === 'e' && state.open) { el.btnExport.click(); return; }
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); move(1); return; }
  if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
  if (e.key === 'Home') { e.preventDefault(); pick(0); return; }
  if (e.key === 'End') { e.preventDefault(); pick(items().length - 1); return; }
});

const items = () => $$('.row, .hit', el.list);
function move(d) {
  const list = items();
  if (!list.length) return;
  let i = list.findIndex(n => n.classList.contains('is-on'));
  i = i < 0 ? 0 : Math.max(0, Math.min(list.length - 1, i + d));
  pick(i);
}
function pick(i) {
  const list = items();
  if (i < 0 || i >= list.length) return;
  const n = list[i];
  list.forEach(x => x.classList.toggle('is-on', x === n));
  n.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  if (n.classList.contains('hit')) {
    const hit = state.hits[+n.dataset.i];
    if (hit) openSession(hit.session_id, hit.ordinal);
  } else openSession(n.dataset.id);
}

/* ── wiring ───────────────────────────────────────────────────────── */
el.q.addEventListener('input', onQuery);
el.qClear.addEventListener('click', () => { el.q.value = ''; onQuery(); el.q.focus(); });
el.sort.addEventListener('change', () => { state.sort = el.sort.value; loadList(); });
$('#btn-theme').addEventListener('click', () =>
  applyTheme(state.theme === 'dark' ? 'light' : 'dark'));
$('#btn-rescan').addEventListener('click', () => rescan());
$$('.rail__item').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));

applyTheme(state.theme);

// Reader toggles are a preference, not per-session state: the previous
// versions reset them on every open, which read as a bug.
state.includeTools = localStorage.getItem('vellum.tools') !== 'false';
state.includeThinking = localStorage.getItem('vellum.thinking') === 'true';
el.tTools.checked = state.includeTools;
el.tThinking.checked = state.includeThinking;

setStatus('Starting…', 'busy');
pollStatus();
setInterval(pollStatus, 1200);
