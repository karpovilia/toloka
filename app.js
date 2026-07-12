"use strict";
/* Toloka — ручная адъюдикация конфликтов разметки нескольких агентов.
   Схема входа: config.json -> data/event_types.json + data/conflicts.json.
   Конфликт-сайт: {item_id, slice, model, benchmark, question_id, domain, cell, seg_id, segs,
     agents_present:[...], per_agent:{agent:[types]}, quotes:{agent:[quote]}, priority,
     context_window:[{seg_id,text}]}.
   Вердикт человека на сайте = правильный тип события | "∅" (нет события) | "unclear".
   Разметка хранится в localStorage (tv_annot::<annotator>), обмен — файлами. */

const $ = (s, r = document) => r.querySelector(s);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const AGENT_COLOR = { regex: "#f76b15", claude: "#d97757", deepseek: "#4c8dff", qwen: "#8e4ec6" };
const AGENT_LABEL = { regex: "regex", claude: "Claude", deepseek: "DeepSeek", qwen: "Qwen" };
const PRIO_COLOR = { 4: "#e5484d", 3: "#ffb224", 2: "#4c8dff", 1: "#8b93a5" };
const PALETTE = ["#e5484d", "#30a46c", "#8e4ec6", "#0091ff", "#f76b15", "#ffb224", "#12a594", "#e93d82", "#6e56cf", "#46a758", "#d6409f", "#5b5bd6"];
const NONE = "∅", UNCLEAR = "unclear";

const S = {
  model: null, items: [], filtered: [], idx: 0,
  annotatorId: localStorage.getItem("tv_last_annotator") || "ki",
  myAnnot: {}, pool: [], cfg: null,
};

