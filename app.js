"use strict";
/* Toloka v2 — верификация СОБЫТИЙ модели на reasoning-трассе.
   Работаем трассами: слева список трасс, в центре — трасса целиком с картой,
   на каждом событии агентов инлайн-контрол: ✓ подтвердить / ✗ отклонить (FP) / другой тип.
   Кандидат события = кластер событий одного типа в окне ±1 сегмента (agents = union).
   Вход: config.json -> event_types.json + traces_index.json (+ traces_dir, trace_maps_meta).
   Разметка в localStorage (tv_annot::<annotator>), ключ = candId; обмен файлом / коммитом в GitHub. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const AGENT_COLOR = { regex: "#f76b15", claude: "#d97757", deepseek: "#4c8dff", qwen: "#8e4ec6" };
const AGENT_LABEL = { regex: "regex", claude: "Claude", deepseek: "DeepSeek", qwen: "Qwen" };
const PALETTE = ["#e5484d", "#30a46c", "#8e4ec6", "#0091ff", "#f76b15", "#ffb224", "#12a594", "#e93d82", "#6e56cf", "#46a758", "#d6409f", "#5b5bd6"];
const OP_COLOR = { SETUP: "#6e56cf", DERIVING: "#4c8dff", EXPLORING: "#f76b15", VERIFYING: "#12a594", REVISING: "#e5484d", CONCLUDING: "#46a758", GROUNDING: "#8e4ec6" };
const FORK = { branch: "◇", backtrack: "◄", failed_attempt: "✗" };
const isFork = (t) => Object.prototype.hasOwnProperty.call(FORK, t);
const REASONOPS_OP = { backtrack: "BACKTRACKING", verify: "CONSTRAINING", branch: "HYPOTHESIZING", subgoal_done: "INFERRING", decomposition: "INITIATING", evidence_merge: "GROUNDING", internal_use: "QUALIFYING" };
const isReasonOps = (t) => Object.prototype.hasOwnProperty.call(REASONOPS_OP, t);
const provClass = (t) => isReasonOps(t) ? " ro" : " ours";
const provTitle = (t) => isReasonOps(t) ? "ReasonOps: " + REASONOPS_OP[t] : "наш тип";
const CONFIRM = "✓", REJECT = "✗";
const CHUNK = 20, EDGE = 140;
// что кодирует операторный спан (из dual-label отчёта)
const OP_DESC = {
  SETUP: "постановка: переформулировка задачи, план, декомпозиция",
  DERIVING: "деривация: ровная прямая выкладка/вычисление без реверсов",
  EXPLORING: "разбор альтернатив/случаев/веток",
  VERIFYING: "устойчивая проверка/пересчёт уже полученного",
  REVISING: "восстановление после тупика/реверса — отмена и переделка",
  CONCLUDING: "финализация и фиксация ответа",
  GROUNDING: "работа с извлечёнными источниками (RAG)",
};
const opTitle = (op) => "спан " + op + (OP_DESC[op] ? " — " + OP_DESC[op] : "");
const typeDesc = (t) => (S.model && S.model.typesById[t] && S.model.typesById[t].definition) || "";
const typeTitle = (t) => provTitle(t) + (typeDesc(t) ? " — " + typeDesc(t) : "");

const S = {
  model: null, index: [], filtered: [], tidx: 0,
  curTF: null, trace: null, cands: [], candBySeg: new Map(), selSeg: null,
  myAnnot: {}, pool: [], cfg: null, mapMeta: null,
  annotatorId: localStorage.getItem("tv_last_annotator") || "ki",
  traces: {}, view: null, scrolling: false, wholeTrace: true, linkedSeg: null,
};

const typeColor = (id) => { let h = 0; for (const c of (id || "")) h = (h * 31 + c.charCodeAt(0)) | 0; return PALETTE[Math.abs(h) % PALETTE.length]; };
function contrast(hex) { const h = hex.replace("#", ""); const r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16); return (r * 299 + g * 587 + b * 114) / 1000 > 140 ? "#0b0d12" : "#fff"; }
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3200); }
function esc(s) { return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

/* ---------- storage / verdicts ---------- */
const annotKey = (id) => "tv_annot::" + id;
function loadMyAnnot() { try { S.myAnnot = JSON.parse(localStorage.getItem(annotKey(S.annotatorId))) || {}; } catch { S.myAnnot = {}; } }
function saveMyAnnot() { localStorage.setItem(annotKey(S.annotatorId), JSON.stringify(S.myAnnot)); }
const candId = (tf, c) => tf + "|s" + c.seg + "|" + c.type;
const vget = (id) => S.myAnnot[id] && S.myAnnot[id].verdict;
function setVerdict(cand, v) {
  const id = candId(S.curTF, cand);
  const cur = vget(id);
  if (cur === v) delete S.myAnnot[id];
  else S.myAnnot[id] = { verdict: v, notes: (S.myAnnot[id] && S.myAnnot[id].notes) || "", updated: new Date().toISOString() };
  saveMyAnnot();
  updateCandDom(id); renderSelLine(); renderTraceProgress(); renderProgress(); renderTraceRow(S.tidx);
}
function verdictLabel(v) { return v === CONFIRM ? "✓ да" : v === REJECT ? "✗ нет (FP)" : v ? "→ " + v : ""; }

/* ---------- loading ---------- */
async function tryFetch(p) { try { const r = await fetch(p); if (!r.ok) throw 0; return await r.text(); } catch { return null; } }

function setModel(raw) {
  const typesById = {}, byDomain = {};
  for (const [dom, dv] of Object.entries(raw.domains || {})) {
    byDomain[dom] = [];
    for (const [id, t] of Object.entries(dv.types || {})) {
      typesById[id] = typesById[id] || { id, definition: t.definition || "", domains: [] };
      typesById[id].domains.push(dom); byDomain[dom].push(id);
    }
  }
  S.model = { typesById, byDomain, raw };
}

