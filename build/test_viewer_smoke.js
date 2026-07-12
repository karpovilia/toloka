"use strict";
/* jsdom-смоук для нового app.js (Toloka N-way conflict viewer).
   Грузит index.html + app.js в jsdom, стабит fetch (config, event_types, 2 реальных сайта),
   проверяет: данные подтянулись; список и карточки агентов отрисованы; вердикт проставляется
   и переключает match/miss на карточках; клавиши (j, 0, цифры) работают; экспорт формирует JSON. */
const fs = require("fs"), path = require("path");
const { JSDOM } = require(path.join("/home/ki/repos/reasoning/internal_signals_poc/trace_verifier/node_modules/jsdom"));

const DIR = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(DIR, "index.html"), "utf8");
const appjs = fs.readFileSync(path.join(DIR, "app.js"), "utf8");
const ET = JSON.parse(fs.readFileSync(path.join(DIR, "data/event_types.json"), "utf8"));
const ITEMS = JSON.parse(fs.readFileSync(path.join(__dirname, "_smoke_items.json"), "utf8"));
const CFG = { event_types: "ET", conflicts: "CF" };

const routes = { "config.json": CFG, "ET": ET, "CF": ITEMS };
let fail = 0;
function ok(c, m) { console.log((c ? "  ok  " : " FAIL ") + m); if (!c) fail++; }

const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "http://localhost/" });
const { window } = dom;
global.window = window; global.document = window.document;
window.fetch = (u) => {
  const key = Object.keys(routes).find(k => u === k || u.endsWith(k));
  return Promise.resolve({ ok: key != null, text: () => Promise.resolve(JSON.stringify(routes[key])) });
};
// jsdom даёт рабочий localStorage (url задан); только читаем его в проверках
const store = window.localStorage;
try { window.URL.createObjectURL = () => "blob:x"; window.URL.revokeObjectURL = () => {}; } catch {}
let lastExport = null;
const realCreate = window.document.createElement.bind(window.document);
window.HTMLAnchorElement.prototype.click = function () { lastExport = this.download; };

window.eval(appjs);

function key(k) { const ev = new window.KeyboardEvent("keydown", { key: k, bubbles: true }); window.document.dispatchEvent(ev); }
const $ = s => window.document.querySelector(s);
const $$ = s => [...window.document.querySelectorAll(s)];

setTimeout(() => {
  // 1) данные подтянулись
  ok($("#filterCount").textContent.includes("из 2"), "загружено 2 конфликта: " + $("#filterCount").textContent);
  ok($$("#exampleList li").length >= 2, "список: " + $$("#exampleList li").length + " элементов");

  // 2) карточки агентов первого сайта (regex+claude+deepseek)
  const names = $$("#agents .aname").map(e => e.textContent);
  ok(names.length === 3 && names.includes("Claude") && names.includes("DeepSeek"), "3 карточки агентов: " + names.join(","));
  ok($$("#agents .atype").length >= 3, "типы на карточках отрисованы: " + $$("#agents .atype").length);

  // 3) вердикт: жмём цифру 1 (первый кандидат-тип) -> запись появилась, карточки получили match/miss
  const cands = $$("#verdict .vbtn.cand").map(b => b.textContent);
  ok(cands.length >= 1, "кандидат-кнопки: " + cands.join(" | "));
  key("1");
  const rec = JSON.parse(store.getItem("tv_annot::ki") || "{}");
  const firstId = ITEMS[0].item_id;
  ok(rec[firstId] && rec[firstId].verdict, "вердикт записан: " + JSON.stringify(rec[firstId]));
  ok($$("#agents .agent.match").length + $$("#agents .agent.miss").length === 3, "карточки размечены match/miss: " +
     $$("#agents .agent.match").length + " match / " + $$("#agents .agent.miss").length + " miss");
  ok($("#progress").textContent.includes("1 /"), "прогресс обновился: " + $("#progress").textContent);

  // 4) навигация к сайту 2 (qwen) и вердикт '∅'
  key("j");
  const names2 = $$("#agents .aname").map(e => e.textContent);
  ok(names2.length === 2 && names2.includes("Qwen"), "второй сайт: агенты " + names2.join(","));
  key("0");
  const rec2 = JSON.parse(store.getItem("tv_annot::ki") || "{}");
  ok(rec2[ITEMS[1].item_id] && rec2[ITEMS[1].item_id].verdict === "∅", "∅-вердикт на qwen-сайте записан");

  // 5) экспорт
  $("#exportAnnot").click();
  ok(lastExport === "annot_ki.json", "экспорт вызвал скачивание: " + lastExport);

  console.log(fail ? `\nFAILED (${fail})` : "\nALL OK");
  process.exit(fail ? 1 : 0);
}, 500);
