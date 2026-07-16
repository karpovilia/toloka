"use strict";
/* jsdom-смоук для Toloka v2 (верификация событий на трассе).
   Стабит fetch (config, event_types, traces_index, trace_maps_meta, trace-файлы),
   проверяет: список трасс; авто-открытие; кандидаты+инлайн-чипы; ✓/✗ пишут вердикт;
   навигация j/k; retype; прогресс; deep-link на трассу; экспорт; GitHub PUT. */
const fs = require("fs"), path = require("path");
const { JSDOM } = require(path.join("/home/ki/repos/reasoning/internal_signals_poc/trace_verifier/node_modules/jsdom"));

const DIR = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(DIR, "index.html"), "utf8");
const appjs = fs.readFileSync(path.join(DIR, "app.js"), "utf8");

const ET = { domains: { M: { types: { backtrack: { definition: "реверс хода рассуждения" }, verify: { definition: "проверка полученного" }, commit: { definition: "финальный ответ" }, subgoal_done: {}, branch: {}, failed_attempt: {} } } } };
const MM = { hawkes_by_type: { backtrack: { mu: 0.08, alpha: 0.49, beta: 1.5 }, verify: { mu: 0.04, alpha: 0.49, beta: 1.5 }, commit: { mu: 0.005, alpha: 0.86, beta: 0.16 } }, floor: 0.12, lam_max: 3.0, operators: ["DERIVING", "CONCLUDING"] };
function trace(tf, agents, events) {
  const maxs = Math.max(...events.map(e => e.s)) + 4;
  const segments = []; for (let i = 0; i <= maxs; i++) segments.push({ seg_id: i, text: "seg " + i });
  const spans = [{ a: 0, b: Math.ceil(maxs / 2), op: "DERIVING" }, { a: Math.ceil(maxs / 2), b: maxs + 1, op: "CONCLUDING" }];
  const lam = segments.map(() => 0.3);
  return { cell: tf.replace(".json", ""), question_id: tf, benchmark: "gpqa_diamond", domain: "M", segments, events, spans, lam, agents };
}
const T1 = trace("t1.json", ["regex", "claude", "deepseek"], [
  { s: 3, t: "backtrack", a: "claude" }, { s: 3, t: "backtrack", a: "deepseek" },
  { s: 8, t: "verify", a: "regex" }, { s: 12, t: "commit", a: "claude" }]);   // 3 кандидата
const T2 = trace("t2.json", ["regex", "qwen"], [
  { s: 2, t: "backtrack", a: "qwen" }, { s: 2, t: "backtrack", a: "regex" }]);   // 1 кандидат
const INDEX = [
  { trace_file: "t1.json", cell: "gemma__gpqa_diamond", question_id: "q1", benchmark: "gpqa_diamond", domain: "M", model: "gemma", slice: "A", n_segments: T1.segments.length, n_events: 4, n_candidates: 3, agents: T1.agents },
  { trace_file: "t2.json", cell: "gptoss__gpqa_diamond", question_id: "q2", benchmark: "gpqa_diamond", domain: "M", model: "gptoss", slice: "B", n_segments: T2.segments.length, n_events: 2, n_candidates: 1, agents: T2.agents },
];
const CFG = { event_types: "ET", traces_index: "IX", traces_dir: "data/traces", trace_maps_meta: "MM" };
const routes = { "config.json": CFG, "ET": ET, "IX": INDEX, "MM": MM, "t1.json": T1, "t2.json": T2 };

let fail = 0; const ok = (c, m) => { console.log((c ? "  ok  " : " FAIL ") + m); if (!c) fail++; };
const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "http://localhost/" });
const { window } = dom; global.window = window; global.document = window.document;
let ghPut = null;
window.fetch = (u, opts) => {
  if (String(u).includes("api.github.com")) { if (opts && opts.method === "PUT") { ghPut = JSON.parse(opts.body); return Promise.resolve({ ok: true, json: () => Promise.resolve({ commit: { sha: "abc1234" } }) }); } return Promise.resolve({ ok: false, status: 404 }); }
  const key = Object.keys(routes).find(k => u === k || u.endsWith(k));
  return Promise.resolve({ ok: key != null, status: key ? 200 : 404, text: () => Promise.resolve(JSON.stringify(routes[key])) });
};
window.btoa = s => Buffer.from(s, "binary").toString("base64");
const store = window.localStorage;
try { window.URL.createObjectURL = () => "blob:x"; window.URL.revokeObjectURL = () => {}; } catch {}
let lastExport = null; window.HTMLAnchorElement.prototype.click = function () { lastExport = this.download; };
window.eval(appjs);
const $ = s => window.document.querySelector(s), $$ = s => [...window.document.querySelectorAll(s)];
const key = k => window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true }));

