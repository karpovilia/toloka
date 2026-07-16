"use strict";
/* Toloka — ручная адъюдикация конфликтов разметки нескольких агентов.
   Вход: config.json -> data/event_types.json + data/conflicts.json (+ data/traces для контекста).
   Конфликт-сайт: {item_id, slice, model, benchmark, question_id, domain, cell, seg_id, segs,
     agents_present:[...], per_agent:{agent:[types]}, quotes:{agent:[quote]}, priority,
     context_window:[{seg_id,text}], trace_file, n_segments}.
   Вердикт человека = МНОЖЕСТВО типов события (мультиселект) | ["∅"] (нет события) | ["unclear"].
   Разметка в localStorage (tv_annot::<annotator>); сохранение: файлом или коммитом в GitHub. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const AGENT_COLOR = { regex: "#f76b15", claude: "#d97757", deepseek: "#4c8dff", qwen: "#8e4ec6" };
const AGENT_LABEL = { regex: "regex", claude: "Claude", deepseek: "DeepSeek", qwen: "Qwen" };
const PRIO_COLOR = { 4: "#e5484d", 3: "#ffb224", 2: "#4c8dff", 1: "#8b93a5" };
const PALETTE = ["#e5484d", "#30a46c", "#8e4ec6", "#0091ff", "#f76b15", "#ffb224", "#12a594", "#e93d82", "#6e56cf", "#46a758", "#d6409f", "#5b5bd6"];
const NONE = "∅", UNCLEAR = "unclear";
const CHUNK = 20, EDGE = 140; // догрузка контекста по скроллу

// Провенанс типов события. ReasonOps (Gandhi/Stanford) описывает 7 дискурсивных операторов;
// наши типы события — отдельная таксономия. Ниже — семантическое соответствие наших типов
// операторам ReasonOps: помеченные считаются «стенфордскими» (рамка-пилюля), остальные — наши
// (прямоугольная пунктирная рамка). РЕДАКТИРУЙ этот словарь, если разбивка другая.
const REASONOPS_OP = {
  backtrack: "BACKTRACKING", verify: "CONSTRAINING", branch: "HYPOTHESIZING",
  subgoal_done: "INFERRING", decomposition: "INITIATING", evidence_merge: "GROUNDING",
  internal_use: "QUALIFYING",
};
const isReasonOps = (t) => Object.prototype.hasOwnProperty.call(REASONOPS_OP, t);
const provClass = (t) => (t === NONE || t === UNCLEAR) ? "" : (isReasonOps(t) ? " ro" : " ours");
const provTitle = (t) => isReasonOps(t) ? "ReasonOps: " + REASONOPS_OP[t] : (t === NONE || t === UNCLEAR ? "" : "наш тип");

// карта reasoning: цвета операторных спанов и глифы развилок
const OP_COLOR = { SETUP: "#6e56cf", DERIVING: "#4c8dff", EXPLORING: "#f76b15", VERIFYING: "#12a594", REVISING: "#e5484d", CONCLUDING: "#46a758", GROUNDING: "#8e4ec6" };
const FORK = { branch: "◇", backtrack: "◄", failed_attempt: "✗" };
const isFork = (t) => Object.prototype.hasOwnProperty.call(FORK, t);
const typeGlyphColor = (t) => ({ verify: "#12a594", subgoal_done: "#46a758", commit: "#ffb224" }[t] || "#8b93a5");

const S = {
  model: null, items: [], filtered: [], idx: 0,
  annotatorId: localStorage.getItem("tv_last_annotator") || "ki",
  myAnnot: {}, pool: [], cfg: null,
  traces: {},                 // trace_file -> {segments, events, spans, lam, agents} | null
  view: null,                 // {item, lo, hi, minId, maxId} текущее окно контекста
  scrolling: false,
  mapMeta: null,              // trace_maps_meta.json (hawkes params, lam_max, operators)
  wholeTrace: false,          // режим «вся трасса»
};

const typeColor = (id) => { if (id === NONE) return "#8b93a5"; if (id === UNCLEAR) return "#ffb224"; let h = 0; for (const c of (id || "")) h = (h * 31 + c.charCodeAt(0)) | 0; return PALETTE[Math.abs(h) % PALETTE.length]; };
function contrast(hex) { const h = hex.replace("#", ""); const r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16); return (r * 299 + g * 587 + b * 114) / 1000 > 140 ? "#0b0d12" : "#fff"; }
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3200); }
function esc(s) { return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

/* ---------- вердикт как множество ---------- */
function vset(r) { if (!r) return []; return Array.isArray(r.verdict) ? r.verdict : (r.verdict ? [r.verdict] : []); }
const recDone = (r) => vset(r).length > 0;
function agentState(types, vs) {          // "match" | "miss" | null(нейтрально)
  if (!vs.length || vs.includes(UNCLEAR)) return null;
  if (vs.includes(NONE)) return (types.length === 1 && types[0] === NONE) ? "match" : "miss";
  return types.some(t => vs.includes(t)) ? "match" : "miss";
}