const typeColor = (id) => { if (id === NONE) return "#8b93a5"; if (id === UNCLEAR) return "#ffb224"; let h = 0; for (const c of (id || "")) h = (h * 31 + c.charCodeAt(0)) | 0; return PALETTE[Math.abs(h) % PALETTE.length]; };
function contrast(hex) { const h = hex.replace("#", ""); const r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16); return (r * 299 + g * 587 + b * 114) / 1000 > 140 ? "#0b0d12" : "#fff"; }
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 2200); }
function esc(s) { return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

/* ---------- storage ---------- */
const annotKey = (id) => "tv_annot::" + id;
function loadMyAnnot() { try { S.myAnnot = JSON.parse(localStorage.getItem(annotKey(S.annotatorId))) || {}; } catch { S.myAnnot = {}; } }
function saveMyAnnot() { localStorage.setItem(annotKey(S.annotatorId), JSON.stringify(S.myAnnot)); }
function recFor(id, create) { if (!S.myAnnot[id] && create) S.myAnnot[id] = { verdict: null, notes: "", updated: null }; return S.myAnnot[id]; }
const recDone = (r) => !!r && !!r.verdict;

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
    li.appendChild(el("div", "li-id", `${AGENT_LABEL[it.agents_present[0]] ? it.slice : it.slice} · ${it.benchmark} · s${it.seg_id}`));
    const meta = el("div", "li-meta");
    const pb = el("span", "badge prio", "P" + it.priority); pb.style.background = PRIO_COLOR[it.priority]; pb.style.color = contrast(PRIO_COLOR[it.priority]); meta.appendChild(pb);
    for (const a of it.agents_present) { const b = el("span", "badge", (it.per_agent[a] || [NONE]).join(",")); b.style.borderColor = AGENT_COLOR[a]; b.title = AGENT_LABEL[a] || a; meta.appendChild(b); }
    const rec = S.myAnnot[it.item_id]; if (recDone(rec)) { const vb = el("span", "badge done", "✓ " + rec.verdict); meta.appendChild(vb); }
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

function renderItem() {
  const it = currentItem();
  $("#posLabel").textContent = S.filtered.length ? `${S.idx + 1}/${S.filtered.length}` : "–/–";
  const body = $("#traceBody"); body.innerHTML = "";
  if (!it) { $("#qLabel").textContent = ""; $("#agents").innerHTML = ""; $("#verdict").innerHTML = ""; $("#myDecisions").innerHTML = ""; $("#focusSeg").textContent = "–"; $("#focusText").textContent = ""; return; }
  $("#qLabel").textContent = `срез ${it.slice} · ${it.model} · ${it.benchmark} · qid ${it.question_id} · domain ${it.domain} · P${it.priority}`;
  const radius = parseInt($("#ctxRadius").value);
  const focusSet = new Set(it.segs || [it.seg_id]);
  for (const s of (it.context_window || [])) {
    if (!isNaN(radius) && Math.abs(s.seg_id - it.seg_id) > radius) continue;
    const focus = focusSet.has(s.seg_id);
    const row = el("div", "seg" + (focus ? " focus" : ""));
    row.appendChild(el("div", "sid", String(s.seg_id)));
    const txt = el("div", "stext");
    if (focus) txt.innerHTML = highlight(s.text, it); else txt.textContent = s.text;
    row.appendChild(txt); body.appendChild(row);
  }
  const f = $("#traceBody .focus"); if (f && f.scrollIntoView) f.scrollIntoView({ block: "center" });

  $("#focusSeg").textContent = `s${it.seg_id}` + (it.segs && it.segs.length > 1 ? ` (сегм. ${it.segs.join(",")})` : "");
  const focusTxt = (it.context_window || []).filter(s => focusSet.has(s.seg_id)).map(s => s.text).join(" ⏎ ");
  $("#focusText").textContent = focusTxt;

  renderAgents(it); renderVerdict(it); renderMy(it);
}

function renderAgents(it) {
  const box = $("#agents"); box.innerHTML = "";
  const rec = S.myAnnot[it.item_id];
  for (const a of it.agents_present) {
    const types = it.per_agent[a] || [NONE];
    const card = el("div", "agent");
    if (recDone(rec)) card.classList.add(types.includes(rec.verdict) ? "match" : "miss");
    const head = el("div", "ahead");
    const nm = el("span", "aname", AGENT_LABEL[a] || a); nm.style.color = AGENT_COLOR[a]; head.appendChild(nm);
    const tw = el("span", "atypes");
    for (const t of types) { const tt = el("span", "atype", t); const c = typeColor(t); tt.style.background = c; tt.style.color = contrast(c); tw.appendChild(tt); }
    head.appendChild(tw);
    if (recDone(rec)) head.appendChild(el("span", "averdict", types.includes(rec.verdict) ? "✅" : "❌"));
    card.appendChild(head);
    const qu = (it.quotes?.[a] || []).filter(Boolean)[0];
    if (qu) { const d = el("div", "aquote"); d.innerHTML = "«<b>" + esc(qu) + "</b>»"; card.appendChild(d); }
    box.appendChild(card);
  }
}

function candidateTypes(it) {
  const domTypes = S.model?.byDomain?.[it.domain] || Object.keys(S.model?.typesById || {});
  const asserted = siteTypes(it);
  // кандидаты вперёд: типы, которые назвал хоть один агент (в порядке частоты)
  return { asserted, all: domTypes };
}

function renderVerdict(it) {
  const box = $("#verdict"); box.innerHTML = "";
  box.appendChild(el("div", "vhead", "истина на сайте: что здесь на самом деле?"));
  const rec = recFor(it.item_id, false);
  const { asserted, all } = candidateTypes(it);

  const cand = el("div", "vbtns");
  asserted.forEach((t, i) => {
    const b = el("button", "vbtn cand" + (rec?.verdict === t ? " sel" : ""), (i < 9 ? (i + 1) + " · " : "") + t);
    b.onclick = () => setVerdict(it, t); cand.appendChild(b);
  });
  box.appendChild(cand);

  const sp = el("div", "vbtns");
  const bn = el("button", "vbtn none" + (rec?.verdict === NONE ? " sel" : ""), "0 · " + NONE + " нет события");
  bn.onclick = () => setVerdict(it, NONE); sp.appendChild(bn);
  const bu = el("button", "vbtn unclear" + (rec?.verdict === UNCLEAR ? " sel" : ""), "u · неясно");
  bu.onclick = () => setVerdict(it, UNCLEAR); sp.appendChild(bu);
  box.appendChild(sp);

  box.appendChild(el("div", "vhint", "или выбрать другой тип из домена " + it.domain + ":"));
  const opt = (txt, val) => { const o = el("option", null, txt); o.value = val; return o; };
  const csel = el("select"); csel.appendChild(opt("— другой тип —", ""));
  for (const id of all) if (!asserted.includes(id)) csel.appendChild(opt(id, id));
  csel.value = (rec && !asserted.includes(rec.verdict) && rec.verdict !== NONE && rec.verdict !== UNCLEAR) ? rec.verdict : "";
  csel.onchange = () => { if (csel.value) setVerdict(it, csel.value); };
  box.appendChild(csel);

  const note = el("textarea"); note.rows = 2; note.placeholder = "заметка…"; note.value = rec?.notes || "";
  note.onchange = () => { const r = recFor(it.item_id, true); r.notes = note.value; touch(r); };
  box.appendChild(note);
}

function setVerdict(it, v) { const r = recFor(it.item_id, true); r.verdict = (r.verdict === v ? null : v); touch(r); renderList(); renderAgents(it); renderVerdict(it); renderMy(it); renderProgress(); }
function touch(r) { r.updated = new Date().toISOString(); saveMyAnnot(); }

function renderMy(it) {
  const box = $("#myDecisions"); box.innerHTML = "";
  const r = S.myAnnot[it.item_id];
  if (recDone(r) || r?.notes) { const d = el("div", "mydec"); d.textContent = `${S.annotatorId}: ${r.verdict || "—"}${r.notes ? " · " + r.notes : ""}`; box.appendChild(d); }
  for (const p of S.pool) { const pr = p.annotations?.[it.item_id]; if (recDone(pr)) { const d = el("div", "mydec"); d.style.borderColor = "#8e4ec6"; d.textContent = `👤 ${p.annotator_id}: ${pr.verdict}${pr.notes ? " · " + pr.notes : ""}`; box.appendChild(d); } }
}

/* ---------- export / import ---------- */
function download(name, obj) { const blob = new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" }); const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href); }

/* ---------- events ---------- */
function bind() {
  $("#annotatorId").value = S.annotatorId;
  $("#annotatorId").onchange = () => { S.annotatorId = $("#annotatorId").value.trim() || "anon"; localStorage.setItem("tv_last_annotator", S.annotatorId); loadMyAnnot(); applyFilters(); toast("аннотатор: " + S.annotatorId); };
  $("#importAnnot").onchange = ev => { for (const f of ev.target.files) { const r = new FileReader(); r.onload = () => { try { const j = JSON.parse(r.result); if (j.annotator_id === S.annotatorId) { Object.assign(S.myAnnot, j.annotations || {}); saveMyAnnot(); toast("загружена МОЯ разметка"); } else { S.pool = S.pool.filter(p => p.annotator_id !== j.annotator_id); S.pool.push(j); toast("подключена разметка: " + j.annotator_id); } applyFilters(); } catch { toast("не JSON"); } }; r.readAsText(f); } };
  $("#exportAnnot").onclick = () => download(`annot_${S.annotatorId}.json`, { annotator_id: S.annotatorId, exported: new Date().toISOString(), tool: "toloka", annotations: S.myAnnot });
  $("#prevBtn").onclick = () => go(-1);
  $("#nextBtn").onclick = () => go(1);
  $("#toFocus").onclick = renderItem;
  $("#ctxRadius").onchange = renderItem;
  ["textSearch", "fSlice", "fBench", "fPrio", "fType", "fMine", "fAgent"].forEach(id => { const e = $("#" + id); e.oninput = e.onchange = applyFilters; });
  document.addEventListener("keydown", ev => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    const it = currentItem(); if (!it) return;
    if (ev.key === "j" || ev.key === "ArrowRight") go(1);
    else if (ev.key === "k" || ev.key === "ArrowLeft") go(-1);
    else if (ev.key === "0") setVerdict(it, NONE);
    else if (ev.key === "u") setVerdict(it, UNCLEAR);
    else if (/^[1-9]$/.test(ev.key)) { const cands = siteTypes(it); const t = cands[parseInt(ev.key) - 1]; if (t) setVerdict(it, t); }
  });
}
function go(d) { if (!S.filtered.length) return; S.idx = (S.idx + d + S.filtered.length) % S.filtered.length; renderList(); renderItem(); }

/* ---------- init ---------- */
(async function init() {
  bind(); loadMyAnnot();
  const cfgTxt = await tryFetch("config.json");
  S.cfg = cfgTxt ? JSON.parse(cfgTxt) : { event_types: "data/event_types.json", conflicts: "data/conflicts.json" };
  const m = await tryFetch(S.cfg.event_types); if (m) try { setModel(JSON.parse(m)); } catch (e) { toast("event_types: " + e.message); }
  const d = await tryFetch(S.cfg.conflicts); if (d) try { setData(JSON.parse(d)); } catch (e) { toast("conflicts: " + e.message); }
  if (!d) toast("не удалось загрузить conflicts.json");
})();