function setIndex(raw) {
  if (!Array.isArray(raw) || !raw.length) { toast("traces_index.json: пусто"); return; }
  S.index = raw;
  fillSelect("#fBench", uniq(raw.map(x => x.benchmark)), "все бенчи");
  fillSelect("#fModel", uniq(raw.map(x => x.model)), "все модели");
  const agents = uniq(raw.flatMap(x => x.agents || []));
  const fa = $("#fAgent"); fa.innerHTML = '<option value="">любой агент</option>';
  for (const a of agents) { const o = el("option", null, AGENT_LABEL[a] || a); o.value = a; fa.appendChild(o); }
  fillSelect("#fType", uniq(raw.flatMap(x => x.types || [])), "все типы событий");
  applyFilters();
}
function uniq(a) { return [...new Set(a.filter(Boolean))].sort(); }
function fillSelect(sel, vals, allLabel) { const s = $(sel); s.innerHTML = ""; const o0 = el("option", null, allLabel); o0.value = ""; s.appendChild(o0); for (const v of vals) { const o = el("option", null, v); o.value = v; s.appendChild(o); } }

async function loadTrace(tf) {
  if (S.traces[tf] === undefined) {
    const t = await tryFetch((S.cfg && S.cfg.traces_dir || "data/traces") + "/" + tf);
    S.traces[tf] = t ? JSON.parse(t) : null;
  }
  return S.traces[tf];
}

/* ---------- candidates ---------- */
function buildCandidates(events) {
  const byType = {};
  for (const e of (events || [])) (byType[e.t] = byType[e.t] || []).push(e);
  const cands = [];
  for (const t in byType) {
    const evs = byType[t].slice().sort((a, b) => a.s - b.s);
    const used = new Array(evs.length).fill(false);
    for (let i = 0; i < evs.length; i++) {
      if (used[i]) continue;
      const segs = [evs[i].s], agents = new Set([evs[i].a]); used[i] = true;
      for (let j = i + 1; j < evs.length; j++) if (!used[j] && Math.abs(evs[j].s - evs[i].s) <= 1) { segs.push(evs[j].s); agents.add(evs[j].a); used[j] = true; }
      const cnt = {}; let anchor = segs[0], best = 0;
      for (const s of segs) { cnt[s] = (cnt[s] || 0) + 1; if (cnt[s] > best) { best = cnt[s]; anchor = s; } }
      cands.push({ seg: anchor, type: t, agents: [...agents].sort(), segs: [...new Set(segs)].sort((a, b) => a - b) });
    }
  }
  cands.sort((a, b) => a.seg - b.seg || (a.type < b.type ? -1 : 1));
  return cands;
}
function tracePrefix(tf) { return tf + "|"; }
function verifiedCount(tf) { let n = 0; const p = tracePrefix(tf); for (const k in S.myAnnot) if (k.startsWith(p) && S.myAnnot[k].verdict) n++; return n; }

/* ---------- filters + trace list ---------- */
function applyFilters() {
  const q = $("#textSearch").value.trim().toLowerCase();
  const fb = $("#fBench").value, fmo = $("#fModel").value, fa = $("#fAgent").value, fm = $("#fMine").value, fty = $("#fType").value;
  S.filtered = [];
  S.index.forEach((tr, i) => {
    if (fb && tr.benchmark !== fb) return;
    if (fmo && tr.model !== fmo) return;
    if (fa && !(tr.agents || []).includes(fa)) return;
    if (fty && !(tr.types || []).includes(fty)) return;
    const done = verifiedCount(tr.trace_file), total = tr.n_candidates;
    if (fm === "untouched" && done > 0) return;
    if (fm === "done" && done < total) return;
    if (fm === "unfinished" && (done === 0 || done >= total)) return;
    if (q && !((tr.question_id || "").toLowerCase().includes(q) || (tr.model || "").toLowerCase().includes(q))) return;
    S.filtered.push(i);
  });
  $("#filterCount").textContent = `${S.filtered.length} из ${S.index.length} трасс`;
  if (S.tidx >= S.filtered.length) S.tidx = 0;
  renderTraceList(); renderProgress();
}
function renderProgress() {
  const done = Object.values(S.myAnnot).filter(r => r.verdict).length;
  $("#progress").textContent = `размечено событий: ${done}`;
}
function renderTraceList() {
  const ul = $("#traceList"); ul.innerHTML = "";
  const cap = 800, n = Math.min(S.filtered.length, cap);
  for (let pos = 0; pos < n; pos++) ul.appendChild(traceRow(pos));
  if (S.filtered.length > n) ul.appendChild(el("li", "muted", `…ещё ${S.filtered.length - n} (сузь фильтр)`));
}
function traceRow(pos) {
  const tr = S.index[S.filtered[pos]];
  const li = el("li"); li.dataset.pos = pos; if (pos === S.tidx) li.classList.add("active");
  li.appendChild(el("div", "li-id", `${tr.benchmark} · ${tr.model}`));
  const meta = el("div", "li-meta");
  meta.appendChild(el("span", "qid", tr.question_id));
  const done = verifiedCount(tr.trace_file), total = tr.n_candidates;
  const badge = el("span", "badge prog" + (done >= total && total ? " full" : done ? " part" : "")); badge.textContent = `${done}/${total}`;
  meta.appendChild(badge);
  for (const a of (tr.agents || [])) { const d = el("span", "adot"); d.style.background = AGENT_COLOR[a] || "#666"; d.title = AGENT_LABEL[a] || a; meta.appendChild(d); }
  li.appendChild(meta);
  li.onclick = () => { S.tidx = pos; renderTraceList(); openTrace(tr.trace_file); };
  return li;
}
function renderTraceRow(pos) {
  const old = $(`#traceList li[data-pos="${pos}"]`);
  if (old) old.replaceWith(traceRow(pos));
}

/* ---------- open + render trace ---------- */
const curTrace = () => S.filtered.length ? S.index[S.filtered[S.tidx]] : null;
async function openTrace(tf, opts = {}) {
  S.curTF = tf; S.view = null;
  const tr = await loadTrace(tf);
  S.trace = tr;
  S.cands = tr ? buildCandidates(tr.events) : [];
  S.candBySeg = new Map();
  S.cands.forEach((c, i) => { if (!S.candBySeg.has(c.seg)) S.candBySeg.set(c.seg, []); S.candBySeg.get(c.seg).push(i); });
  S.linkedSeg = (opts.seg != null && !isNaN(opts.seg)) ? opts.seg : null;
  S.selSeg = (opts.seg != null && !isNaN(opts.seg)) ? opts.seg : (S.cands.length ? S.cands[0].seg : (tr && tr.segments && tr.segments.length ? tr.segments[0].seg_id : 0));
  const idx = S.index.find(x => x.trace_file === tf);
  $("#qLabel").textContent = idx ? `${idx.benchmark} · ${idx.model} · qid ${idx.question_id} · разметили: ${(idx.agents || []).map(a => AGENT_LABEL[a] || a).join("+")} · ${idx.n_segments} сегм · ${S.cands.length} событий` : tf;
  renderTrace(true);
  if (S.linkedSeg != null) setHashLine(S.linkedSeg); else updateHash();
}