/* ---------- storage ---------- */
const annotKey = (id) => "tv_annot::" + id;
function loadMyAnnot() { try { S.myAnnot = JSON.parse(localStorage.getItem(annotKey(S.annotatorId))) || {}; } catch { S.myAnnot = {}; } }
function saveMyAnnot() { localStorage.setItem(annotKey(S.annotatorId), JSON.stringify(S.myAnnot)); }
function recFor(id, create) { if (!S.myAnnot[id] && create) S.myAnnot[id] = { verdict: [], notes: "", updated: null }; return S.myAnnot[id]; }

/* ---------- loading ---------- */
async function tryFetch(p) { try { const r = await fetch(p); if (!r.ok) throw 0; return await r.text(); } catch { return null; } }

function setModel(raw) {
  const typesById = {}, byDomain = {};
  for (const [dom, dv] of Object.entries(raw.domains || {})) {
    byDomain[dom] = [];
    for (const [id, t] of Object.entries(dv.types || {})) {
      typesById[id] = typesById[id] || { id, color: typeColor(id), definition: t.definition || "", domains: [] };
      typesById[id].domains.push(dom); byDomain[dom].push(id);
    }
  }
  S.model = { typesById, byDomain, raw };
  const sel = $("#fType"); sel.innerHTML = '<option value="">любой тип события</option>';
  for (const id of Object.keys(typesById).sort()) { const o = el("option", null, id); o.value = id; sel.appendChild(o); }
}

function setData(raw) {
  if (!Array.isArray(raw) || !raw.length || raw[0].item_id === undefined) { toast("conflicts.json: не тот формат"); return; }
  S.items = raw;
  fillSelect("#fBench", uniq(raw.map(x => x.benchmark)), "все бенчи");
  const agents = uniq(raw.flatMap(x => x.agents_present));
  const fa = $("#fAgent"); fa.innerHTML = '<option value="">любой агент участвует</option>';
  for (const a of agents) { const o = el("option", null, AGENT_LABEL[a] || a); o.value = a; fa.appendChild(o); }
  S.idx = 0; applyFilters();
  toast(`конфликтов загружено: ${raw.length}`);
}
function uniq(a) { return [...new Set(a.filter(Boolean))].sort(); }
function fillSelect(sel, vals, allLabel) { const s = $(sel); s.innerHTML = ""; const o0 = el("option", null, allLabel); o0.value = ""; s.appendChild(o0); for (const v of vals) { const o = el("option", null, v); o.value = v; s.appendChild(o); } }

async function loadTrace(it) {
  if (!it.trace_file || !S.cfg || !S.cfg.traces_dir) return null;
  if (S.traces[it.trace_file] === undefined) {
    const t = await tryFetch(S.cfg.traces_dir + "/" + it.trace_file);
    S.traces[it.trace_file] = t ? JSON.parse(t) : null;
  }
  return S.traces[it.trace_file];
}

/* ---------- filters + list ---------- */
function siteTypes(it) { return uniq(Object.values(it.per_agent).flat().filter(t => t !== NONE)); }

function applyFilters() {
  const q = $("#textSearch").value.trim().toLowerCase();
  const fs = $("#fSlice").value, fb = $("#fBench").value, fp = $("#fPrio").value;
  const ft = $("#fType").value, fm = $("#fMine").value, fa = $("#fAgent").value;
  S.filtered = [];
  S.items.forEach((it, i) => {
    if (fs && it.slice !== fs) return;
    if (fb && it.benchmark !== fb) return;
    if (fp && String(it.priority) !== fp) return;
    if (fa && !it.agents_present.includes(fa)) return;
    if (ft && !siteTypes(it).includes(ft)) return;
    const rec = S.myAnnot[it.item_id];
    if (fm === "ann" && !recDone(rec)) return;
    if (fm === "unann" && recDone(rec)) return;
    if (q) {
      const hay = [...Object.values(it.quotes || {}).flat(), ...siteTypes(it), rec?.notes || ""].join(" ").toLowerCase();
      if (!hay.includes(q)) return;
    }
    S.filtered.push(i);
  });
  $("#filterCount").textContent = `${S.filtered.length} из ${S.items.length}`;
  if (S.idx >= S.filtered.length) S.idx = 0;
  renderList(); renderItem(); renderProgress();
}

