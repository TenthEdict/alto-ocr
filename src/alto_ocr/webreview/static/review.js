/* Transcript review client.
 *
 * Holds no authority over anything. Every semantic decision -- what an edit
 * means, what counts toward provenance, how a transcript is rendered -- is
 * the server's (remediate.py). This file's whole job is: show the scan with
 * its word boxes, collect what the corrector did, and post it as one batch.
 *
 * Pending corrections are one ordered log: a corrector who fixes the same
 * word twice produces one operation, not two (the newer replaces the older).
 * The server rejects a batch with two operations on one word precisely
 * because a double count would overstate how many words a human changed in
 * the ALTO's provenance -- coalescing here is what keeps that honest. The
 * log is also the undo history, and it is sent in the order it was made,
 * because reading-order moves run where they sit in the batch.
 *
 * The reading order the page shows is never computed here: the pending
 * batch is posted to the server's preview endpoint, which applies it with
 * the same code a save runs and answers with the resulting order.
 */
'use strict';

const view = document.getElementById('view');
const crumbs = document.getElementById('crumbs');
const banner = document.getElementById('banner');
let CONFIG = { readOnly: false, lowWc: 0.5, version: '', symbols: { categories: [], labels: {} } };
let symbolTab = null;  // the keyboard tab last used, kept across pages

/* ---------------------------------------------------------------- utils */

const api = async (path, options) => {
  const res = await fetch(path, options);
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = (body && (body.detail || body.message)) || text || res.statusText;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return body;
};

const postJSON = (path, payload) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});

const enc = encodeURIComponent;
const objUrl = (refId) => `api/objects/${enc(refId)}`;
const pageUrl = (refId, stem) => `${objUrl(refId)}/pages/${enc(stem)}`;

const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) node.append(kid);
  return node;
};

/* A confidence (0..1) as a whole percentage, the way the Tk GUI shows it.
 * Floored, not rounded, so the number agrees with the flag rule: under a
 * 50% threshold a word shown as 49% is flagged and one shown as 50% is not. */
const pct = (n) =>
  (n === null || n === undefined) ? '—' : `${Math.floor(Number(n) * 100 + 1e-9)}%`;

let bannerTimer = null;
function flash(message, kind) {
  clearTimeout(bannerTimer);
  if (!message) { banner.hidden = true; return; }
  banner.textContent = message;
  banner.className = 'banner' + (kind ? ' ' + kind : '');
  banner.hidden = false;
  banner.title = message;
  if (kind === 'ok') bannerTimer = setTimeout(() => { banner.hidden = true; }, 4000);
}

function setCrumbs(parts) {
  crumbs.replaceChildren();
  parts.forEach((p, i) => {
    if (i) crumbs.append(el('span', { text: '›' }));
    crumbs.append(p.href ? el('a', { href: p.href, text: p.label })
                         : el('span', { text: p.label }));
  });
}

const template = (id) => document.getElementById(id).content.cloneNode(true);

/* ------------------------------------------------------------ worklist */

async function renderWorklist() {
  setCrumbs([{ label: 'Objects' }]);
  view.replaceChildren(template('tpl-worklist'));
  const note = document.getElementById('worklist-note');
  const table = document.getElementById('worklist');
  const tbody = table.querySelector('tbody');
  const filter = document.getElementById('filter');
  let rows = [];

  const paint = () => {
    const needle = filter.value.trim().toLowerCase();
    const shown = needle ? rows.filter(r => r.refId.toLowerCase().includes(needle)) : rows;
    tbody.replaceChildren(...shown.map(r => {
      const state = [];
      if (!r.hasImages) state.push(el('span', { class: 'tag warn', text: 'no scans' }));
      else if (r.missingImages) state.push(el('span', { class: 'tag warn', text: `${r.missingImages} unmatched` }));
      if (r.failedPages) state.push(el('span', { class: 'tag failed', text: `${r.failedPages} failed` }));
      if (r.pages && r.pagesToReview === 0) state.push(el('span', { class: 'tag done', text: 'reviewed' }));
      if (r.hasMets) state.push(el('span', { class: 'tag', text: 'METS' }));
      const tr = el('tr', { class: 'clickable', onclick: () => { location.hash = `#/obj/${enc(r.refId)}`; } },
        el('td', {}, el('strong', { text: r.refId })),
        el('td', { class: 'num', text: String(r.pages) }),
        el('td', { class: 'num', text: String(r.pagesToReview) }),
        el('td', { class: 'num', text: pct(r.meanPc) }),
        el('td', { class: 'num', text: String(r.lowWords) }),
        el('td', {}, ...state),
        el('td', {}, el('span', { class: 'muted', text: '›' })),
      );
      return tr;
    }));
    note.textContent = shown.length
      ? `${shown.length} object(s)${needle ? ` matching “${filter.value.trim()}”` : ''}.`
      : (rows.length ? 'Nothing matches that filter.' : 'No objects found under the configured roots.');
    table.hidden = shown.length === 0;
  };

  const load = async (refresh) => {
    note.textContent = refresh ? 'Rescanning…' : 'Loading…';
    try {
      rows = (await api(`api/objects${refresh ? '?refresh=true' : ''}`)).objects;
      paint();
    } catch (e) {
      note.textContent = `Could not list objects: ${e.message}`;
    }
  };

  filter.addEventListener('input', paint);
  document.getElementById('reload').addEventListener('click', () => load(true));
  await load(false);
}

/* -------------------------------------------------------------- object */

function pageSort(pages, worstFirst) {
  const natural = (s) => s.replace(/\d+/g, (d) => d.padStart(12, '0'));
  const rank = { failed: 0, scored: 1, unscored: 2, empty: 3 };
  return [...pages].sort((a, b) => {
    if (worstFirst) {
      // Reviewed pages sink: never hand a corrector a page they finished
      // while unreviewed ones are still waiting.
      if (a.reviewed !== b.reviewed) return a.reviewed ? 1 : -1;
      const ra = rank[a.status] ?? 4, rb = rank[b.status] ?? 4;
      if (ra !== rb) return ra - rb;
      const pa = a.pc === null ? 2 : a.pc, pb = b.pc === null ? 2 : b.pc;
      if (pa !== pb) return pa - pb;
    }
    return natural(a.stem).localeCompare(natural(b.stem));
  });
}