/* карта: op/lam/events по сегменту */
function traceMap() {
  const tr = S.trace; if (!tr) return null; if (tr._map) return tr._map;
  const op = new Map();
  for (const s of (tr.spans || [])) for (let i = s.a; i < s.b; i++) op.set(i, s.op);
  const ev = new Map();
  for (const e of (tr.events || [])) { if (!ev.has(e.s)) ev.set(e.s, { types: new Set(), agents: new Set(), fork: false }); const o = ev.get(e.s); o.types.add(e.t); o.agents.add(e.a); if (isFork(e.t)) o.fork = true; }
  tr._map = { op, ev, lam: tr.lam || [] };
  return tr._map;
}
function gutter(id, m) {
  const g = el("div", "gutter");
  const opName = m ? m.op.get(id) : null;
  const band = el("div", "opband"); band.style.background = opName ? OP_COLOR[opName] || "#333" : "transparent";
  if (opName) band.title = opTitle(opName); g.appendChild(band);
  const lamMax = (S.mapMeta && S.mapMeta.lam_max) || 1;
  const v = m && m.lam.length > id ? m.lam[id] : 0;
  const bar = el("div", "lam"); const frac = Math.max(0, Math.min(1, v / lamMax));
  bar.style.width = (2 + frac * 22).toFixed(1) + "px"; bar.style.opacity = (0.25 + 0.75 * frac).toFixed(2);
  bar.title = "λ(Hawkes) = " + (v || 0).toFixed(2); g.appendChild(bar);
  const o = m ? m.ev.get(id) : null; const gl = el("div", "glyphs");
  if (o) for (const t of o.types) { const sp = el("span", "glyph", isFork(t) ? FORK[t] : "•"); sp.style.color = typeColor(t); sp.title = t + (typeDesc(t) ? " — " + typeDesc(t) : ""); gl.appendChild(sp); }
  g.appendChild(gl); return g;
}

function highlightSeg(text) {
  // подсветить триггер-цитаты событий этого сегмента
  return esc(text);
}
function domTypes() { return (S.model && S.model.byDomain && (S.model.byDomain[(curTrace() || {}).domain] || Object.keys(S.model.typesById))) || []; }
function verifyChip(ci) {
  const c = S.cands[ci], id = candId(S.curTF, c), v = vget(id);
  const chip = el("div", "ev" + (v ? " done" : "")); chip.dataset.cid = id; chip.dataset.ci = ci;
  const tt = el("span", "evtype" + provClass(c.type), (isFork(c.type) ? FORK[c.type] + " " : "") + c.type);
  const col = typeColor(c.type); tt.style.background = col; tt.style.color = contrast(col); tt.title = typeTitle(c.type);
  chip.appendChild(tt);
  const ag = el("span", "evag"); for (const a of c.agents) { const d = el("span", "adot"); d.style.background = AGENT_COLOR[a] || "#666"; d.title = AGENT_LABEL[a] || a; ag.appendChild(d); }
  chip.appendChild(ag);
  const acts = el("span", "evacts");
  const bc = el("button", "vb ok" + (v === CONFIRM ? " on" : ""), "✓"); bc.title = "подтвердить"; bc.onclick = (e) => { e.stopPropagation(); setVerdict(c, CONFIRM); };
  const bx = el("button", "vb no" + (v === REJECT ? " on" : ""), "✗"); bx.title = "отклонить, FP"; bx.onclick = (e) => { e.stopPropagation(); setVerdict(c, REJECT); };
  acts.appendChild(bc); acts.appendChild(bx);
  const sel = el("select", "retype"); sel.title = "другой тип";
  sel.appendChild(new Option("тип…", ""));
  for (const id2 of domTypes()) if (id2 !== c.type) sel.appendChild(new Option(id2, id2));
  sel.value = (v && v !== CONFIRM && v !== REJECT) ? v : "";
  sel.onchange = (e) => { e.stopPropagation(); if (sel.value) setVerdict(c, sel.value); };
  acts.appendChild(sel);
  chip.appendChild(acts);
  return chip;
}
function segRow(id, text, m) {
  const row = el("div", "seg" + (id === cursorSeg() ? " cursorseg" : "") + (id === S.linkedSeg ? " linked" : "")); row.dataset.segId = id;
  const g = gutter(id, m);
  if (m) { g.style.cursor = "pointer"; g.title = "клик — спан + распределение λ по типам"; g.onclick = (e) => { e.stopPropagation(); jumpToSeg(id); drill(id, e); }; }
  row.appendChild(g);
  const sid = el("div", "sid", String(id)); sid.title = "🔗 копировать ссылку на строку " + id; sid.onclick = (e) => { e.stopPropagation(); copyLineLink(id); };
  row.appendChild(sid);
  const right = el("div", "segright"); right.style.cursor = "pointer"; right.onclick = () => selectSeg(id);
  const txt = el("div", "stext"); txt.innerHTML = highlightSeg(text); right.appendChild(txt);
  const cb = S.candBySeg.get(id);
  if (cb) { const box = el("div", "evbox"); for (const ci of cb) box.appendChild(verifyChip(ci)); right.appendChild(box); }
  row.appendChild(right); return row;
}
const cursorSeg = () => (S.selSeg == null ? -1 : S.selSeg);
function segSource() { return (S.trace && S.trace.segments) ? S.trace.segments : []; }