function renderProgress() {
  const done = Object.values(S.myAnnot).filter(recDone).length;
  $("#progress").textContent = `размечено: ${done} / ${S.items.length}`;
}

function renderList() {
  const ul = $("#exampleList"); ul.innerHTML = "";
  const cap = 600, n = Math.min(S.filtered.length, cap);
  for (let pos = 0; pos < n; pos++) {
    const it = S.items[S.filtered[pos]];
    const li = el("li"); if (pos === S.idx) li.classList.add("active");
    li.appendChild(el("div", "li-id", `${it.slice} · ${it.benchmark} · s${it.seg_id}`));
    const meta = el("div", "li-meta");
    const pb = el("span", "badge prio", "P" + it.priority); pb.style.background = PRIO_COLOR[it.priority]; pb.style.color = contrast(PRIO_COLOR[it.priority]); meta.appendChild(pb);
    for (const a of it.agents_present) { const b = el("span", "badge", (it.per_agent[a] || [NONE]).join(",")); b.style.borderColor = AGENT_COLOR[a]; b.title = AGENT_LABEL[a] || a; meta.appendChild(b); }
    const rec = S.myAnnot[it.item_id]; if (recDone(rec)) meta.appendChild(el("span", "badge done", "✓ " + vset(rec).join("+")));
    li.appendChild(meta);
    li.onclick = () => { S.idx = pos; renderList(); renderItem(); };
    ul.appendChild(li);
  }
  if (S.filtered.length > n) ul.appendChild(el("li", "muted", `…ещё ${S.filtered.length - n} (сузь фильтр)`));
}

/* ---------- render current item ---------- */
const currentItem = () => S.filtered.length ? S.items[S.filtered[S.idx]] : null;

function highlight(text, it) {
  let html = esc(text); const marks = [];
  for (const a of it.agents_present) for (const qu of (it.quotes?.[a] || [])) if (qu) marks.push([qu, AGENT_COLOR[a]]);
  for (const [needle, col] of marks) { const e = esc(needle); if (e && html.includes(e)) html = html.split(e).join(`<mark style="background:${col};color:${contrast(col)}">${e}</mark>`); }
  return html;
}
function traceMap(it) {
  const tr = S.traces[it.trace_file];
  if (!tr || !tr.segments) return null;
  if (tr._map) return tr._map;
  const op = new Map();
  for (const s of (tr.spans || [])) for (let i = s.a; i < s.b; i++) op.set(i, s.op);
  const ev = new Map();
  for (const e of (tr.events || [])) {
    if (!ev.has(e.s)) ev.set(e.s, { types: new Set(), agents: new Set(), fork: false });
    const o = ev.get(e.s); o.types.add(e.t); o.agents.add(e.a); if (isFork(e.t)) o.fork = true;
  }
  tr._map = { op, ev, lam: tr.lam || [] };
  return tr._map;
}

function gutter(id, m) {
  const g = el("div", "gutter");
  const opName = m ? m.op.get(id) : null;
  const band = el("div", "opband"); band.style.background = opName ? OP_COLOR[opName] || "#333" : "transparent";
  if (opName) band.title = "оператор: " + opName;
  g.appendChild(band);
  const lamMax = (S.mapMeta && S.mapMeta.lam_max) || 1;
  const v = m && m.lam.length > id ? m.lam[id] : 0;
  const bar = el("div", "lam"); const frac = Math.max(0, Math.min(1, v / lamMax));
  bar.style.width = (2 + frac * 22).toFixed(1) + "px";
  bar.style.opacity = (0.25 + 0.75 * frac).toFixed(2);
  bar.title = "λ(Hawkes) = " + (v || 0).toFixed(2);
  g.appendChild(bar);
  const o = m ? m.ev.get(id) : null;
  const gl = el("div", "glyphs");
  if (o) {
    for (const t of o.types) {
      const sp = el("span", "glyph", isFork(t) ? FORK[t] : "•");
      sp.style.color = isFork(t) ? typeColor(t) : typeGlyphColor(t);
      sp.title = t + " · " + [...o.agents].join(",");
      gl.appendChild(sp);
    }
  }
  g.appendChild(gl);
  return g;
}

function segRow(id, text, it, focusSet, m) {
  const focus = focusSet.has(id);
  const row = el("div", "seg" + (focus ? " focus" : "") + (id === S.cursorSeg ? " cursor" : "")); row.dataset.segId = id;
  const g = gutter(id, m);
  if (m) { g.style.cursor = "pointer"; g.title = "клик — распределение λ по типам + перейти сюда"; g.onclick = (e) => { e.stopPropagation(); jumpToSeg(it, id); drill(it, id, e); }; }
  row.appendChild(g);
  row.appendChild(el("div", "sid", String(id)));
  const txt = el("div", "stext");
  if (focus) txt.innerHTML = highlight(text, it); else txt.textContent = text;
  row.appendChild(txt); return row;
}
function segSource(it) { const tr = S.traces[it.trace_file]; return (tr && tr.segments) ? tr.segments : (it.context_window || []); }