async function renderObject(refId) {
  setCrumbs([{ label: 'Objects', href: '#/' }, { label: refId }]);
  walk = null;   // coming back to the list re-ranks the page walk
  view.replaceChildren(template('tpl-object'));
  document.getElementById('obj-title').textContent = refId;
  const statsLine = document.getElementById('obj-stats');
  const tbody = document.getElementById('pages').querySelector('tbody');
  const worstFirst = document.getElementById('worst-first');
  const repackage = document.getElementById('repackage');
  let data = null;

  const paint = () => {
    tbody.replaceChildren(...pageSort(data.pages, worstFirst.checked).map(p => el('tr', {
      class: 'clickable',
      onclick: () => { location.hash = `#/obj/${enc(refId)}/${enc(p.stem)}`; },
    },
      el('td', {}, el('span', { class: 'mono', text: p.stem }),
         p.hasImage ? null : el('span', { class: 'tag warn', text: ' no scan' })),
      el('td', {}, el('span', { class: `tag ${p.status === 'failed' ? 'failed' : p.status === 'empty' ? 'empty' : ''}`, text: p.status })),
      el('td', { class: 'num', text: pct(p.pc) }),
      el('td', { class: 'num', text: String(p.words) }),
      el('td', { class: 'num', text: String(p.lowWords) }),
      el('td', {}, p.reviewed ? el('span', { class: 'tag done', text: 'yes' }) : el('span', { class: 'muted', text: '—' })),
    )));
  };

  try {
    data = await api(objUrl(refId));
  } catch (e) {
    statsLine.textContent = `Could not load ${refId}: ${e.message}`;
    return;
  }

  const s = data.stats;
  statsLine.textContent =
    `${s.pages} page(s) · ${s.pagesToReview} left to review · mean PC ${pct(s.meanPc)} · `
    + `${s.lowWords} low-confidence word(s) of ${s.words}`
    + (s.missingImages ? ` · ${s.missingImages} page(s) with no matching scan` : '')
    + `\n${data.objectDir}`;
  if (data.goobiUrl) {
    const link = document.getElementById('goobi-link');
    link.href = data.goobiUrl;
    link.hidden = false;
  }
  if (CONFIG.readOnly) repackage.disabled = true;
  repackage.addEventListener('click', async () => {
    repackage.disabled = true;
    flash('Repackaging — checksums over the share can take a moment…');
    try {
      const r = await postJSON(`${objUrl(refId)}/repackage`, {});
      const bits = [`${r.transcriptsUpdated} transcript(s) updated`,
                    r.metsUpdated ? 'METS refreshed' : 'METS unchanged'];
      if (r.externallyEdited.length) bits.push(`${r.externallyEdited.length} externally-edited page(s) stamped`);
      flash(bits.join(', ') + '.', r.problems.length ? 'err' : 'ok');
      if (r.problems.length) flash(r.problems[0], 'err');
    } catch (e) {
      flash(`Repackage failed: ${e.message}`, 'err');
    } finally {
      repackage.disabled = CONFIG.readOnly;
    }
  });

  worstFirst.addEventListener('change', paint);
  paint();
}

/* ---------------------------------------------------------------- page */

/* Per-viewer preferences (threshold, All words, the symbol keyboard). Only
 * conveniences live here; storage can be unavailable, so every access is
 * guarded and a default always stands in. */
const prefs = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem('altoReview.' + key);
      return v === null ? fallback : JSON.parse(v);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('altoReview.' + key, JSON.stringify(value)); } catch { /* private window */ }
  },
};

/* The page walk for ◀ Page / Page ▶: the sidebar's order, frozen for one
 * object until "Worst first" is toggled or the object list is revisited --
 * the same rule as the Tk tab, so saving a page (which moves its PC) never
 * reshuffles the list under the corrector. */
let walk = null;

const WORD_OPS = new Set(['edit', 'verify', 'delete']);
const ZOOM_CHOICES = [['fit', 'Fit page'], ['width', 'Fit width'], [0.25, '25%'], [0.5, '50%'],
                      [0.75, '75%'], [1, '100%'], [1.5, '150%'], [2, '200%'], [3, '300%']];

