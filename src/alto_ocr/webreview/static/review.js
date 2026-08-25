/* Transcript review client.
 *
 * Holds no authority over anything. Every semantic decision -- what an edit
 * means, what counts toward provenance, how a transcript is rendered -- is
 * the server's (remediate.py). This file's whole job is: show the scan with
 * its word boxes, collect what the corrector did, and post it as one batch.
 *
 * Pending corrections are kept in a Map keyed by word index, so a corrector
 * who fixes the same word twice produces one operation, not two. The server
 * rejects a batch with two operations on one word precisely because a double
 * count would overstate how many words a human changed in the ALTO's
 * provenance -- coalescing here is what keeps that honest.
 */
'use strict';

const GLYPHS = ['π','Σ','Δ','θ','Ω','μ','√','∫','∞','±','×','÷',
                '≈','≠','≤','≥','°','·','²','³','½','¼','¾','—'];
const ZOOMS = [0.1, 0.15, 0.25, 0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0];

const view = document.getElementById('view');
const crumbs = document.getElementById('crumbs');
const banner = document.getElementById('banner');
let CONFIG = { readOnly: false, lowWc: 0.5, version: '' };

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

const fmt = (n, digits = 2) =>
  (n === null || n === undefined) ? '—' : Number(n).toFixed(digits);

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

/* Which colour class a word's box gets. Mirrors the Tk GUI's convention so
 * a corrector moving between the two reads the same page the same way. */