/* ---------- drill-down: распределение λ по типам в точке ---------- */
function lamByType(it, seg) {
  const P = S.mapMeta && S.mapMeta.hawkes_by_type; const tr = S.traces[it.trace_file];
  if (!P || !tr) return null;
  const byT = {};
  for (const e of (tr.events || [])) (byT[e.t] = byT[e.t] || []).push(e.s);
  const out = [];
  for (const t in P) {
    const p = P[t]; let exc = 0;
    for (const ti of (byT[t] || [])) if (ti < seg) exc += Math.exp(-p.beta * (seg - ti));
    out.push({ type: t, lam: p.mu + p.alpha * p.beta * exc, hasEv: (byT[t] || []).some(x => x <= seg) });
  }
  out.sort((a, b) => b.lam - a.lam);
  return out;
}
function closeDrill() { const d = $("#drillpop"); if (d) d.remove(); }
function drill(it, seg, evt) {
  closeDrill();
  const dist = lamByType(it, seg); if (!dist) return;
  const tot = dist.reduce((a, b) => a + b.lam, 0);
  const max = Math.max(...dist.map(d => d.lam), 1e-6);
  const pop = el("div", "drillpop"); pop.id = "drillpop";
  pop.appendChild(el("div", "dh", `сегмент ${seg} · Σλ = ${tot.toFixed(2)} · распределение по типам`));
  for (const d of dist) {
    if (d.lam < 1e-3 && !d.hasEv) continue;
    const row = el("div", "drow");
    const nm = el("span", "dtype" + provClass(d.type), d.type); const c = typeColor(d.type); nm.style.color = c;
    row.appendChild(nm);
    const bw = el("span", "dbarwrap"); const bar = el("span", "dbar");
    bar.style.width = (3 + 92 * d.lam / max).toFixed(0) + "px"; bar.style.background = c;
    bw.appendChild(bar); row.appendChild(bw);
    row.appendChild(el("span", "dval", d.lam.toFixed(3)));
    pop.appendChild(row);
  }
  document.body.appendChild(pop);
  const x = Math.min((evt ? evt.clientX : 200) + 10, window.innerWidth - 250);
  const y = Math.min((evt ? evt.clientY : 120) + 4, window.innerHeight - pop.offsetHeight - 12);
  pop.style.left = Math.max(6, x) + "px"; pop.style.top = Math.max(6, y) + "px";
}

/* ---------- навигация по событиям внутри трассы ---------- */
function eventSegs(it) { const tr = S.traces[it.trace_file]; return (tr && tr.events) ? [...new Set(tr.events.map(e => e.s))].sort((a, b) => a - b) : []; }
function jumpToSeg(it, seg) {
  if (!S.wholeTrace && S.view && (seg < S.view.lo || seg > S.view.hi)) {
    S.view.lo = Math.max(S.view.minId, Math.min(S.view.lo, seg - 3));
    S.view.hi = Math.min(S.view.maxId, Math.max(S.view.hi, seg + 3));
    renderContext(it, false);
  }
  const row = $('#traceBody .seg[data-seg-id="' + seg + '"]');
  if (row) {
    $$('#traceBody .seg.cursor').forEach(r => r.classList.remove("cursor"));
    row.classList.add("cursor"); if (row.scrollIntoView) row.scrollIntoView({ block: "center" });
  }
  S.cursorSeg = seg;
}
function gotoEvent(it, dir) {
  const segs = eventSegs(it); if (!segs.length) { toast("на трассе нет событий"); return; }
  const cur = S.cursorSeg == null ? it.seg_id : S.cursorSeg;
  let idx = segs.indexOf(cur);
  if (idx < 0) {
    idx = dir > 0 ? segs.findIndex(s => s > cur) : [...segs].reverse().findIndex(s => s < cur);
    if (dir < 0 && idx >= 0) idx = segs.length - 1 - idx;
    if (idx < 0) idx = dir > 0 ? 0 : segs.length - 1;
  } else idx = (idx + dir + segs.length) % segs.length;
  jumpToSeg(it, segs[idx]);
}