async function renderPage(refId, stem) {
  setCrumbs([{ label: 'Objects', href: '#/' },
             { label: refId, href: `#/obj/${enc(refId)}` },
             { label: stem }]);
  view.replaceChildren(template('tpl-page'));

  const $ = (id) => document.getElementById(id);
  const scan = $('scan'), overlay = $('overlay'), stage = $('stage'), scroller = $('scroller');
  const status = $('page-status');

  /* ---- state ---- */
  let page = null;               // server payload for the current page
  let objectPages = [];          // sidebar
  // Every correction, in the order it was made: the batch a save posts. A
  // word carries at most one edit/verify/delete (a newer one replaces the
  // older and moves to the end, which keeps the server's one-op-per-word
  // rule); adds and reading-order moves append. Order matters for moves,
  // which run where they sit in the batch.
  const log = [];
  const undoStack = [];          // {op, prev?}: Ctrl+Z takes `op` out, puts `prev` back
  let clearPending = false;
  let selected = null;           // word being edited
  let zoom = 'fit';              // 'fit' | 'width' | a scale factor
  let mode = null;               // null | 'add' | 'order'
  let draw = null;               // add mode: drag in progress {x0,y0,x1,y1}
  let pendingBox = null;         // add mode: drawn box awaiting its text
  let threshold = Number(prefs.get('threshold', CONFIG.lowWc));
  if (!(threshold >= 0 && threshold <= 1)) threshold = CONFIG.lowWc;
  let showAll = prefs.get('showAll', true) !== false;
  // Reading-order mode. `order` is the server's preview of the pending
  // batch: the words in reading order with their boxes, line breaks and
  // places in the transcript -- computed by the code that will save it.
  let order = null;
  let orderSel = [];             // load-time word indices, in reading order
  let orderAnchor = null;        // where Shift+click extends from
  let orderSeq = 0;              // discards out-of-date preview responses
  let press = null;              // order mode: mouse press, until release says click or box

  const say = (msg, kind) => { status.textContent = msg || ''; status.className = 'status' + (kind ? ' ' + kind : ''); };
  const dirty = () => log.length > 0 || clearPending;
  const flagged = (w) => w.wc === null || w.wc === undefined || w.wc < threshold;
  const wordOp = (i) => log.find(o => o.index === i && WORD_OPS.has(o.op));
  const wordText = (i) => {
    const op = wordOp(i);
    return op && op.op === 'edit' ? op.text : page.words[i].text;
  };
  const unresolved = () => page.words.filter(w => flagged(w) && !wordOp(w.i));

  const refreshPending = () => {
    const n = log.length + (clearPending ? 1 : 0);
    $('pending').textContent = n ? `${n} unsaved change${n === 1 ? '' : 's'}` : '';
    $('save').disabled = !n || CONFIG.readOnly;
    $('undo').disabled = !undoStack.length;
  };

  /* Record one correction. Returns a promise that settles once the views
   * have caught up (the reading-order preview is a round trip), or null
   * when the correction was refused. */
  const record = (op) => {
    if (clearPending && op.op !== 'add') {
      say('The page is queued to be cleared — save that first.', 'err');
      return null;
    }
    const entry = { op };
    if (WORD_OPS.has(op.op)) {
      entry.prev = wordOp(op.index);
      if (entry.prev) log.splice(log.indexOf(entry.prev), 1);
    }
    log.push(op);
    undoStack.push(entry);
    return changed();
  };

  const changed = () => {
    refreshPending();
    drawOverlay();
    return mode === 'order' ? refreshOrder() : Promise.resolve();
  };

  const undo = () => {
    const entry = undoStack.pop();
    if (!entry) { say('Nothing to undo on this page.'); return; }
    const at = log.indexOf(entry.op);
    if (at >= 0) log.splice(at, 1);
    if (entry.prev) log.push(entry.prev);
    const op = entry.op;
    if (WORD_OPS.has(op.op)) {
      say(`Undone: ${op.op === 'delete' ? 'deletion of' : op.op === 'verify' ? 'confirmation of' : 'correction of'} “${page.words[op.index].text}”.`);
      if (mode === null) select(page.words[op.index]);
    } else {
      say(op.op === 'add' ? `Undone: added word “${op.text}”.` : 'Undone: reading-order move.');
    }
    changed();
  };

  /* ---- geometry ---- */
  const scale = () => {
    if (!page) return 1;
    if (typeof zoom === 'number') return zoom;
    const fitW = Math.max(scroller.clientWidth - 34, 200) / page.imageWidth;
    if (zoom === 'width') return fitW;
    return Math.min(fitW, Math.max(scroller.clientHeight - 34, 200) / page.imageHeight);
  };
  const applyScale = () => {
    if (!page) return;
    const s = scale();
    const w = Math.max(1, Math.round(page.imageWidth * s));
    const h = Math.max(1, Math.round(page.imageHeight * s));
    stage.style.width = w + 'px';
    stage.style.height = h + 'px';
    scan.width = w; scan.height = h;
    overlay.setAttribute('viewBox', `0 0 ${page.imageWidth} ${page.imageHeight}`);
    const select = $('zoom');
    const custom = select.querySelector('option[value="custom"]');
    if (typeof zoom === 'number' && !ZOOM_CHOICES.some(([v]) => v === zoom)) {
      custom.hidden = false;
      custom.textContent = `${Math.round(zoom * 100)}%`;
      select.value = 'custom';
    } else {
      custom.hidden = true;
      select.value = String(zoom);
    }
  };
  /* Pointer position in source-image pixels -- the frame every box and every
   * add-word box is expressed in, on both sides of the API. */
  const toImage = (event) => {
    const rect = scan.getBoundingClientRect();
    const s = rect.width / page.imageWidth;
    return { x: (event.clientX - rect.left) / s, y: (event.clientY - rect.top) / s };
  };
  const center = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];

  /* ---- overlay ---- */
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
    return node;
  };

  const svgTitle = (text) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    node.textContent = text;
    return node;
  };

  /* Which colour class a word's box gets. Mirrors the Tk GUI's convention so
   * a corrector moving between the two reads the same page the same way. */
  const boxClass = (w) => {
    const op = wordOp(w.i);
    if (op) return op.op === 'delete' ? 'gone' : 'done';
    if (flagged(w)) return 'flag';
    if (w.wc >= 1.0) return 'done';
    return 'good';
  };

  const drawOverlay = () => {
    overlay.replaceChildren();
    if (!page) return;
    if (mode === 'order') { drawOrder(); return; }
    if (!clearPending) {
      for (const w of page.words) {
        const op = wordOp(w.i);
        const isSel = selected && selected.i === w.i;
        if (!showAll && !flagged(w) && !op && !isSel) continue;
        const [x, y, bw, bh] = w.box;
        const rect = svgEl('rect', { x, y, width: Math.max(bw, 1), height: Math.max(bh, 1) });
        rect.setAttribute('class', 'wbox ' + boxClass(w) + (isSel ? ' sel' : ''));
        rect.addEventListener('mousedown', (ev) => { if (mode !== 'add') ev.stopPropagation(); });
        rect.addEventListener('click', (ev) => {
          if (mode === 'add') return;
          ev.stopPropagation();
          select(w);
        });
        const label = op && op.op === 'edit' ? `${w.text} → ${op.text}`
                    : op && op.op === 'delete' ? `${w.text} (delete)`
                    : op && op.op === 'verify' ? `${w.text} (confirmed)`
                    : `${w.text} · ${pct(w.wc)}`;
        rect.append(svgTitle(label));
        overlay.append(rect);
      }
    }
    for (const a of log.filter(o => o.op === 'add')) {
      const [x, y, bw, bh] = a.box;
      const rect = svgEl('rect', { x, y, width: bw, height: bh });
      rect.setAttribute('class', 'wbox added');
      rect.append(svgTitle(`${a.text} (added)`));
      overlay.append(rect);
    }
    if (draw) {
      overlay.append(svgEl('rect', {
        class: 'drawbox',
        x: Math.min(draw.x0, draw.x1), y: Math.min(draw.y0, draw.y1),
        width: Math.abs(draw.x1 - draw.x0), height: Math.abs(draw.y1 - draw.y0),
      }));
    }
    if (pendingBox) {
      const [x, y, bw, bh] = pendingBox;
      overlay.append(svgEl('rect', { class: 'drawbox', x, y, width: bw, height: bh }));
    }
  };

  /* Reading-order view: every word outlined, the selection heavy, and the
   * reading path -- a thin line from word to word along each line and an
   * arrow from the end of one line to the start of the next -- so a page
   * read in the wrong order shows at a glance. */
  const drawOrder = () => {
    if (!order) return;
    const size = Math.max(page.imageWidth / 130, 8);
    const marker = svgEl('marker', {
      id: 'order-arrow', viewBox: '0 0 10 10', refX: 10, refY: 5, orient: 'auto',
      markerUnits: 'userSpaceOnUse', markerWidth: size, markerHeight: size,
    });
    marker.append(svgEl('path', { d: 'M0,0 L10,5 L0,10 z', class: 'oarrow' }));
    const defs = svgEl('defs', {});
    defs.append(marker);
    overlay.append(defs);
    const placed = order.order.filter(o => o.box);
    let prev = null;
    for (const o of placed) {
      const [x2, y2] = center(o.box);
      if (prev) {
        const [x1, y1] = center(prev.box);
        const brk = prev.line !== o.line;
        overlay.append(svgEl('line', {
          x1, y1, x2, y2, class: 'opath' + (brk ? ' brk' : ''),
          'marker-end': brk ? 'url(#order-arrow)' : null,
        }));
      }
      prev = o;
    }
    const chosen = new Set(orderSel);
    for (const o of placed) {
      const [x, y, bw, bh] = o.box;
      const cls = o.i === null ? ' added' : chosen.has(o.i) ? ' chosen' : '';
      overlay.append(svgEl('rect', {
        x, y, width: Math.max(bw, 1), height: Math.max(bh, 1), class: 'obox' + cls,
      }));
    }
    if (press && press.dragging) {
      overlay.append(svgEl('rect', {
        class: 'drawbox',
        x: Math.min(press.x0, press.x1), y: Math.min(press.y0, press.y1),
        width: Math.abs(press.x1 - press.x0), height: Math.abs(press.y1 - press.y0),
      }));
    }
  };

  /* ---- editor ---- */
  const editor = $('editor'), edText = $('ed-text');

  const select = (w) => {
    selected = w;
    if (!w) { editor.hidden = true; drawOverlay(); return; }
    $('addbar').hidden = true;
    editor.hidden = false;
    const wc = w.wc;
    const badge = $('ed-wc');
    badge.textContent = wc === null || wc === undefined ? 'unscored' : `confidence ${pct(wc)}`;
    badge.className = 'badge' + (flagged(w) ? ' low' : '');
    $('ed-pos').textContent = `word ${w.i + 1} of ${page.words.length} · ${unresolved().length} flagged left`;
    edText.value = wordText(w.i);
    const [x, y, bw, bh] = w.box;
    $('snippet').src = `${pageUrl(refId, stem)}/crop?x=${x}&y=${y}&w=${bw}&h=${bh}&zoom=2.5`;
    drawOverlay();
    scrollToBox(w.box);
    edText.focus();
    edText.select();
  };

  const scrollToBox = (box) => {
    const s = scale();
    const cx = (box[0] + box[2] / 2) * s;
    const cy = (box[1] + box[3] / 2) * s;
    const stageRect = stage.getBoundingClientRect();
    const scrRect = scroller.getBoundingClientRect();
    const offsetX = stageRect.left - scrRect.left + scroller.scrollLeft;
    const offsetY = stageRect.top - scrRect.top + scroller.scrollTop;
    if (cx + offsetX < scroller.scrollLeft || cx + offsetX > scroller.scrollLeft + scroller.clientWidth
        || cy + offsetY < scroller.scrollTop || cy + offsetY > scroller.scrollTop + scroller.clientHeight) {
      scroller.scrollTo({ left: Math.max(0, offsetX + cx - scroller.clientWidth / 2),
                          top: Math.max(0, offsetY + cy - scroller.clientHeight / 2),
                          behavior: 'smooth' });
    }
  };

  /* Next/previous *unresolved* flagged word after the one in hand, by
   * document position -- the same rule as remediate.ReviewPage.step_from.
   * Positional, not membership-based: correcting a word takes it out of the
   * flagged set immediately, so "where am I" cannot be answered by looking
   * the current word up in the list. */
  const step = (delta) => {
    if (!page || !page.words.length || mode === 'order') return;
    const pool = unresolved();
    if (!pool.length) { select(null); say('No flagged words left on this page. Save, then go to the next page.', 'ok'); return; }
    const here = selected ? selected.i : (delta > 0 ? -1 : page.words.length);
    const later = pool.filter(w => w.i > here);
    const earlier = pool.filter(w => w.i < here);
    const next = delta > 0 ? (later.length ? later[0] : pool[0])
                           : (earlier.length ? earlier[earlier.length - 1] : pool[pool.length - 1]);
    const wrapped = selected && (delta > 0 ? !later.length : !earlier.length);
    select(next);
    if (wrapped) say(`Wrapped to the ${delta > 0 ? 'top' : 'bottom'} of the page — ${pool.length} flagged word(s) still unreviewed.`);
  };

  const commitEdit = (advance) => {
    if (!selected) return;
    const text = edText.value.trim();
    if (!text) { deleteSelected(); return; }
    if (text === selected.text) {
      // Unchanged text means "the OCR was right, the confidence was just
      // low" -- the same claim as an edit, recorded as a confirmation so
      // the count of words a human actually *changed* stays honest.
      if (!record({ op: 'verify', index: selected.i })) return;
      say(`Confirmed “${text}”.`);
    } else {
      if (!record({ op: 'edit', index: selected.i, text })) return;
      say(`Queued “${selected.text}” → “${text}”.`);
    }
    if (advance) step(1); else select(selected);
  };

  /* Deleting happens at once, without an "are you sure?" -- Ctrl+Z puts
   * the word back. */
  const deleteSelected = () => {
    if (!selected) return;
    const w = selected;
    if (!record({ op: 'delete', index: w.i })) return;
    step(1);
    say(`Deleted “${w.text}” (Ctrl+Z puts it back).`);
  };

  $('ed-commit').addEventListener('click', () => commitEdit(true));
  $('ed-confirm').addEventListener('click', () => {
    if (!selected) return;
    edText.value = selected.text;
    commitEdit(true);
  });
  $('ed-delete').addEventListener('click', deleteSelected);
  $('ed-cancel').addEventListener('click', () => select(null));
  edText.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); commitEdit(true); }
    else if (ev.key === 'Escape') { ev.preventDefault(); select(null); }
  });

  /* Symbol keyboard: rows of keys under category tabs, in the layout the
   * server sends (symbols.py -- the Tk GUI renders the same one, so the two
   * offer identical keys). A key types like a key press: it replaces the
   * selection and leaves the caret after it. mousedown is cancelled so a
   * click never takes focus away from the word being edited. */
  const insertSymbol = (g) => {
    const at = edText.selectionStart ?? edText.value.length;
    edText.value = edText.value.slice(0, at) + g + edText.value.slice(edText.selectionEnd ?? at);
    edText.focus();
    edText.setSelectionRange(at + g.length, at + g.length);
  };
  const keyboard = CONFIG.symbols || { categories: [], labels: {} };
  const keepFocus = (ev) => ev.preventDefault();
  const paintSymbols = () => {
    const names = keyboard.categories.map(c => c.name);
    if (!names.includes(symbolTab)) symbolTab = names[0] ?? null;
    $('symbol-tabs').replaceChildren(...keyboard.categories.map(c => el('button', {
      type: 'button', class: 'symtab', text: c.name,
      'aria-pressed': String(c.name === symbolTab), onmousedown: keepFocus,
      onclick: () => { symbolTab = c.name; paintSymbols(); },
    })));
    const shown = keyboard.categories.find(c => c.name === symbolTab);
    $('glyphs').replaceChildren(...(shown ? shown.rows : []).map(row => el('div', { class: 'glyph-row' },
      ...row.map(g => el('button', {
        type: 'button', text: g, title: keyboard.labels[g] || g,
        onmousedown: keepFocus, onclick: () => insertSymbol(g),
      })))));
  };
  paintSymbols();
  const symbolsOn = $('symbols-on');
  symbolsOn.checked = prefs.get('symbols', true) !== false;
  $('symbols').hidden = !symbolsOn.checked;
  symbolsOn.addEventListener('change', () => {
    $('symbols').hidden = !symbolsOn.checked;
    prefs.set('symbols', symbolsOn.checked);
  });

  /* ---- modes: add word / reading order (one at a time) ---- */
  const setMode = (next) => {
    if (mode === next) next = null;
    mode = next;
    $('add-mode').setAttribute('aria-pressed', String(mode === 'add'));
    $('order-mode').setAttribute('aria-pressed', String(mode === 'order'));
    stage.classList.toggle('adding', mode === 'add');
    stage.classList.toggle('ordering', mode === 'order');
    $('order-panel').hidden = mode !== 'order';
    pendingBox = null; draw = null; press = null;
    $('addbar').hidden = true;
    selected = null;
    editor.hidden = true;
    orderSel = []; orderAnchor = null; order = null;
    if (mode === 'add') say('Drag a box around the word the OCR missed.');
    if (mode === 'order') { paintOrderPanel(); refreshOrder(); }
    applyScale();   // the reading-order panel takes width from the scan
    drawOverlay();
  };
  $('add-mode').addEventListener('click', () => setMode('add'));
  $('order-mode').addEventListener('click', () => setMode('order'));

  /* ---- add-word mode ---- */
  $('add-cancel').addEventListener('click', () => { pendingBox = null; $('addbar').hidden = true; drawOverlay(); });
  $('add-commit').addEventListener('click', () => {
    const text = $('add-text').value.trim();
    if (!pendingBox) { say('Draw the box first.', 'err'); return; }
    if (!text) { say('Type the word to add.', 'err'); return; }
    const box = pendingBox;
    pendingBox = null;
    $('add-text').value = '';
    $('addbar').hidden = true;
    record({ op: 'add', box, text });
    say(`Queued new word “${text}”.`);
  });
  $('add-text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); $('add-commit').click(); }
  });

  /* ---- reading-order mode ---- */
  const batch = () => (clearPending
    ? [{ op: 'clear' }, ...log.filter(o => o.op === 'add')]
    : [...log]);

  const refreshOrder = async () => {
    const seq = ++orderSeq;
    try {
      const res = await postJSON(`${pageUrl(refId, stem)}/preview`, { ops: batch() });
      if (seq !== orderSeq || mode !== 'order') return;
      // A move that changed nothing (the words were already read there, or
      // already first or last) is dropped, so it neither counts as a change
      // nor waits on the undo stack doing nothing.
      if (!clearPending && res.report.noops.length) {
        const dead = new Set(res.report.noops.map(k => log[k]));
        for (let k = log.length - 1; k >= 0; k--) if (dead.has(log[k])) log.splice(k, 1);
        for (let k = undoStack.length - 1; k >= 0; k--) if (dead.has(undoStack[k].op)) undoStack.splice(k, 1);
        say('Nothing to move — the words are already read there.');
        refreshPending();
      }
      order = res;
      const present = new Set(order.order.map(o => o.i));
      orderSel = orderSel.filter(i => present.has(i));
      if (!present.has(orderAnchor)) orderAnchor = orderSel[0] ?? null;
      paintOrderPanel();
      drawOverlay();
    } catch (e) {
      if (seq === orderSeq) say(`Could not show the reading order: ${e.message}`, 'err');
    }
  };

  const paintOrderPanel = () => {
    const n = orderSel.length;
    $('order-hint').textContent = n
      ? `${n} word(s) selected. Now click the word they should come after — on the scan or in the transcript. “Page start” puts them first; Ctrl+↑/Ctrl+↓ nudge them one word; Esc clears; Ctrl+Z undoes a move.`
      : 'Select the words that are out of place: click one (on the scan or in the transcript below), Shift+click to extend to a run, Ctrl+click to add or drop a word, or drag a box around a whole column or caption on the scan.';
    for (const id of ['order-earlier', 'order-later', 'order-start', 'order-clear']) $(id).disabled = !n;
    const box = $('order-text');
    if (!order) { box.textContent = 'Loading…'; return; }
    const text = order.transcript;
    if (!text) { box.textContent = '(no words on this page)'; return; }
    box.replaceChildren();
    const chosen = new Set(orderSel);
    let at = 0, prevChosen = false, first = null;
    for (const o of order.order) {
      const isChosen = o.i !== null && chosen.has(o.i);
      const gap = text.slice(at, o.start);
      // The space between two selected words is part of the selection.
      if (gap) box.append(prevChosen && isChosen && !gap.includes('\n')
        ? el('span', { class: 'chosen', text: gap }) : document.createTextNode(gap));
      const span = el('span', {
        class: 'tw' + (isChosen ? ' chosen' : '') + (o.i === null ? ' added' : ''),
        text: text.slice(o.start, o.end),
        'data-i': o.i === null ? null : String(o.i),
      });
      box.append(span);
      if (isChosen && !first) first = span;
      at = o.end;
      prevChosen = isChosen;
    }
    box.append(document.createTextNode(text.slice(at)));
    if (first) first.scrollIntoView({ block: 'nearest' });
  };

  const orderPos = () => {
    const pos = new Map();
    order.order.forEach((o, k) => { if (o.i !== null) pos.set(o.i, k); });
    return pos;
  };
  const setOrderSel = (indices, anchor) => {
    const pos = orderPos();
    orderSel = [...new Set(indices)].filter(i => pos.has(i)).sort((a, b) => pos.get(a) - pos.get(b));
    orderAnchor = anchor;
    paintOrderPanel();
    drawOverlay();
  };

  /* One click in reading-order mode, on the scan or in the transcript.
   * Nothing selected: the word becomes the selection. Shift: the selection
   * becomes every word from the first one picked to this one. A selection
   * and a plain click elsewhere: the selection moves to read right after
   * the clicked word. A plain click inside the selection starts over. */
  const orderPick = (i, shift) => {
    if (!order) return;
    if (i === null || i === undefined) {
      if (!shift && orderSel.length) setOrderSel([], null);
      return;
    }
    if (shift && orderAnchor !== null) {
      const pos = orderPos();
      const a = pos.get(orderAnchor), b = pos.get(i);
      if (a !== undefined && b !== undefined) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        setOrderSel(order.order.slice(lo, hi + 1).map(o => o.i).filter(v => v !== null), orderAnchor);
        return;
      }
    }
    if (!orderSel.length || shift || orderSel.includes(i)) { setOrderSel([i], i); return; }
    moveSelection({ op: 'move', indices: [...orderSel], after: i }, `after “${wordText(i)}”`);
  };

  const orderToggle = (i) => {
    if (i === null || i === undefined || !order) return;
    if (orderSel.includes(i)) {
      const rest = orderSel.filter(v => v !== i);
      setOrderSel(rest, orderAnchor === i ? (rest[0] ?? null) : orderAnchor);
    } else {
      setOrderSel([...orderSel, i], i);
    }
  };

  /* Select the words whose centres fall inside a box: a column or a caption
   * in one drag, where the OCR's lines interleave it with the text beside
   * it. With Shift or Ctrl held the box adds to the selection. */
  const orderBoxSelect = (box, add) => {
    const [x, y, bw, bh] = box;
    let hits = order.order.filter(o => o.i !== null && o.box).filter(o => {
      const [cx, cy] = center(o.box);
      return cx >= x && cx <= x + bw && cy >= y && cy <= y + bh;
    }).map(o => o.i);
    if (!hits.length) { say('No words inside that box.'); return; }
    const anchor = add && orderAnchor !== null ? orderAnchor : hits[0];
    if (add) hits = [...orderSel, ...hits];
    setOrderSel(hits, anchor);
  };

  const orderEntryAt = (x, y) => {
    let best = null;
    for (const o of order.order) {
      if (o.i === null || !o.box) continue;
      const [bx, by, bw, bh] = o.box;
      if (x >= bx && x <= bx + bw && y >= by && y <= by + bh
          && (!best || bw * bh < best.box[2] * best.box[3])) best = o;
    }
    return best;
  };

  const moveSelection = async (op, where) => {
    if (!orderSel.length) return;
    const n = orderSel.length;
    const done = record(op);
    if (!done) return;
    await done;
    if (log.includes(op)) say(`Moved ${n} word(s) ${where}. Ctrl+Z undoes.`);
  };
  const nudge = (delta) => {
    if (mode === 'order' && orderSel.length) {
      moveSelection({ op: 'nudge', indices: [...orderSel], delta },
                    delta < 0 ? 'one word earlier' : 'one word later');
    }
  };
  $('order-earlier').addEventListener('click', () => nudge(-1));
  $('order-later').addEventListener('click', () => nudge(1));
  $('order-start').addEventListener('click', () => {
    moveSelection({ op: 'move', indices: [...orderSel], after: null }, 'to the start of the page');
  });
  $('order-clear').addEventListener('click', () => setOrderSel([], null));
  const orderText = $('order-text');
  orderText.addEventListener('mousedown', (ev) => ev.preventDefault());  // no text selection
  orderText.addEventListener('click', (ev) => {
    const span = ev.target.closest('.tw');
    const i = span && span.dataset.i !== undefined ? Number(span.dataset.i) : null;
    if (ev.ctrlKey || ev.metaKey) orderToggle(i);
    else orderPick(i, ev.shiftKey);
  });

  /* ---- mouse on the scan: add-word drags, reading-order clicks and boxes ---- */
  stage.addEventListener('mousedown', (ev) => {
    if (!page || ev.button !== 0) return;
    if (mode === 'order') {
      if (!order) return;
      ev.preventDefault();
      const p = toImage(ev);
      press = { x0: p.x, y0: p.y, x1: p.x, y1: p.y, cx: ev.clientX, cy: ev.clientY,
                mods: ev.shiftKey ? 'shift' : (ev.ctrlKey || ev.metaKey) ? 'ctrl' : '',
                dragging: false };
      return;
    }
    if (mode !== 'add') return;
    ev.preventDefault();
    const p = toImage(ev);
    draw = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  });
  const onMove = (ev) => {
    if (press) {
      const p = toImage(ev);
      press.x1 = p.x; press.y1 = p.y;
      // A few pixels of wobble is still a click.
      if (!press.dragging && Math.hypot(ev.clientX - press.cx, ev.clientY - press.cy) > 5) press.dragging = true;
      if (press.dragging) drawOverlay();
      return;
    }
    if (!draw) return;
    const p = toImage(ev);
    draw.x1 = p.x; draw.y1 = p.y;
    drawOverlay();
  };
  const onUp = () => {
    if (press) {
      const p = press;
      press = null;
      if (p.dragging) {
        orderBoxSelect([Math.min(p.x0, p.x1), Math.min(p.y0, p.y1),
                        Math.abs(p.x1 - p.x0), Math.abs(p.y1 - p.y0)], p.mods !== '');
        drawOverlay();
      } else {
        const hit = orderEntryAt(p.x0, p.y0);
        if (p.mods === 'ctrl') orderToggle(hit && hit.i);
        else orderPick(hit ? hit.i : null, p.mods === 'shift');
      }
      return;
    }
    if (!draw) return;
    const box = [Math.min(draw.x0, draw.x1), Math.min(draw.y0, draw.y1),
                 Math.abs(draw.x1 - draw.x0), Math.abs(draw.y1 - draw.y0)];
    draw = null;
    // A stray click in add mode is a click, not a zero-size word.
    if (box[2] < 4 || box[3] < 4) { drawOverlay(); return; }
    pendingBox = box;
    $('addbar').hidden = false;
    $('add-text').focus();
    drawOverlay();
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);

  /* ---- clear page ---- */
  $('clear-page').addEventListener('click', () => {
    if (clearPending) {
      clearPending = false;
      say('Clear cancelled.');
    } else {
      if (log.some(o => o.op !== 'add')) { say('Save or undo the queued corrections first — clear cannot be combined with them.', 'err'); return; }
      if (!confirm(`Remove every word on ${stem}?\n\nFor pages whose OCR is wholly hallucinated — covers, photographs, blank pages where texture read as text. The page skeleton stays, so you can add back any words that were real.`)) return;
      clearPending = true;
      selected = null;
      editor.hidden = true;
      say(`${page.words.length} word(s) queued for removal. Save to apply.`, 'err');
    }
    $('clear-page').setAttribute('aria-pressed', String(clearPending));
    changed();
  });

  /* ---- zoom ---- */
  const zoomSelect = $('zoom');
  zoomSelect.replaceChildren(
    ...ZOOM_CHOICES.map(([v, label]) => el('option', { value: String(v), text: label })),
    el('option', { value: 'custom', hidden: true }));
  const setZoom = (z) => { zoom = z; applyScale(); drawOverlay(); };
  zoomSelect.addEventListener('change', () => {
    const v = zoomSelect.value;
    if (v === 'custom') return;
    setZoom(v === 'fit' || v === 'width' ? v : Number(v));
  });
  const ZOOM_STEPS = [0.1, 0.15, 0.25, 0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0];
  $('zoom-in').addEventListener('click', () => {
    const cur = scale();
    setZoom(ZOOM_STEPS.find(z => z > cur + 0.001) ?? ZOOM_STEPS[ZOOM_STEPS.length - 1]);
  });
  $('zoom-out').addEventListener('click', () => {
    const cur = scale();
    setZoom([...ZOOM_STEPS].reverse().find(z => z < cur - 0.001) ?? ZOOM_STEPS[0]);
  });
  // Ctrl+scroll zooms about the pointer, ~20% a notch: the point under the
  // cursor stays under it. Plain and Shift+scroll keep panning.
  scroller.addEventListener('wheel', (ev) => {
    if (!(ev.ctrlKey || ev.metaKey) || !page) return;
    ev.preventDefault();
    const before = scan.getBoundingClientRect();
    const s0 = before.width / page.imageWidth;   // the scale on screen now
    const ix = (ev.clientX - before.left) / s0, iy = (ev.clientY - before.top) / s0;
    const s1 = Math.min(4, Math.max(0.05, s0 * (ev.deltaY < 0 ? 1.2 : 1 / 1.2)));
    if (Math.abs(s1 - s0) < 1e-4) return;
    setZoom(s1);
    const after = scan.getBoundingClientRect();
    scroller.scrollLeft += after.left + ix * s1 - ev.clientX;
    scroller.scrollTop += after.top + iy * s1 - ev.clientY;
  }, { passive: false });
  // The fit zooms follow the scan area itself, not just the window: the
  // editor bar and the reading-order panel take room from it too.
  const onResize = () => { if (page && typeof zoom !== 'number') applyScale(); };
  const resizeWatch = new ResizeObserver(onResize);
  resizeWatch.observe(scroller);

  /* ---- what to flag, what to show ---- */
  const thresholdSelect = $('threshold');
  const choices = Array.from({ length: 21 }, (_, k) => k * 5);
  const current = Math.round(threshold * 100);
  if (!choices.includes(current)) {
    choices.push(current);
    choices.sort((a, b) => a - b);
  }
  thresholdSelect.replaceChildren(...choices.map(p => el('option', { value: String(p), text: `${p}%` })));
  thresholdSelect.value = String(current);
  thresholdSelect.addEventListener('change', () => {
    threshold = Number(thresholdSelect.value) / 100;
    prefs.set('threshold', threshold);
    drawOverlay();
    if (selected) select(selected);
    say(`${unresolved().length} word(s) flagged below ${pct(threshold)}.`);
  });
  const showAllBox = $('show-all');
  showAllBox.checked = showAll;
  showAllBox.addEventListener('change', () => {
    showAll = showAllBox.checked;
    prefs.set('showAll', showAll);
    drawOverlay();
  });

  /* ---- save ---- */
  const save = async (force) => {
    if (!dirty()) { say('Nothing to save.'); return; }
    if (CONFIG.readOnly) { say('Read-only server: corrections cannot be saved.', 'err'); return; }
    $('save').disabled = true;
    say('Saving…');
    try {
      const res = await postJSON(pageUrl(refId, stem), { token: page.token, ops: batch(), force: !!force });
      log.length = 0; undoStack.length = 0; clearPending = false;
      $('clear-page').setAttribute('aria-pressed', 'false');
      selected = null;
      editor.hidden = true;
      // Word indices are positions in the saved page now: the old ones
      // (and a reading-order selection made of them) are spent.
      orderSel = []; orderAnchor = null;
      page = res.page || page;
      applyScale();
      await changed();
      say(res.report.summary + (res.txt ? ' Transcript regenerated.' : ''), 'ok');
      loadSidebar();          // PC and reviewed-state have moved
    } catch (e) {
      refreshPending();
      if (e.status === 409) {
        say(e.message, 'err');
        if (confirm(e.message + '\n\nOverwrite the newer version with your corrections?')) {
          await save(true);
          return;
        }
      } else {
        say(`Save failed: ${e.message}`, 'err');
      }
    }
  };
  $('save').addEventListener('click', () => save(false));
  $('undo').addEventListener('click', undo);
  $('prev-flag').addEventListener('click', () => step(-1));
  $('next-flag').addEventListener('click', () => step(1));

  /* ---- pages ---- */
  const gotoPage = (delta) => {
    const stems = walk && walk.refId === refId ? walk.stems : objectPages.map(p => p.stem);
    const k = stems.indexOf(stem);
    const next = stems[k + delta];
    if (k < 0 || next === undefined) {
      say(delta > 0 ? 'This is the last page in the list.' : 'This is the first page in the list.');
      return;
    }
    if (dirty() && !confirm('You have unsaved corrections on this page. Leave anyway?')) return;
    location.hash = `#/obj/${enc(refId)}/${enc(next)}`;
  };
  $('page-prev').addEventListener('click', () => gotoPage(-1));
  $('page-next').addEventListener('click', () => gotoPage(1));

  /* ---- keyboard ---- */
  const onKey = (ev) => {
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key === 's') { ev.preventDefault(); save(false); return; }
    // Browsers keep Ctrl+Page Up/Down for their own tabs; Alt takes the
    // Tk tab's page-walk shortcut instead.
    if (ev.altKey && (ev.key === 'PageUp' || ev.key === 'PageDown')) {
      ev.preventDefault();
      gotoPage(ev.key === 'PageUp' ? -1 : 1);
      return;
    }
    const target = ev.target;
    const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
                || target instanceof HTMLSelectElement;
    // Ctrl+Z undoes the last correction -- except while the corrector has
    // typed into the word box, where it undoes their typing.
    const untouchedEditor = target === edText && selected && edText.value === wordText(selected.i);
    if (mod && ev.key.toLowerCase() === 'z' && !ev.shiftKey && (!typing || untouchedEditor)) {
      ev.preventDefault();
      undo();
      return;
    }
    if (typing) return;
    if (mode === 'order' && mod && (ev.key === 'ArrowUp' || ev.key === 'ArrowDown')) {
      ev.preventDefault();
      nudge(ev.key === 'ArrowUp' ? -1 : 1);
      return;
    }
    if (mod || ev.altKey) return;
    if (ev.key === 'n') { ev.preventDefault(); step(1); }
    else if (ev.key === 'p') { ev.preventDefault(); step(-1); }
    else if (ev.key === 'a') { ev.preventDefault(); setMode('add'); }
    else if (ev.key === 'r') { ev.preventDefault(); setMode('order'); }
    else if (ev.key === 'Escape') {
      if (mode === 'order' && orderSel.length) setOrderSel([], null);
      else if (mode === 'add') setMode(null);
      else select(null);
    }
  };
  document.addEventListener('keydown', onKey);
  const onLeave = (ev) => { if (dirty()) { ev.preventDefault(); ev.returnValue = ''; } };
  window.addEventListener('beforeunload', onLeave);
  // Hash navigation replaces #view wholesale; drop the window-level
  // listeners with it so handlers for a page nobody is looking at do not
  // accumulate.
  const observer = new MutationObserver(() => {
    if (!document.body.contains(status)) {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('beforeunload', onLeave);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      resizeWatch.disconnect();
      observer.disconnect();
    }
  });
  observer.observe(view, { childList: true });

  /* ---- sidebar ---- */
  const loadSidebar = async () => {
    try {
      const data = await api(objUrl(refId));
      objectPages = data.pages;
    } catch { return; }
    const worst = $('side-worst').checked;
    if (!walk || walk.refId !== refId || walk.worst !== worst) {
      walk = { refId, worst, stems: pageSort(objectPages, worst).map(p => p.stem) };
    }
    const byStem = new Map(objectPages.map(p => [p.stem, p]));
    const listed = [...walk.stems.filter(s => byStem.has(s)),
                    ...objectPages.map(p => p.stem).filter(s => !walk.stems.includes(s))];
    walk.stems = listed;
    $('side-pages').replaceChildren(...listed.map(s => byStem.get(s)).map(p => el('li', {
      'aria-current': p.stem === stem ? 'true' : null,
      title: `${p.status}${p.pc === null ? '' : ` · PC ${pct(p.pc)}`}${p.reviewed ? ' · reviewed' : ''}`,
      onclick: () => {
        if (p.stem === stem) return;
        if (dirty() && !confirm('You have unsaved corrections on this page. Leave anyway?')) return;
        location.hash = `#/obj/${enc(refId)}/${enc(p.stem)}`;
      },
    },
      el('span', { class: 'dot' + (p.status === 'failed' ? ' failed' : p.reviewed ? ' reviewed' : '') }),
      el('span', { class: 'mono', text: p.stem }),
      el('span', { class: 'pc', text: p.pc === null ? p.status : pct(p.pc) }),
    )));
    const current = $('side-pages').querySelector('[aria-current="true"]');
    if (current) current.scrollIntoView({ block: 'nearest' });
  };
  $('side-ref').textContent = refId;
  if (walk && walk.refId === refId) $('side-worst').checked = walk.worst;
  $('side-worst').addEventListener('change', () => { walk = null; loadSidebar(); });

  /* ---- go ---- */
  say('Loading page…');
  loadSidebar();
  refreshPending();
  try {
    page = await api(pageUrl(refId, stem));
  } catch (e) {
    say(e.status === 409
      ? `${e.message}`
      : `Could not open ${stem}: ${e.message}`, 'err');
    return;
  }
  scan.src = `${pageUrl(refId, stem)}/image`;
  scan.alt = `Scan of ${stem}`;
  applyScale();
  drawOverlay();
  refreshPending();
  const flaggedCount = unresolved().length;
  say(`${page.words.length} word(s), ${flaggedCount} flagged below ${pct(threshold)}`
      + (page.pc ? ` · page confidence ${pct(page.pc)}` : '')
      + '. Click a word, or press n for the next flagged one.');
  if (flaggedCount) step(1);
}