function renderTrace(fresh) {
  const body = $("#traceBody"); const segs = segSource();
  const byId = new Map(segs.map(s => [s.seg_id, s.text]));
  const ids = segs.map(s => s.seg_id);
  const minId = ids.length ? Math.min(...ids) : 0, maxId = ids.length ? Math.max(...ids) : 0;
  const radius = parseInt($("#ctxRadius").value) || 12;
  const focus = cursorSeg() >= 0 ? cursorSeg() : minId;
  const whole = S.wholeTrace;
  if (fresh || !S.view) S.view = whole ? { lo: minId, hi: maxId, minId, maxId } : { lo: Math.max(minId, focus - radius), hi: Math.min(maxId, focus + radius), minId, maxId };
  else { S.view.minId = minId; S.view.maxId = maxId; if (whole) { S.view.lo = minId; S.view.hi = maxId; } }
  const m = traceMap();
  body.innerHTML = "";
  if (!whole && S.view.lo > minId) body.appendChild(el("div", "moretop", "↑ ещё контекст"));
  for (let id = S.view.lo; id <= S.view.hi; id++) if (byId.has(id)) body.appendChild(segRow(id, byId.get(id), m));
  if (!whole && S.view.hi < maxId) body.appendChild(el("div", "morebot", "↓ ещё контекст"));
  if (fresh) { if (S.linkedSeg != null) scrollToSeg(S.linkedSeg); else scrollToCursor(); }
  renderSelLine(); renderTraceProgress();
}
function scrollToCursor() { const r = $(`#traceBody .seg[data-seg-id="${cursorSeg()}"]`); if (r && r.scrollIntoView) r.scrollIntoView({ block: "center" }); }
function scrollToSeg(seg) { const r = $(`#traceBody .seg[data-seg-id="${seg}"]`); if (r && r.scrollIntoView) r.scrollIntoView({ block: "center" }); }

function extend(dir) {
  const segs = segSource(), byId = new Map(segs.map(s => [s.seg_id, s.text])), m = traceMap(), body = $("#traceBody");
  if (dir < 0) {
    const newLo = Math.max(S.view.minId, S.view.lo - CHUNK); if (newLo >= S.view.lo) return;
    const before = body.scrollHeight, top = $("#traceBody .moretop"), frag = document.createDocumentFragment();
    for (let id = newLo; id < S.view.lo; id++) if (byId.has(id)) frag.appendChild(segRow(id, byId.get(id), m));
    body.insertBefore(frag, top ? top.nextSibling : body.firstChild); S.view.lo = newLo;
    if (top && newLo <= S.view.minId) top.remove(); body.scrollTop += body.scrollHeight - before;
  } else {
    const newHi = Math.min(S.view.maxId, S.view.hi + CHUNK); if (newHi <= S.view.hi) return;
    const bot = $("#traceBody .morebot"), frag = document.createDocumentFragment();
    for (let id = S.view.hi + 1; id <= newHi; id++) if (byId.has(id)) frag.appendChild(segRow(id, byId.get(id), m));
    body.insertBefore(frag, bot); S.view.hi = newHi; if (bot && newHi >= S.view.maxId) bot.remove();
  }
}
function onCtxScroll() {
  const body = $("#traceBody"); if (!S.view) return;
  if (body.scrollTop < EDGE && S.view.lo > S.view.minId) extend(-1);
  if (body.scrollHeight - body.scrollTop - body.clientHeight < EDGE && S.view.hi < S.view.maxId) extend(1);
}

/* ---------- cursor / navigation по событиям ---------- */
function jumpToSeg(seg) {
  if (!S.wholeTrace && S.view && (seg < S.view.lo || seg > S.view.hi)) {
    S.view.lo = Math.max(S.view.minId, Math.min(S.view.lo, seg - 3));
    S.view.hi = Math.min(S.view.maxId, Math.max(S.view.hi, seg + 3)); renderTrace(false);
  }
  const r = $(`#traceBody .seg[data-seg-id="${seg}"]`); if (r && r.scrollIntoView) r.scrollIntoView({ block: "center" });
}
const eventSegs = () => [...S.candBySeg.keys()].sort((a, b) => a - b);
function selectSeg(seg, scroll) {
  S.selSeg = seg;
  let row = $(`#traceBody .seg[data-seg-id="${seg}"]`);
  if (!row && !S.wholeTrace && S.view) {
    S.view.lo = Math.max(S.view.minId, Math.min(S.view.lo, seg - 3));
    S.view.hi = Math.min(S.view.maxId, Math.max(S.view.hi, seg + 3)); renderTrace(false);
    row = $(`#traceBody .seg[data-seg-id="${seg}"]`);
  }
  $$("#traceBody .seg.cursorseg").forEach(r => r.classList.remove("cursorseg"));
  if (row) { row.classList.add("cursorseg"); if (scroll && row.scrollIntoView) row.scrollIntoView({ block: "center" }); }
  renderSelLine(); updateHash();
}
function gotoEvent(dir) {
  const segs = eventSegs(); if (!segs.length) { toast("на трассе нет событий"); return; }
  const cur = S.selSeg == null ? (dir > 0 ? -1 : Infinity) : S.selSeg;
  let target = dir > 0 ? segs.find(s => s > cur) : null;
  if (dir < 0) for (let i = segs.length - 1; i >= 0; i--) { if (segs[i] < cur) { target = segs[i]; break; } }
  if (target == null) target = dir > 0 ? segs[0] : segs[segs.length - 1];   // зациклить
  selectSeg(target, true);
}