function renderContext(it, fresh) {
  const body = $("#traceBody");
  const segs = segSource(it);
  const byId = new Map(segs.map(s => [s.seg_id, s.text]));
  const ids = segs.map(s => s.seg_id);
  const minId = ids.length ? Math.min(...ids) : it.seg_id, maxId = ids.length ? Math.max(...ids) : it.seg_id;
  const radius = parseInt($("#ctxRadius").value) || 12;
  const whole = S.wholeTrace && S.traces[it.trace_file];
  if (fresh || !S.view || S.view.item !== it.item_id) {
    S.view = whole
      ? { item: it.item_id, lo: minId, hi: maxId, minId, maxId }
      : { item: it.item_id, lo: Math.max(minId, it.seg_id - radius), hi: Math.min(maxId, it.seg_id + radius), minId, maxId };
  } else { S.view.minId = minId; S.view.maxId = maxId; if (whole) { S.view.lo = minId; S.view.hi = maxId; } }
  const focusSet = new Set(it.segs || [it.seg_id]);
  const m = traceMap(it);
  body.innerHTML = "";
  if (!whole && S.view.lo > minId) body.appendChild(el("div", "moretop", "↑ листай вверх — ещё контекст"));
  for (let id = S.view.lo; id <= S.view.hi; id++) if (byId.has(id)) body.appendChild(segRow(id, byId.get(id), it, focusSet, m));
  if (!whole && S.view.hi < maxId) body.appendChild(el("div", "morebot", "↓ листай вниз — ещё контекст"));
  if (fresh) { const f = $("#traceBody .focus"); if (f && f.scrollIntoView) f.scrollIntoView({ block: "center" }); }
}

function extendUp(it) {
  const segs = segSource(it), byId = new Map(segs.map(s => [s.seg_id, s.text])), m = traceMap(it);
  const focusSet = new Set(it.segs || [it.seg_id]); const body = $("#traceBody");
  const newLo = Math.max(S.view.minId, S.view.lo - CHUNK); if (newLo >= S.view.lo) return;
  const before = body.scrollHeight, top = $("#traceBody .moretop");
  const frag = document.createDocumentFragment();
  for (let id = newLo; id < S.view.lo; id++) if (byId.has(id)) frag.appendChild(segRow(id, byId.get(id), it, focusSet, m));
  body.insertBefore(frag, top ? top.nextSibling : body.firstChild);
  S.view.lo = newLo;
  if (top && newLo <= S.view.minId) top.remove();
  body.scrollTop += body.scrollHeight - before;
}
function extendDown(it) {
  const segs = segSource(it), byId = new Map(segs.map(s => [s.seg_id, s.text])), m = traceMap(it);
  const focusSet = new Set(it.segs || [it.seg_id]); const body = $("#traceBody");
  const newHi = Math.min(S.view.maxId, S.view.hi + CHUNK); if (newHi <= S.view.hi) return;
  const bot = $("#traceBody .morebot"); const frag = document.createDocumentFragment();
  for (let id = S.view.hi + 1; id <= newHi; id++) if (byId.has(id)) frag.appendChild(segRow(id, byId.get(id), it, focusSet, m));
  body.insertBefore(frag, bot);
  S.view.hi = newHi;
  if (bot && newHi >= S.view.maxId) bot.remove();
}
function onCtxScroll() {
  const it = currentItem(); if (!it || !S.view || S.view.item !== it.item_id) return;
  const body = $("#traceBody");
  if (body.scrollTop < EDGE && S.view.lo > S.view.minId) extendUp(it);
  if (body.scrollHeight - body.scrollTop - body.clientHeight < EDGE && S.view.hi < S.view.maxId) extendDown(it);
}

function renderItem() {
  const it = currentItem();
  $("#posLabel").textContent = S.filtered.length ? `${S.idx + 1}/${S.filtered.length}` : "–/–";
  if (!it) { $("#traceBody").innerHTML = ""; $("#qLabel").textContent = ""; $("#agents").innerHTML = ""; $("#verdict").innerHTML = ""; $("#myDecisions").innerHTML = ""; $("#focusSeg").textContent = "–"; $("#focusText").textContent = ""; return; }
  $("#qLabel").textContent = `срез ${it.slice} · ${it.model} · ${it.benchmark} · qid ${it.question_id} · domain ${it.domain} · P${it.priority}` + (it.n_segments ? ` · ${it.n_segments} сегм.` : "");
  renderContext(it, true);
  loadTrace(it).then(tr => { if (tr && currentItem() === it) renderContext(it, true); });
  $("#focusSeg").textContent = `s${it.seg_id}` + (it.segs && it.segs.length > 1 ? ` (сегм. ${it.segs.join(",")})` : "");
  const focusSet = new Set(it.segs || [it.seg_id]);
  $("#focusText").textContent = (segSource(it)).filter(s => focusSet.has(s.seg_id)).map(s => s.text).join(" ⏎ ");
  renderAgents(it); renderVerdict(it); renderMy(it); updateHash();
}