setTimeout(() => {
  // 1) список трасс + авто-открытие первой
  ok($$("#traceList li").length === 2, "список трасс: " + $$("#traceList li").length);
  ok($("#qLabel").textContent.includes("q1"), "открыта первая трасса: " + $("#qLabel").textContent.slice(0, 40));
  // 2) кандидаты + инлайн-чипы (3)
  ok($$("#traceBody .ev").length === 3, "инлайн-события: " + $$("#traceBody .ev").length);
  ok($("#curEvent .ce-pos").textContent.includes("1 / 3"), "текущее событие 1/3: " + $("#curEvent .ce-pos").textContent);
  // клик по гуттеру -> попап показывает операторный спан точки
  const gut = $('#traceBody .seg[data-seg-id="3"] .gutter');
  if (gut) gut.dispatchEvent(new window.MouseEvent("click", { bubbles: true, clientX: 80, clientY: 80 }));
  const opchip = $("#drillpop .opchip");
  ok(opchip && opchip.textContent === "DERIVING", "клик по карте показал спан: " + (opchip ? opchip.textContent : "нет"));
  // тултипы: определение события в правой панели + тултип спана на opband
  ok($("#curEvent .ce-def") && $("#curEvent .ce-def").textContent.includes("реверс"), "определение события в панели: " + ($("#curEvent .ce-def") ? $("#curEvent .ce-def").textContent : "нет"));
  ok($$("#traceBody .opband").some(b => (b.title || "").includes("DERIVING —")), "тултип спана на карте (что кодирует)");
  ok(opchip && opchip.title.includes("DERIVING —"), "тултип на чипе спана в попапе: " + (opchip ? opchip.title : ""));
  ok($("#drillpop .dlink"), "в попапе есть кнопка ссылки на строку");
  if ($("#drillpop .dlink")) { $("#drillpop .dlink").dispatchEvent(new window.MouseEvent("click", { bubbles: true })); ok(window.location.hash.includes("seg=3"), "кнопка попапа скопировала ссылку на строку 3: " + window.location.hash); }
  // ссылка на строку: клик по номеру строки -> hash seg= + подсветка linked
  const sid5 = $('#traceBody .seg[data-seg-id="5"] .sid');
  if (sid5) sid5.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  ok(window.location.hash.includes("seg=5"), "ссылка на строку: hash = " + window.location.hash);
  ok($('#traceBody .seg[data-seg-id="5"]').classList.contains("linked"), "строка 5 подсвечена linked");
  // сворачивание левого списка
  $("#listToggle").click();
  ok($("#main").classList.contains("nolist"), "левый список свёрнут");
  $("#listToggle").click();
  // 3) подтверждаем первое (клавиша 1) -> вердикт ✓ записан, прогресс
  key("1");
  const a1 = JSON.parse(store.getItem("tv_annot::ki") || "{}");
  ok(a1["t1.json|s3|backtrack"] && a1["t1.json|s3|backtrack"].verdict === "✓", "✓ записан: " + JSON.stringify(a1["t1.json|s3|backtrack"]));
  ok($("#traceProgress").textContent.includes("1 / 3"), "прогресс трассы 1/3: " + $("#traceProgress").textContent);
  ok($$("#traceBody .ev.done").length === 1, "чип помечен done: " + $$("#traceBody .ev.done").length);
  // 4) навигация j -> событие 2, отклоняем (клавиша 2)
  key("j");
  ok($("#curEvent .ce-pos").textContent.includes("2 / 3"), "перешли к 2/3: " + $("#curEvent .ce-pos").textContent);
  key("2");
  const a2 = JSON.parse(store.getItem("tv_annot::ki") || "{}");
  ok(a2["t1.json|s8|verify"] && a2["t1.json|s8|verify"].verdict === "✗", "✗ записан для verify@8");
  // 5) retype третьего через правую панель
  key("j");
  const sel = $("#curEvent .ce-retype"); sel.value = "subgoal_done"; sel.dispatchEvent(new window.Event("change"));
  const a3 = JSON.parse(store.getItem("tv_annot::ki") || "{}");
  ok(a3["t1.json|s12|commit"] && a3["t1.json|s12|commit"].verdict === "subgoal_done", "retype commit->subgoal_done: " + JSON.stringify(a3["t1.json|s12|commit"]));
  // 6) экспорт
  $("#exportAnnot").click();
  ok(lastExport === "annot_ki.json", "экспорт: " + lastExport);
  // 7) deep-link на t2
  window.location.hash = "#trace=t2.json";
  window.dispatchEvent(new window.Event("hashchange"));
  setTimeout(() => {
    ok($("#qLabel").textContent.includes("q2"), "deep-link открыл t2: " + $("#qLabel").textContent.slice(0, 40));
    ok($$("#traceBody .ev").length === 1, "t2: 1 кандидат (regex+qwen merged): " + $$("#traceBody .ev").length);
    // 8) GitHub PUT
    $("#ghOwner").value = "karpovilia"; $("#ghRepo").value = "toloka"; $("#ghToken").value = "github_pat_x";
    $("#ghCommit").click();
    setTimeout(() => {
      ok(ghPut && ghPut.content, "GitHub PUT ушёл");
      if (ghPut) { const dec = JSON.parse(Buffer.from(ghPut.content, "base64").toString("utf8")); ok(dec.tool === "toloka-v2" && dec.annotations, "PUT-контент = annot v2"); }
      console.log(fail ? `\nFAILED (${fail})` : "\nALL OK");
      process.exit(fail ? 1 : 0);
    }, 120);
  }, 80);
}, 400);