/* правая панель = инфа о ВЫБРАННОЙ строке (сегменте) */
function selEvent(ci) {
  const c = S.cands[ci], id = candId(S.curTF, c), v = vget(id);
  const box = el("div", "sl-ev" + (v ? " done" : ""));
  const tt = el("span", "ce-type" + provClass(c.type), (isFork(c.type) ? FORK[c.type] + " " : "") + c.type);
  const col = typeColor(c.type); tt.style.background = col; tt.style.color = contrast(col); tt.title = typeTitle(c.type); box.appendChild(tt);
  if (typeDesc(c.type)) box.appendChild(el("div", "ce-def", typeDesc(c.type)));
  box.appendChild(el("div", "ce-ag", "нашли: " + c.agents.map(a => AGENT_LABEL[a] || a).join(", ")));
  const acts = el("div", "ce-acts");
  const bc = el("button", "vbig ok" + (v === CONFIRM ? " on" : ""), "✓ да"); bc.onclick = () => setVerdict(c, CONFIRM);
  const bx = el("button", "vbig no" + (v === REJECT ? " on" : ""), "✗ нет"); bx.onclick = () => setVerdict(c, REJECT);
  acts.appendChild(bc); acts.appendChild(bx); box.appendChild(acts);
  const sel = el("select", "ce-retype"); sel.appendChild(new Option("→ другой тип…", ""));
  for (const id2 of domTypes()) if (id2 !== c.type) sel.appendChild(new Option(id2, id2));
  sel.value = (v && v !== CONFIRM && v !== REJECT) ? v : ""; sel.onchange = () => { if (sel.value) setVerdict(c, sel.value); };
  box.appendChild(sel);
  if (v) box.appendChild(el("div", "ce-ver", "вердикт: " + verdictLabel(v)));
  const note = el("textarea", "ce-note"); note.rows = 2; note.placeholder = "заметка…"; note.value = (S.myAnnot[id] && S.myAnnot[id].notes) || "";
  note.onchange = () => { if (!S.myAnnot[id]) S.myAnnot[id] = { verdict: null, notes: "", updated: null }; S.myAnnot[id].notes = note.value; S.myAnnot[id].updated = new Date().toISOString(); saveMyAnnot(); };
  box.appendChild(note);
  for (const p of S.pool) { const pr = p.annotations && p.annotations[id]; if (pr && pr.verdict) box.appendChild(el("div", "peer", `👤 ${p.annotator_id}: ${verdictLabel(pr.verdict)}`)); }
  return box;
}
function renderSelLine() {
  const box = $("#curEvent"); box.innerHTML = "";
  const seg = S.selSeg;
  if (seg == null || !S.trace) { box.appendChild(el("div", "muted", "выбери строку слева")); return; }
  const acts = el("div", "sl-nav");
  const pe = el("button", "btn small", "◀ соб."); pe.onclick = () => gotoEvent(-1);
  const ne = el("button", "btn small primary", "перейти к след. событию ▶"); ne.onclick = () => gotoEvent(1);
  acts.appendChild(pe); acts.appendChild(ne); box.appendChild(acts);
  box.appendChild(el("div", "ce-pos", "строка (сегмент) " + seg));
  const m = traceMap(); const op = m ? m.op.get(seg) : null;
  if (op) {
    const opl = el("div", "sl-op"); opl.appendChild(el("span", "sl-lbl", "спан: "));
    const chip = el("span", "opchip", op); chip.style.background = OP_COLOR[op] || "#333"; chip.style.color = contrast(OP_COLOR[op] || "#333"); chip.title = opTitle(op);
    opl.appendChild(chip);
    const sp = (S.trace.spans || []).find(s => seg >= s.a && seg < s.b); if (sp) opl.appendChild(el("span", "sl-dim", ` (${sp.a}–${sp.b - 1})`));
    box.appendChild(opl);
    if (OP_DESC[op]) box.appendChild(el("div", "ce-def", OP_DESC[op]));
  }
  const lam = m && m.lam.length > seg ? m.lam[seg] : null;
  if (lam != null) { const lr = el("div", "sl-lam"); lr.appendChild(el("span", "sl-lbl", "λ Hawkes: " + lam.toFixed(2) + " ")); const b = el("button", "btn small", "распределение"); b.onclick = (e) => drill(seg, e); lr.appendChild(b); box.appendChild(lr); }
  const st = (S.trace.segments.find(s => s.seg_id === seg) || {}).text || "";
  box.appendChild(el("div", "sl-text", st));
  const lb = el("button", "btn small", "🔗 ссылка на строку"); lb.onclick = () => copyLineLink(seg); box.appendChild(lb);
  const cb = S.candBySeg.get(seg) || [];
  box.appendChild(el("div", "sl-h", cb.length ? "события здесь (" + cb.length + "):" : "событий на этой строке нет"));
  for (const ci of cb) box.appendChild(selEvent(ci));
}
function renderTraceProgress() {
  const done = verifiedCount(S.curTF);
  $("#traceProgress").textContent = `на трассе размечено: ${done} / ${S.cands.length}`;
}
function updateCandDom(id) {
  const v = vget(id);
  $$(`#traceBody .ev[data-cid="${id.replace(/"/g, '\\"')}"]`).forEach(chip => {
    chip.classList.toggle("done", !!v);
    const ok = chip.querySelector(".vb.ok"), no = chip.querySelector(".vb.no");
    if (ok) ok.classList.toggle("on", v === CONFIRM); if (no) no.classList.toggle("on", v === REJECT);
  });
}