function renderAgents(it) {
  const box = $("#agents"); box.innerHTML = "";
  const vs = vset(S.myAnnot[it.item_id]);
  for (const a of it.agents_present) {
    const types = it.per_agent[a] || [NONE];
    const st = agentState(types, vs);
    const card = el("div", "agent" + (st ? " " + st : ""));
    const head = el("div", "ahead");
    const nm = el("span", "aname", AGENT_LABEL[a] || a); nm.style.color = AGENT_COLOR[a]; head.appendChild(nm);
    const tw = el("span", "atypes");
    for (const t of types) { const tt = el("span", "atype" + provClass(t), t); tt.title = provTitle(t); const c = typeColor(t); tt.style.background = c; tt.style.color = contrast(c); tw.appendChild(tt); }
    head.appendChild(tw);
    if (st) head.appendChild(el("span", "averdict", st === "match" ? "✅" : "❌"));
    card.appendChild(head);
    const qu = (it.quotes?.[a] || []).filter(Boolean)[0];
    if (qu) { const d = el("div", "aquote"); d.innerHTML = "«<b>" + esc(qu) + "</b>»"; card.appendChild(d); }
    box.appendChild(card);
  }
}

function candidateTypes(it) {
  const domTypes = S.model?.byDomain?.[it.domain] || Object.keys(S.model?.typesById || {});
  return { asserted: siteTypes(it), all: domTypes };
}

function renderVerdict(it) {
  const box = $("#verdict"); box.innerHTML = "";
  box.appendChild(el("div", "vhead", "истина на сайте: что здесь на самом деле?"));
  box.appendChild(el("div", "vhint", "можно выбрать несколько типов (мультиселект); ∅/неясно — исключающие"));
  const vs = vset(S.myAnnot[it.item_id]);
  const { asserted, all } = candidateTypes(it);

  const cand = el("div", "vbtns");
  asserted.forEach((t, i) => {
    const b = el("button", "vbtn cand" + provClass(t) + (vs.includes(t) ? " sel" : ""), (i < 9 ? (i + 1) + " · " : "") + t);
    b.title = provTitle(t);
    b.onclick = () => toggleVerdict(it, t); cand.appendChild(b);
  });
  box.appendChild(cand);

  const sp = el("div", "vbtns");
  const bn = el("button", "vbtn none" + (vs.includes(NONE) ? " sel" : ""), "0 · " + NONE + " нет события");
  bn.onclick = () => toggleVerdict(it, NONE); sp.appendChild(bn);
  const bu = el("button", "vbtn unclear" + (vs.includes(UNCLEAR) ? " sel" : ""), "u · неясно");
  bu.onclick = () => toggleVerdict(it, UNCLEAR); sp.appendChild(bu);
  box.appendChild(sp);

  box.appendChild(el("div", "vhint", "добавить другой тип из домена " + it.domain + ":"));
  const opt = (txt, val) => { const o = el("option", null, txt); o.value = val; return o; };
  const csel = el("select"); csel.appendChild(opt("+ добавить тип", ""));
  for (const id of all) if (!asserted.includes(id)) csel.appendChild(opt(id + (vs.includes(id) ? " ✓" : ""), id));
  csel.onchange = () => { if (csel.value) toggleVerdict(it, csel.value); };
  box.appendChild(csel);

  const note = el("textarea"); note.rows = 2; note.placeholder = "заметка…"; note.value = (S.myAnnot[it.item_id]?.notes) || "";
  note.onchange = () => { const r = recFor(it.item_id, true); r.notes = note.value; touch(r); };
  box.appendChild(note);
}

function toggleVerdict(it, v) {
  const r = recFor(it.item_id, true);
  let cur = vset(r).slice();
  if (v === NONE || v === UNCLEAR) {
    cur = (cur.length === 1 && cur[0] === v) ? [] : [v];       // исключающий тумблер
  } else {
    cur = cur.filter(x => x !== NONE && x !== UNCLEAR);          // типы несовместимы с ∅/неясно
    cur = cur.includes(v) ? cur.filter(x => x !== v) : [...cur, v];
  }
  r.verdict = cur;
  touch(r); renderList(); renderAgents(it); renderVerdict(it); renderMy(it); renderProgress();
}
function touch(r) { r.updated = new Date().toISOString(); saveMyAnnot(); }