function boxClass(word, pending) {
  const op = pending.get(word.i);
  if (op) {
    if (op.op === 'delete') return 'gone';
    return 'done';
  }
  if (word.wc === null || word.wc === undefined) return 'flag';
  if (word.wc >= 1.0) return 'done';
  if (word.wc < CONFIG.lowWc) return 'flag';
  return 'good';
}

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
        el('td', { class: 'num', text: fmt(r.meanPc) }),
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
      el('td', { class: 'num', text: fmt(p.pc) }),
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
    `${s.pages} page(s) · ${s.pagesToReview} left to review · mean PC ${fmt(s.meanPc)} · `
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
  const pending = new Map();     // word index -> {op, index, text?}
  const adds = [];               // {op:'add', box, text}
  let clearPending = false;
  let selected = null;           // word object
  let zoom = null;               // null = fit
  let addMode = false;
  let draw = null;               // in-progress drag {x0,y0,x1,y1}
  let pendingBox = null;         // committed drag awaiting text

  const say = (msg, kind) => { status.textContent = msg || ''; status.className = 'status' + (kind ? ' ' + kind : ''); };
  const dirty = () => pending.size > 0 || adds.length > 0 || clearPending;

  const opCount = () => pending.size + adds.length + (clearPending ? 1 : 0);
  const refreshPending = () => {
    const n = opCount();
    $('pending').textContent = n ? `${n} unsaved change${n === 1 ? '' : 's'}` : '';
    $('save').disabled = !n || CONFIG.readOnly;
  };

  /* ---- geometry ---- */
  const scale = () => {
    if (!page) return 1;
    if (zoom !== null) return zoom;
    const avail = Math.max(scroller.clientWidth - 34, 200);
    return Math.min(avail / page.imageWidth, 1);
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
    $('zoom-label').textContent = zoom === null ? 'fit' : Math.round(zoom * 100) + '%';
  };
  /* Pointer position in source-image pixels -- the frame every box and every
   * add-word box is expressed in, on both sides of the API. */
  const toImage = (event) => {
    const rect = scan.getBoundingClientRect();
    const s = rect.width / page.imageWidth;
    return { x: (event.clientX - rect.left) / s, y: (event.clientY - rect.top) / s };
  };

  /* ---- overlay ---- */
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };

  const svgTitle = (text) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    node.textContent = text;
    return node;
  };

  const drawOverlay = () => {
    overlay.replaceChildren();
    if (!page) return;
    if (!clearPending) {
      for (const w of page.words) {
        const [x, y, bw, bh] = w.box;
        const rect = svgEl('rect', { x, y, width: Math.max(bw, 1), height: Math.max(bh, 1) });
        rect.setAttribute('class', 'wbox ' + boxClass(w, pending) + (selected && selected.i === w.i ? ' sel' : ''));
        rect.addEventListener('mousedown', (ev) => { if (!addMode) ev.stopPropagation(); });
        rect.addEventListener('click', (ev) => {
          if (addMode) return;
          ev.stopPropagation();
          select(w);
        });
        const op = pending.get(w.i);
        const label = op && op.op === 'edit' ? `${w.text} → ${op.text}`
                    : op && op.op === 'delete' ? `${w.text} (delete)`
                    : op && op.op === 'verify' ? `${w.text} (confirmed)`
                    : w.text;
        rect.append(svgTitle(label));
        overlay.append(rect);
      }
    }
    for (const a of adds) {
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

  /* ---- editor ---- */
  const editor = $('editor'), edText = $('ed-text');

  const effectiveText = (w) => {
    const op = pending.get(w.i);
    return op && op.op === 'edit' ? op.text : w.text;
  };

  const select = (w) => {
    selected = w;
    if (!w) { editor.hidden = true; drawOverlay(); return; }
    $('addbar').hidden = true;
    editor.hidden = false;
    const wc = w.wc;
    const badge = $('ed-wc');
    badge.textContent = wc === null || wc === undefined ? 'no WC' : `WC ${fmt(wc)}`;
    badge.className = 'badge' + ((wc === null || wc === undefined || wc < CONFIG.lowWc) ? ' low' : '');
    const flagged = page.flagged.filter(i => !pending.has(i));
    $('ed-pos').textContent = `word ${w.i + 1} of ${page.words.length} · ${flagged.length} flagged left`;
    edText.value = effectiveText(w);
    const [x, y, bw, bh] = w.box;
    $('snippet').src = `${pageUrl(refId, stem)}/crop?x=${x}&y=${y}&w=${bw}&h=${bh}&zoom=2.5`;
    drawOverlay();
    scrollWordIntoView(w);
    edText.focus();
    edText.select();
  };

  const scrollWordIntoView = (w) => {
    const s = scale();
    const cx = (w.box[0] + w.box[2] / 2) * s;
    const cy = (w.box[1] + w.box[3] / 2) * s;
    const stageRect = stage.getBoundingClientRect();
    const scrRect = scroller.getBoundingClientRect();
    const offsetX = stageRect.left - scrRect.left + scroller.scrollLeft;
    const offsetY = stageRect.top - scrRect.top + scroller.scrollTop;
    const targetX = offsetX + cx - scroller.clientWidth / 2;
    const targetY = offsetY + cy - scroller.clientHeight / 2;
    if (cx + offsetX < scroller.scrollLeft || cx + offsetX > scroller.scrollLeft + scroller.clientWidth
        || cy + offsetY < scroller.scrollTop || cy + offsetY > scroller.scrollTop + scroller.clientHeight) {
      scroller.scrollTo({ left: Math.max(0, targetX), top: Math.max(0, targetY), behavior: 'smooth' });
    }
  };

  /* Queue one word operation, coalescing on the word index. */
  const queue = (op) => {
    if (clearPending) { say('The page is queued to be cleared — save that first.', 'err'); return; }
    pending.set(op.index, op);
    refreshPending();
    drawOverlay();
  };

  /* Next/previous *unresolved* flagged word after the one in hand, by
   * document position -- the same rule as remediate.ReviewPage.step_from.
   * Positional, not membership-based: correcting a word takes it out of the
   * flagged set immediately, so "where am I" cannot be answered by looking
   * the current word up in the list. */
  const step = (delta) => {
    if (!page || !page.words.length) return;
    const pool = page.words.filter(w => w.flagged && !pending.has(w.i));
    if (!pool.length) { say('No flagged words left on this page.', 'ok'); return; }
    const here = selected ? selected.i : (delta > 0 ? -1 : page.words.length);
    const later = pool.filter(w => w.i > here);
    const earlier = pool.filter(w => w.i < here);
    let next;
    if (delta > 0) next = later.length ? later[0] : pool[0];
    else next = earlier.length ? earlier[earlier.length - 1] : pool[pool.length - 1];
    const wrapped = delta > 0 ? !later.length : !earlier.length;
    select(next);
    if (wrapped) say('Wrapped around to the ' + (delta > 0 ? 'first' : 'last') + ' flagged word.');
  };

  const commitEdit = (advance) => {
    if (!selected) return;
    const text = edText.value.trim();
    if (!text) { say('Empty text. Use Delete to remove a hallucinated word.', 'err'); return; }
    if (text === selected.text) {
      // Unchanged text means "the OCR was right, the confidence was just
      // low" -- the same claim as an edit, recorded as a confirmation so
      // the count of words a human actually *changed* stays honest.
      queue({ op: 'verify', index: selected.i });
      say(`Confirmed “${text}”.`);
    } else {
      queue({ op: 'edit', index: selected.i, text });
      say(`Queued “${selected.text}” → “${text}”.`);
    }
    if (advance) step(1); else select(selected);
  };

  $('ed-commit').addEventListener('click', () => commitEdit(true));
  $('ed-confirm').addEventListener('click', () => {
    if (!selected) return;
    queue({ op: 'verify', index: selected.i });
    say(`Confirmed “${selected.text}”.`);
    step(1);
  });
  $('ed-delete').addEventListener('click', () => {
    if (!selected) return;
    queue({ op: 'delete', index: selected.i });
    say(`Queued deletion of “${selected.text}”.`);
    step(1);
  });
  $('ed-cancel').addEventListener('click', () => select(null));
  edText.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); commitEdit(true); }
    else if (ev.key === 'Escape') { ev.preventDefault(); select(null); }
  });

  const glyphs = $('glyphs');
  for (const g of GLYPHS) {
    glyphs.append(el('button', {
      type: 'button', text: g, title: `Insert ${g}`,
      onclick: () => {
        const at = edText.selectionStart ?? edText.value.length;
        edText.value = edText.value.slice(0, at) + g + edText.value.slice(edText.selectionEnd ?? at);
        edText.focus();
        edText.setSelectionRange(at + g.length, at + g.length);
      },
    }));
  }

  /* ---- add-word mode ---- */
  const setAddMode = (on) => {
    addMode = on;
    $('add-mode').setAttribute('aria-pressed', String(on));
    stage.classList.toggle('adding', on);
    if (!on) { pendingBox = null; draw = null; $('addbar').hidden = true; drawOverlay(); }
    else { select(null); say('Drag a box around the word the OCR missed.'); }
  };
  $('add-mode').addEventListener('click', () => setAddMode(!addMode));
  $('add-cancel').addEventListener('click', () => { pendingBox = null; $('addbar').hidden = true; drawOverlay(); });
  $('add-commit').addEventListener('click', () => {
    const text = $('add-text').value.trim();
    if (!pendingBox) { say('Draw the box first.', 'err'); return; }
    if (!text) { say('Type the word to add.', 'err'); return; }
    adds.push({ op: 'add', box: pendingBox, text });
    say(`Queued new word “${text}”.`);
    pendingBox = null;
    $('add-text').value = '';
    $('addbar').hidden = true;
    refreshPending();
    drawOverlay();
  });
  $('add-text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); $('add-commit').click(); }
  });

  stage.addEventListener('mousedown', (ev) => {
    if (!addMode || !page || ev.button !== 0) return;
    ev.preventDefault();
    const p = toImage(ev);
    draw = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  });
  window.addEventListener('mousemove', (ev) => {
    if (!draw) return;
    const p = toImage(ev);
    draw.x1 = p.x; draw.y1 = p.y;
    drawOverlay();
  });
  window.addEventListener('mouseup', () => {
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
  });

  /* ---- clear page ---- */
  $('clear-page').addEventListener('click', () => {
    if (clearPending) {
      clearPending = false;
      say('Clear cancelled.');
    } else {
      if (pending.size) { say('Save or discard the queued word corrections first — clear cannot be combined with them.', 'err'); return; }
      if (!confirm(`Remove every word on ${stem}?\n\nFor pages whose OCR is wholly hallucinated — covers, photographs, blank pages where texture read as text. The page skeleton stays, so you can add back any words that were real.`)) return;
      clearPending = true;
      select(null);
      say(`${page.words.length} word(s) queued for removal. Save to apply.`, 'err');
    }
    $('clear-page').setAttribute('aria-pressed', String(clearPending));
    refreshPending();
    drawOverlay();
  });

  /* ---- zoom ---- */
  const setZoom = (z) => { zoom = z; applyScale(); drawOverlay(); };
  $('zoom-in').addEventListener('click', () => {
    const cur = scale();
    setZoom(ZOOMS.find(z => z > cur + 0.001) ?? ZOOMS[ZOOMS.length - 1]);
  });
  $('zoom-out').addEventListener('click', () => {
    const cur = scale();
    setZoom([...ZOOMS].reverse().find(z => z < cur - 0.001) ?? ZOOMS[0]);
  });
  $('zoom-fit').addEventListener('click', () => setZoom(null));
  window.addEventListener('resize', () => { if (zoom === null) { applyScale(); drawOverlay(); } });

  /* ---- save ---- */
  const save = async (force) => {
    if (!dirty()) { say('Nothing to save.'); return; }
    const batch = clearPending
      ? [{ op: 'clear' }, ...adds]
      : [...pending.values(), ...adds];
    $('save').disabled = true;
    say('Saving…');
    try {
      const res = await postJSON(pageUrl(refId, stem), { token: page.token, ops: batch, force: !!force });
      pending.clear(); adds.length = 0; clearPending = false;
      $('clear-page').setAttribute('aria-pressed', 'false');
      selected = null;
      editor.hidden = true;
      page = res.page || page;
      if (res.page) { applyScale(); }
      drawOverlay();
      refreshPending();
      say(res.report.summary + (res.txt ? ' Transcript regenerated.' : ''), 'ok');
      loadSidebar();          // PC and reviewed-state have moved
    } catch (e) {
      $('save').disabled = false;
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
  $('prev-flag').addEventListener('click', () => step(-1));
  $('next-flag').addEventListener('click', () => step(1));

  /* ---- keyboard ---- */
  const onKey = (ev) => {
    if (ev.key === 's' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save(false); return; }
    const typing = ev.target instanceof HTMLInputElement || ev.target instanceof HTMLTextAreaElement;
    if (typing) return;
    if (ev.key === 'n') { ev.preventDefault(); step(1); }
    else if (ev.key === 'p') { ev.preventDefault(); step(-1); }
    else if (ev.key === 'a') { ev.preventDefault(); setAddMode(!addMode); }
    else if (ev.key === 'Escape') { select(null); if (addMode) setAddMode(false); }
  };
  document.addEventListener('keydown', onKey);
  // Hash navigation replaces #view wholesale; drop the listener with it so
  // handlers for a page nobody is looking at do not accumulate.
  const observer = new MutationObserver(() => {
    if (!document.body.contains(status)) {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('beforeunload', onLeave);
      observer.disconnect();
    }
  });
  observer.observe(view, { childList: true });

  const onLeave = (ev) => { if (dirty()) { ev.preventDefault(); ev.returnValue = ''; } };
  window.addEventListener('beforeunload', onLeave);

  /* ---- sidebar ---- */
  const loadSidebar = async () => {
    try {
      const data = await api(objUrl(refId));
      objectPages = data.pages;
    } catch { return; }
    const list = $('side-pages');
    const worst = $('side-worst').checked;
    list.replaceChildren(...pageSort(objectPages, worst).map(p => el('li', {
      'aria-current': p.stem === stem ? 'true' : null,
      title: `${p.status}${p.pc === null ? '' : ` · PC ${fmt(p.pc)}`}${p.reviewed ? ' · reviewed' : ''}`,
      onclick: () => {
        if (dirty() && !confirm('You have unsaved corrections on this page. Leave anyway?')) return;
        location.hash = `#/obj/${enc(refId)}/${enc(p.stem)}`;
      },
    },
      el('span', { class: 'dot' + (p.status === 'failed' ? ' failed' : p.reviewed ? ' reviewed' : '') }),
      el('span', { class: 'mono', text: p.stem }),
      el('span', { class: 'pc', text: p.pc === null ? p.status : fmt(p.pc) }),
    )));
  };
  $('side-ref').textContent = refId;
  $('side-worst').addEventListener('change', loadSidebar);

  /* ---- go ---- */
  say('Loading page…');
  loadSidebar();
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
  const flaggedCount = page.flagged.length;
  say(`${page.words.length} word(s), ${flaggedCount} flagged below WC ${CONFIG.lowWc}`
      + (page.pc ? ` · page PC ${page.pc}` : '')
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

(async function main() {
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