/* ---------- drill-down ---------- */
function lamByType(seg) {
  const P = S.mapMeta && S.mapMeta.hawkes_by_type; if (!P || !S.trace) return null;
  const byT = {}; for (const e of (S.trace.events || [])) (byT[e.t] = byT[e.t] || []).push(e.s);
  const out = [];
  for (const t in P) { const p = P[t]; let exc = 0; for (const ti of (byT[t] || [])) if (ti < seg) exc += Math.exp(-p.beta * (seg - ti)); out.push({ type: t, lam: p.mu + p.alpha * p.beta * exc, hasEv: (byT[t] || []).some(x => x <= seg) }); }
  out.sort((a, b) => b.lam - a.lam); return out;
}
function closeDrill() { const d = $("#drillpop"); if (d) d.remove(); }
function drill(seg, evt) {
  closeDrill();
  const m = traceMap(); const op = m ? m.op.get(seg) : null;
  const pop = el("div", "drillpop"); pop.id = "drillpop";
  // какой операторный спан идёт в этой точке
  const head = el("div", "dh", `сегмент ${seg}`);
  pop.appendChild(head);
  const opline = el("div", "dop");
  if (op) {
    opline.appendChild(el("span", "dopl", "спан: "));
    const chip = el("span", "opchip", op); chip.style.background = OP_COLOR[op] || "#333"; chip.style.color = contrast(OP_COLOR[op] || "#333"); chip.title = opTitle(op);
    opline.appendChild(chip);
    // границы спана
    const sp = (S.trace && S.trace.spans || []).find(s => seg >= s.a && seg < s.b);
    if (sp) opline.appendChild(el("span", "dopr", ` (сегм. ${sp.a}–${sp.b - 1})`));
  } else opline.appendChild(el("span", "dopl", "спан: —"));
  pop.appendChild(opline);
  if (op && OP_DESC[op]) pop.appendChild(el("div", "dopdesc", OP_DESC[op]));
  const linkBtn = el("button", "dlink", "🔗 копировать ссылку на строку " + seg);
  linkBtn.onclick = (e) => { e.stopPropagation(); copyLineLink(seg); };
  pop.appendChild(linkBtn);
  const dist = lamByType(seg);
  if (dist) {
    const tot = dist.reduce((a, b) => a + b.lam, 0), max = Math.max(...dist.map(d => d.lam), 1e-6);
    pop.appendChild(el("div", "dsub", `Σλ = ${tot.toFixed(2)} · интенсивность по типам`));
    for (const d of dist) { if (d.lam < 1e-3 && !d.hasEv) continue; const row = el("div", "drow"); const nm = el("span", "dtype" + provClass(d.type), d.type); nm.style.color = typeColor(d.type); nm.title = typeTitle(d.type); row.appendChild(nm); const bw = el("span", "dbarwrap"), bar = el("span", "dbar"); bar.style.width = (3 + 92 * d.lam / max).toFixed(0) + "px"; bar.style.background = typeColor(d.type); bw.appendChild(bar); row.appendChild(bw); row.appendChild(el("span", "dval", d.lam.toFixed(3))); pop.appendChild(row); }
  }
  document.body.appendChild(pop);
  const x = Math.min((evt ? evt.clientX : 200) + 10, window.innerWidth - 250), y = Math.min((evt ? evt.clientY : 120) + 4, window.innerHeight - pop.offsetHeight - 12);
  pop.style.left = Math.max(6, x) + "px"; pop.style.top = Math.max(6, y) + "px";
}

/* ---------- дерево всей трассы (d3) ---------- */
// иерархия: корень(трасса) -> операторные спаны -> события-кандидаты в спане
function buildTreeData() {
  const tr = S.trace; if (!tr) return null;
  const m = traceMap();
  const lamAt = (s) => (m && m.lam.length > s ? m.lam[s] : 0);
  const mkEvent = (ci) => {
    const c = S.cands[ci], v = vget(candId(S.curTF, c));
    return { kind: "event", ci, seg: c.seg, type: c.type, agents: c.agents, fork: isFork(c.type), verdict: v || null };
  };
  const spans = (tr.spans || []).slice().sort((a, b) => a.a - b.a);
  const used = new Set(), spanNodes = [];
  for (const sp of spans) {
    const kids = [];
    S.cands.forEach((c, i) => { if (!used.has(i) && c.seg >= sp.a && c.seg < sp.b) { kids.push(mkEvent(i)); used.add(i); } });
    let sum = 0, n = 0; for (let s = sp.a; s < sp.b; s++) { sum += lamAt(s); n++; }
    spanNodes.push({ kind: "span", op: sp.op, a: sp.a, b: sp.b, seg: sp.a, lam: n ? sum / n : 0, children: kids });
  }
  const orphans = []; S.cands.forEach((c, i) => { if (!used.has(i)) orphans.push(mkEvent(i)); });
  if (orphans.length) spanNodes.push({ kind: "span", op: null, a: null, b: null, seg: orphans[0].seg, lam: 0, children: orphans.sort((a, b) => a.seg - b.seg) });
  const idx = S.index.find(x => x.trace_file === S.curTF);
  return { kind: "root", name: idx ? `${idx.benchmark} · ${idx.model} · ${idx.question_id}` : (S.curTF || "трасса"), children: spanNodes };
}
function nodeLabel(nd) {
  if (nd.kind === "root") return nd.name;
  if (nd.kind === "span") return (nd.op || "вне спанов") + (nd.a != null ? ` ${nd.a}–${nd.b - 1}` : "") + (nd.lam ? `  λ${nd.lam.toFixed(2)}` : "");
  const ag = nd.agents.map(a => AGENT_LABEL[a] || a).join("/");
  const vv = nd.verdict === CONFIRM ? " ✓" : nd.verdict === REJECT ? " ✗" : nd.verdict ? " →" + nd.verdict : "";
  return `${(nd.fork ? FORK[nd.type] + " " : "")}${nd.type} · s${nd.seg} · ${ag}${vv}`;
}
function nodeTitle(nd) {
  if (nd.kind === "span") return nd.op ? opTitle(nd.op) : "события вне операторных спанов";
  if (nd.kind === "event") return typeTitle(nd.type) + " · нашли: " + nd.agents.map(a => AGENT_LABEL[a] || a).join(", ") + (nd.verdict ? " · вердикт " + verdictLabel(nd.verdict) : "");
  return nd.name;
}
function treeEsc(e) { if (e.key === "Escape") closeTree(); }
function closeTree() { const o = $("#treeModal"); if (o) o.remove(); document.removeEventListener("keydown", treeEsc); }
function openTree() {
  closeTree(); closeDrill();
  if (typeof d3 === "undefined") { toast("d3 не загрузился (vendor/d3.min.js)"); return; }
  const data = buildTreeData();
  if (!data || !data.children.length) { toast("на трассе нет спанов/событий"); return; }
  const overlay = el("div", "treemodal"); overlay.id = "treeModal";
  overlay.onclick = (e) => { if (e.target === overlay) closeTree(); };
  const win = el("div", "treewin");
  const head = el("div", "treehead");
  head.appendChild(el("div", "treetitle", "🌳 " + data.name));
  const leg = el("div", "treelegend");
  leg.innerHTML = "спаны — цвет оператора · события: ● тип с ReasonOps-аналогом, ▧ наш тип · ◇◄✗ форки · кольцо: <b style='color:#30a46c'>✓</b>/<b style='color:#e5484d'>✗</b>/<b style='color:#4c8dff'>тип</b> · клик по узлу — перейти к строке";
  head.appendChild(leg);
  const x = el("button", "treex", "✕"); x.title = "закрыть (Esc)"; x.onclick = closeTree; head.appendChild(x);
  win.appendChild(head);
  const scroll = el("div", "treescroll"); win.appendChild(scroll);
  overlay.appendChild(win); document.body.appendChild(overlay);
  document.addEventListener("keydown", treeEsc);

  const root = d3.hierarchy(data);
  const rowH = 22, colW = 230;
  d3.tree().nodeSize([rowH, colW]).separation((a, b) => a.parent === b.parent ? 1 : 1.4)(root);
  let minX = Infinity, maxX = -Infinity, maxY = 0;
  root.each(d => { if (d.x < minX) minX = d.x; if (d.x > maxX) maxX = d.x; if (d.y > maxY) maxY = d.y; });
  const W = maxY + colW + 200, H = (maxX - minX) + rowH * 3;
  const svg = d3.select(scroll).append("svg").attr("width", W).attr("height", H);
  const g = svg.append("g").attr("transform", `translate(120,${-minX + rowH})`);
  g.append("g").attr("fill", "none").attr("stroke", "#2f3644").attr("stroke-width", 1.2)
    .selectAll("path").data(root.links()).join("path")
    .attr("d", d3.linkHorizontal().x(d => d.y).y(d => d.x));
  const node = g.append("g").selectAll("g").data(root.descendants()).join("g")
    .attr("class", d => "tnode tnode-" + d.data.kind)
    .attr("transform", d => `translate(${d.y},${d.x})`)
    .style("cursor", d => d.data.kind === "root" ? "default" : "pointer")
    .on("click", (e, d) => { if (d.data.kind === "root") return; closeTree(); selectSeg(d.data.seg, true); });
  node.each(function (d) {
    const sel = d3.select(this), nd = d.data;
    if (nd.kind === "span") {
      const c = nd.op ? (OP_COLOR[nd.op] || "#555") : "#3a4152";
      sel.append("rect").attr("x", -7).attr("y", -8).attr("width", 14).attr("height", 16).attr("rx", 4)
        .attr("fill", c).attr("stroke", "#0b0d12").attr("stroke-width", 1);
    } else if (nd.kind === "event") {
      const c = typeColor(nd.type);
      const vc = nd.verdict === CONFIRM ? "#30a46c" : nd.verdict === REJECT ? "#e5484d" : nd.verdict ? "#4c8dff" : "#0b0d12";
      // провенанс: круг = тип с ReasonOps-аналогом, пунктирный квадрат = чисто наш тип
      const shape = isReasonOps(nd.type)
        ? sel.append("circle").attr("r", 6)
        : sel.append("rect").attr("x", -6).attr("y", -6).attr("width", 12).attr("height", 12).attr("stroke-dasharray", nd.verdict ? null : "2,2");
      shape.attr("fill", c).attr("stroke", vc).attr("stroke-width", nd.verdict ? 2.6 : 1.2);
      if (!nd.verdict && !isReasonOps(nd.type)) shape.attr("stroke", "#8b93a5");
      if (nd.fork) sel.append("text").attr("text-anchor", "middle").attr("dy", "0.32em").attr("font-size", "9px").attr("fill", contrast(c)).attr("pointer-events", "none").text(FORK[nd.type]);
    } else {
      sel.append("circle").attr("r", 5).attr("fill", "#8b93a5").attr("stroke", "#0b0d12");
    }
    sel.append("text").attr("x", 12).attr("dy", "0.32em").attr("fill", "#e6e9ef").text(nodeLabel(nd));
    sel.append("title").text(nodeTitle(nd));
  });
}