/* -------------------------------------------------------------- router */

async function route() {
  const hash = location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/').filter(Boolean).map(decodeURIComponent);
  flash(null);
  try {
    if (parts[0] === 'obj' && parts.length >= 3) await renderPage(parts[1], parts.slice(2).join('/'));
    else if (parts[0] === 'obj' && parts.length === 2) await renderObject(parts[1]);
    else await renderWorklist();
  } catch (e) {
    view.replaceChildren(el('section', { class: 'pad' },
      el('h1', { text: 'Something went wrong' }),
      el('p', { class: 'muted', text: e.message }),
      el('p', {}, el('a', { href: '#/', text: '← back to the object list' }))));
  }
}

/* The header's Auto / Light / Dark switch. index.html sets the attribute
 * before the first paint; this keeps it current. */
function wireTheme() {
  const select = document.getElementById('theme');
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const saved = prefs.get('theme', 'auto');
  select.value = ['auto', 'light', 'dark'].includes(saved) ? saved : 'auto';
  const apply = () => {
    const dark = select.value === 'dark' || (select.value === 'auto' && media.matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  };
  select.addEventListener('change', () => { prefs.set('theme', select.value); apply(); });
  media.addEventListener('change', apply);
  apply();
}

(async function main() {
  wireTheme();
  try {
    CONFIG = await api('api/config');
    document.getElementById('version').textContent = 'v' + CONFIG.version
      + (CONFIG.readOnly ? ' · read-only' : '');
  } catch (e) {
    flash(`Cannot reach the review server: ${e.message}`, 'err');
  }
  if (CONFIG.readOnly) flash('Read-only server: corrections cannot be saved.', 'err');
  window.addEventListener('hashchange', route);
  await route();
})();