function renderMy(it) {
  const box = $("#myDecisions"); box.innerHTML = "";
  const r = S.myAnnot[it.item_id];
  if (recDone(r) || r?.notes) { const d = el("div", "mydec"); d.textContent = `${S.annotatorId}: ${vset(r).join("+") || "—"}${r.notes ? " · " + r.notes : ""}`; box.appendChild(d); }
  for (const p of S.pool) { const pr = p.annotations?.[it.item_id]; if (recDone(pr)) { const d = el("div", "mydec"); d.style.borderColor = "#8e4ec6"; d.textContent = `👤 ${p.annotator_id}: ${vset(pr).join("+")}${pr.notes ? " · " + pr.notes : ""}`; box.appendChild(d); } }
}

/* ---------- deep-linking через URL-хэш ---------- */
function parseHash() {
  const p = {};
  for (const kv of location.hash.replace(/^#/, "").split("&")) {
    const i = kv.indexOf("="); if (i > 0) p[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
  }
  return p;
}
function updateHash() {
  const it = currentItem(); if (!it) return;
  let h = "#item=" + encodeURIComponent(it.item_id);
  if (S.wholeTrace) h += "&whole=1";
  if (h !== S._lastHash) { S._lastHash = h; try { history.replaceState(null, "", h); } catch { location.hash = h; } }
}
function selectItemById(id, opts = {}) {
  const idx = S.items.findIndex(x => x.item_id === id);
  if (idx < 0) { toast("сайт из ссылки не найден в наборе"); return false; }
  ["fSlice", "fBench", "fPrio", "fType", "fMine", "fAgent"].forEach(s => { const e = $("#" + s); if (e) e.value = ""; });
  $("#textSearch").value = "";
  if (opts.whole) { S.wholeTrace = true; $("#wholeBtn").classList.add("on"); }
  applyFilters();
  const pos = S.filtered.indexOf(idx);
  if (pos >= 0) { S.idx = pos; renderList(); renderItem(); }
  return true;
}
function applyHash(p) {
  p = p || parseHash();
  if (p.item) return selectItemById(p.item, { whole: p.whole === "1" });
  if (p.trace) {
    const it = S.items.find(x => x.trace_file === p.trace || (x.cell + "|" + x.question_id) === p.trace);
    if (it) return selectItemById(it.item_id, { whole: true });
    toast("трасса из ссылки не найдена");
  }
  return false;
}
function shareLink() {
  updateHash();
  const url = location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(() => toast("ссылка скопирована"), () => toast(url));
  else toast(url);
}

/* ---------- export / import / GitHub ---------- */
function annotPayload() { return { annotator_id: S.annotatorId, exported: new Date().toISOString(), tool: "toloka", annotations: S.myAnnot }; }
function download(name, obj) { const blob = new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" }); const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href); }

function ghCfg() { try { return JSON.parse(localStorage.getItem("tv_gh") || "{}"); } catch { return {}; } }
function ghSave() {
  const c = { owner: $("#ghOwner").value.trim(), repo: $("#ghRepo").value.trim(), branch: $("#ghBranch").value.trim() || "main", path: $("#ghPath").value.trim() || "annotations", token: $("#ghToken").value.trim() };
  localStorage.setItem("tv_gh", JSON.stringify(c)); toast("настройки GitHub сохранены"); return c;
}
function b64(str) { return btoa(unescape(encodeURIComponent(str))); }
async function ghCommit() {
  const c = ghSave();
  if (!c.owner || !c.repo || !c.token) { toast("заполни owner / repo / token"); return; }
  const path = `${c.path}/annot_${S.annotatorId}.json`;
  const api = `https://api.github.com/repos/${c.owner}/${c.repo}/contents/${path.split("/").map(encodeURIComponent).join("/")}`;
  const headers = { Authorization: "Bearer " + c.token, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  let sha = null;
  try { const g = await fetch(api + "?ref=" + encodeURIComponent(c.branch), { headers }); if (g.ok) sha = (await g.json()).sha; else if (g.status === 404) sha = null; else { toast("GitHub чтение " + g.status); } }
  catch (e) { toast("сеть: " + e.message); return; }
  const done = Object.values(S.myAnnot).filter(recDone).length;
  const body = { message: `разметка ${S.annotatorId}: ${done} сайтов`, content: b64(JSON.stringify(annotPayload(), null, 1)), branch: c.branch };
  if (sha) body.sha = sha;
  try {
    const r = await fetch(api, { method: "PUT", headers, body: JSON.stringify(body) });
    if (r.ok) { const j = await r.json(); toast("сохранено в GitHub ✓ " + (j.commit?.sha || "").slice(0, 7)); }
    else { const t = await r.text(); toast("GitHub " + r.status + ": " + t.slice(0, 120)); }
  } catch (e) { toast("сеть: " + e.message); }
}

/* ---------- events ---------- */
function bind() {
  $("#annotatorId").value = S.annotatorId;
  $("#annotatorId").onchange = () => { S.annotatorId = $("#annotatorId").value.trim() || "anon"; localStorage.setItem("tv_last_annotator", S.annotatorId); loadMyAnnot(); applyFilters(); toast("аннотатор: " + S.annotatorId); };
  $("#importAnnot").onchange = ev => { for (const f of ev.target.files) { const r = new FileReader(); r.onload = () => { try { const j = JSON.parse(r.result); if (j.annotator_id === S.annotatorId) { Object.assign(S.myAnnot, j.annotations || {}); saveMyAnnot(); toast("загружена МОЯ разметка"); } else { S.pool = S.pool.filter(p => p.annotator_id !== j.annotator_id); S.pool.push(j); toast("подключена разметка: " + j.annotator_id); } applyFilters(); } catch { toast("не JSON"); } }; r.readAsText(f); } };
  $("#exportAnnot").onclick = () => download(`annot_${S.annotatorId}.json`, annotPayload());
  $("#shareBtn").onclick = shareLink;
  window.addEventListener("hashchange", () => { if (location.hash !== S._lastHash) applyHash(); });
  // GitHub-панель
  $("#ghBtn").onclick = () => $("#ghPanel").classList.toggle("hidden");
  const g = ghCfg();
  $("#ghOwner").value = g.owner || "karpovilia"; $("#ghRepo").value = g.repo || "toloka";
  $("#ghBranch").value = g.branch || "main"; $("#ghPath").value = g.path || "annotations"; $("#ghToken").value = g.token || "";
  $("#ghSaveCfg").onclick = ghSave;
  $("#ghCommit").onclick = ghCommit;
  $("#prevBtn").onclick = () => go(-1);
  $("#nextBtn").onclick = () => go(1);
  $("#toFocus").onclick = () => renderItem();
  $("#ctxRadius").onchange = () => renderItem();
  $("#wholeBtn").onclick = () => { S.wholeTrace = !S.wholeTrace; $("#wholeBtn").classList.toggle("on", S.wholeTrace); renderItem(); };
  $("#prevEv").onclick = () => { const it = currentItem(); if (it) gotoEvent(it, -1); };
  $("#nextEv").onclick = () => { const it = currentItem(); if (it) gotoEvent(it, 1); };
  $("#traceBody").addEventListener("scroll", closeDrill);
  document.addEventListener("click", closeDrill);
  $("#traceBody").addEventListener("scroll", () => { if (S.scrolling) return; S.scrolling = true; requestAnimationFrame(() => { S.scrolling = false; onCtxScroll(); }); });
  ["textSearch", "fSlice", "fBench", "fPrio", "fType", "fMine", "fAgent"].forEach(id => { const e = $("#" + id); e.oninput = e.onchange = applyFilters; });
  document.addEventListener("keydown", ev => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    const it = currentItem(); if (!it) return;
    if (ev.key === "j" || ev.key === "ArrowRight") go(1);
    else if (ev.key === "k" || ev.key === "ArrowLeft") go(-1);
    else if (ev.key === "0") toggleVerdict(it, NONE);
    else if (ev.key === "u") toggleVerdict(it, UNCLEAR);
    else if (ev.key === "n") gotoEvent(it, 1);
    else if (ev.key === "p") gotoEvent(it, -1);
    else if (/^[1-9]$/.test(ev.key)) { const t = siteTypes(it)[parseInt(ev.key) - 1]; if (t) toggleVerdict(it, t); }
  });
}
function go(d) { if (!S.filtered.length) return; S.idx = (S.idx + d + S.filtered.length) % S.filtered.length; S.cursorSeg = null; renderList(); renderItem(); }

/* ---------- init ---------- */
(async function init() {
  bind(); loadMyAnnot();
  const boot = parseHash();   // считать входящий hash ДО первого рендера (setData его перезапишет)
  const cfgTxt = await tryFetch("config.json");
  S.cfg = cfgTxt ? JSON.parse(cfgTxt) : { event_types: "data/event_types.json", conflicts: "data/conflicts.json", traces_dir: "data/traces" };
  const mm = await tryFetch(S.cfg.trace_maps_meta || "data/trace_maps_meta.json"); if (mm) try { S.mapMeta = JSON.parse(mm); } catch {}
  const m = await tryFetch(S.cfg.event_types); if (m) try { setModel(JSON.parse(m)); } catch (e) { toast("event_types: " + e.message); }
  const d = await tryFetch(S.cfg.conflicts); if (d) try { setData(JSON.parse(d)); } catch (e) { toast("conflicts: " + e.message); }
  if (!d) toast("не удалось загрузить conflicts.json");
  if (boot.item || boot.trace) applyHash(boot);   // навигация по входящей ссылке
})();