/* ---------- deep-link / share ---------- */
function parseHash() { const p = {}; for (const kv of location.hash.replace(/^#/, "").split("&")) { const i = kv.indexOf("="); if (i > 0) p[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1)); } return p; }
function updateHash() {
  if (!S.curTF) return;
  let h = "#trace=" + encodeURIComponent(S.curTF);
  if (S.selSeg != null) h += "&seg=" + S.selSeg;
  if (h !== S._lastHash) { S._lastHash = h; try { history.replaceState(null, "", h); } catch { location.hash = h; } }
}
function applyHash(p) {
  p = p || parseHash();
  if (!p.trace) return false;
  const pos = S.filtered.findIndex(i => S.index[i].trace_file === p.trace);
  if (pos >= 0) { S.tidx = pos; renderTraceList(); }
  openTrace(p.trace, { seg: p.seg != null ? parseInt(p.seg) : null });
  return true;
}
function shareLink() { updateHash(); copyUrl("ссылка скопирована"); }
function setHashLine(seg) { const h = "#trace=" + encodeURIComponent(S.curTF) + "&seg=" + seg; if (h !== S._lastHash) { S._lastHash = h; try { history.replaceState(null, "", h); } catch { location.hash = h; } } }
function copyUrl(msg) { const url = location.href; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(() => toast(msg), () => toast(url)); else toast(url); }
function copyLineLink(seg) {
  S.linkedSeg = seg;
  $$("#traceBody .seg.linked").forEach(r => r.classList.remove("linked"));
  const row = $(`#traceBody .seg[data-seg-id="${seg}"]`); if (row) row.classList.add("linked");
  setHashLine(seg); copyUrl("ссылка на строку " + seg + " скопирована");
}

/* ---------- export / import / GitHub ---------- */
function annotPayload() { return { annotator_id: S.annotatorId, exported: new Date().toISOString(), tool: "toloka-v2", annotations: S.myAnnot }; }
function download(name, obj) { const blob = new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" }); const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href); }
function ghCfg() { try { return JSON.parse(localStorage.getItem("tv_gh") || "{}"); } catch { return {}; } }
function ghSave() { const c = { owner: $("#ghOwner").value.trim(), repo: $("#ghRepo").value.trim(), branch: $("#ghBranch").value.trim() || "main", path: $("#ghPath").value.trim() || "annotations", token: $("#ghToken").value.trim() }; localStorage.setItem("tv_gh", JSON.stringify(c)); toast("настройки GitHub сохранены"); return c; }
function b64(str) { return btoa(unescape(encodeURIComponent(str))); }
async function ghCommit() {
  const c = ghSave(); if (!c.owner || !c.repo || !c.token) { toast("заполни owner / repo / token"); return; }
  const path = `${c.path}/annot_${S.annotatorId}.json`;
  const api = `https://api.github.com/repos/${c.owner}/${c.repo}/contents/${path.split("/").map(encodeURIComponent).join("/")}`;
  const headers = { Authorization: "Bearer " + c.token, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  let sha = null;
  try { const g = await fetch(api + "?ref=" + encodeURIComponent(c.branch), { headers }); if (g.ok) sha = (await g.json()).sha; else if (g.status !== 404) toast("GitHub чтение " + g.status); }
  catch (e) { toast("сеть: " + e.message); return; }
  const done = Object.values(S.myAnnot).filter(r => r.verdict).length;
  const body = { message: `разметка ${S.annotatorId}: ${done} событий`, content: b64(JSON.stringify(annotPayload(), null, 1)), branch: c.branch };
  if (sha) body.sha = sha;
  try { const r = await fetch(api, { method: "PUT", headers, body: JSON.stringify(body) }); if (r.ok) { const j = await r.json(); toast("сохранено в GitHub ✓ " + (j.commit && j.commit.sha || "").slice(0, 7)); } else { toast("GitHub " + r.status + ": " + (await r.text()).slice(0, 120)); } }
  catch (e) { toast("сеть: " + e.message); }
}

/* ---------- events ---------- */
function bind() {
  $("#annotatorId").value = S.annotatorId;
  $("#annotatorId").onchange = () => { S.annotatorId = $("#annotatorId").value.trim() || "anon"; localStorage.setItem("tv_last_annotator", S.annotatorId); loadMyAnnot(); applyFilters(); if (S.curTF) renderTrace(false); toast("аннотатор: " + S.annotatorId); };
  $("#importAnnot").onchange = ev => { for (const f of ev.target.files) { const r = new FileReader(); r.onload = () => { try { const j = JSON.parse(r.result); if (j.annotator_id === S.annotatorId) { Object.assign(S.myAnnot, j.annotations || {}); saveMyAnnot(); toast("загружена МОЯ разметка"); } else { S.pool = S.pool.filter(p => p.annotator_id !== j.annotator_id); S.pool.push(j); toast("подключена разметка: " + j.annotator_id); } applyFilters(); if (S.curTF) renderTrace(false); } catch { toast("не JSON"); } }; r.readAsText(f); } };
  $("#exportAnnot").onclick = () => download(`annot_${S.annotatorId}.json`, annotPayload());
  $("#ghBtn").onclick = () => $("#ghPanel").classList.toggle("hidden");
  const g = ghCfg();
  $("#ghOwner").value = g.owner || "karpovilia"; $("#ghRepo").value = g.repo || "toloka"; $("#ghBranch").value = g.branch || "main"; $("#ghPath").value = g.path || "annotations"; $("#ghToken").value = g.token || "";
  $("#ghSaveCfg").onclick = ghSave; $("#ghCommit").onclick = ghCommit;
  $("#shareBtn").onclick = shareLink;
  $("#listToggle").onclick = () => { $("#main").classList.toggle("nolist"); $("#listToggle").classList.toggle("on"); };
  window.addEventListener("hashchange", () => { if (location.hash !== S._lastHash) applyHash(); });
  $("#prevEv").onclick = () => gotoEvent(-1);
  $("#nextEv").onclick = () => gotoEvent(1);
  $("#wholeBtn").onclick = () => { S.wholeTrace = !S.wholeTrace; $("#wholeBtn").classList.toggle("on", S.wholeTrace); renderTrace(true); };
  $("#wholeBtn").classList.toggle("on", S.wholeTrace);
  $("#treeBtn").onclick = openTree;
  $("#ctxRadius").onchange = () => renderTrace(true);
  $("#traceBody").addEventListener("scroll", () => { closeDrill(); if (S.scrolling) return; S.scrolling = true; requestAnimationFrame(() => { S.scrolling = false; onCtxScroll(); }); });
  document.addEventListener("click", closeDrill);
  ["textSearch", "fBench", "fModel", "fAgent", "fType", "fMine"].forEach(id => { const e = $("#" + id); e.oninput = e.onchange = applyFilters; });
  $("#fReset").onclick = () => { ["fBench", "fModel", "fAgent", "fType", "fMine"].forEach(id => { $("#" + id).value = ""; }); $("#textSearch").value = ""; applyFilters(); toast("фильтры сброшены"); };
  document.addEventListener("keydown", ev => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (ev.key === "[") return selTrace(-1);
    if (ev.key === "]") return selTrace(1);
    if (ev.key === "j" || ev.key === "n" || ev.key === "ArrowDown") return gotoEvent(1);
    if (ev.key === "k" || ev.key === "p" || ev.key === "ArrowUp") return gotoEvent(-1);
    if (ev.key === "t") return openTree();
    // 1/2 — вердикт первому событию на выбранной строке
    const cb = S.candBySeg.get(S.selSeg); const c = cb && cb.length ? S.cands[cb[0]] : null;
    if (ev.key === "1" && c) setVerdict(c, CONFIRM);
    else if (ev.key === "2" && c) setVerdict(c, REJECT);
  });
}
function selTrace(d) { if (!S.filtered.length) return; S.tidx = (S.tidx + d + S.filtered.length) % S.filtered.length; renderTraceList(); openTrace(S.index[S.filtered[S.tidx]].trace_file); }

/* ---------- init ---------- */
(async function init() {
  bind(); loadMyAnnot();
  const boot = parseHash();
  const cfgTxt = await tryFetch("config.json");
  S.cfg = cfgTxt ? JSON.parse(cfgTxt) : { event_types: "data/event_types.json", traces_index: "data/traces_index.json", traces_dir: "data/traces", trace_maps_meta: "data/trace_maps_meta.json" };
  const mm = await tryFetch(S.cfg.trace_maps_meta || "data/trace_maps_meta.json"); if (mm) try { S.mapMeta = JSON.parse(mm); } catch {}
  const et = await tryFetch(S.cfg.event_types); if (et) try { setModel(JSON.parse(et)); } catch (e) { toast("event_types: " + e.message); }
  const ix = await tryFetch(S.cfg.traces_index || "data/traces_index.json"); if (ix) try { setIndex(JSON.parse(ix)); } catch (e) { toast("traces_index: " + e.message); }
  if (!ix) { toast("нет traces_index.json"); return; }
  if (boot.trace) applyHash(boot);
  else if (S.filtered.length) openTrace(S.index[S.filtered[0]].trace_file);
})();
