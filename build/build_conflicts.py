"""
Строит N-way конфликт-корпус для ручной адъюдикации разметок нескольких «агентов»
(regex / Claude / DeepSeek / Qwen) на reasoning-трассах.

Реальность данных (важно, см. README):
  - настоящего 4-угольника на ОДНИХ И ТЕХ ЖЕ трассах нет (пересечение всех четырёх = 0);
  - Claude ∩ DeepSeek = 1693 трассы (модели gemma/qwen)  -> агенты {regex, claude, deepseek};
  - Qwen — остров: 485 gpt-oss-трасс, ни с кем не пересекается -> агенты {regex, qwen}.
Поэтому корпус N-way: на каждой трассе ровно те агенты, кто её реально разметил.

Событие агента = {seg_id, type, quote}. regex пересчитывается ТЕМ ЖЕ детектором, что и в
build_verification.py (detectors.detect_events на склеенном тексте сегментов), чтобы быть
согласованным с уже существующими disputes.

Сайт (site) = кластер событий разных агентов рядом (|seg diff|<=TOL). На сайте у каждого
присутствующего агента есть «ответ»: тип, который он поставил, либо ∅ (не сработал).
Конфликт = ответы агентов на сайте не совпадают. Согласие (все один тип) в корпус не идёт.

Приоритет (informativeness) для ранжирования — что размечать в первую очередь:
  4  type_mismatch между двумя+ LLM-агентами (самое ценное);
  3  presence-split между LLM (один поставил тип, другой молчит);
  2  LLM vs regex рассогласование;
  1  одиночное regex-срабатывание (regex FP-кандидат, шумно, но нужно для score regex).

Выход (в OUTDIR):
  data/conflicts.json     — ранжированный ТОП сайтов (для вьюера), с встроенным context_window;
  data/sites_full.jsonl   — ВСЕ сайты (для честных знаменателей при скоринге);
  data/event_types.json   — копия модели типов;
  data/traces/<cell>__<qid>.json — слим-трассы (segments) для сайтов из топа (ленивая подгрузка);
  data/build_summary.json — статистика.
"""
import json, glob, os, re, argparse, importlib.util, hashlib
from collections import Counter, defaultdict

BASE = "/home/ki/repos/reasoning"
POC = os.path.join(BASE, "internal_signals_poc")
GOLD_CLAUDE = os.path.join(POC, "gold")
GOLD_DEEPSEEK = os.path.join(POC, "gold_deepseek_dual")
GOLD_QWEN = os.path.join(POC, "gold_qwen35_dual")
PAYLOADS = os.path.join(POC, "payloads")
EVENT_TYPES = os.path.join(POC, "verification", "event_types.json")
DETECTORS = os.path.join(BASE, "reasoning_budget/temporal_process_experiments/hawkes_2026_05_12/detectors.py")

TOL = 1  # окно выравнивания событий между агентами (в сегментах)

# короткое имя модели (в именах файлов gold/) -> полное (в payload.model / cell)
MODEL_FULL = {
    "gemma": "gemma-4-26b-a4b-it-nitro",
    "qwen": "qwen3.6-35b-a3b-nitro",
    "gptoss": "gpt-oss-20b",
}

_spec = importlib.util.spec_from_file_location("det", DETECTORS)
det = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(det)


def san_qid(q):
    return re.sub(r'[^A-Za-z0-9]+', '_', q).strip('_')


def load_events(path, quote_key):
    """Читает events из файла разметки -> [{seg_id, type, quote}]."""
    try:
        d = json.load(open(path))
    except Exception:
        return None
    out = []
    for e in d.get("events", []):
        if "seg_id" not in e or "type" not in e:
            continue
        out.append({"seg_id": int(e["seg_id"]), "type": e["type"],
                    "quote": e.get(quote_key) or e.get("trigger_quote") or e.get("match") or ""})
    return out


def payload_for(model_short, bench, qid):
    """grid/<short>__<bench>__<qid>.json для gemma/qwen; <bench>__<qid>.json для gpt-oss."""
    if model_short == "gptoss":
        p = os.path.join(PAYLOADS, f"{bench}__{san_qid(qid)}.json")
        return p if os.path.exists(p) else None
    p = os.path.join(PAYLOADS, "grid", f"{model_short}__{bench}__{san_qid(qid)}.json")
    return p if os.path.exists(p) else None


def regex_events_for(pay, dom):
    """Пересчёт regex-детектором по склейке сегментов -> [{seg_id,type,quote}] (как build_verification)."""
    segs = pay["segments"]
    full = ""
    bounds = []  # (seg_id, start, end)
    for s in segs:
        start = len(full)
        full += s["text"] + " "
        bounds.append((s["seg_id"], start, len(full)))

    def seg_of(cp):
        for sid, a, b in bounds:
            if a <= cp < b:
                return sid
        return bounds[-1][0] if bounds else 0

    out = []
    for e in det.detect_events(full, dom):
        out.append({"seg_id": seg_of(e["char_pos"]), "type": e["type"], "quote": e.get("match", "")})
    return out


def dom_of(pay):
    return pay.get("domain") or ("R" if pay.get("benchmark") in ("hotpotqa", "musique") else "M")


def build_sites(agent_events):
    """
    agent_events: {agent_name: [ev,...]} для одной трассы.
    Union-find по событиям разных агентов в пределах ±TOL сегментов.
    Возвращает список сайтов: {seg_id, per_agent:{agent:[types]}, segs:[...]}.
    """
    flat = []  # (agent, seg_id, type, quote)
    for ag, evs in agent_events.items():
        for e in evs:
            flat.append((ag, e["seg_id"], e["type"], e["quote"]))
    n = len(flat)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # объединяем близкие события РАЗНЫХ агентов (свои события агента не сливаем)
    for i in range(n):
        for j in range(i + 1, n):
            if flat[i][0] != flat[j][0] and abs(flat[i][1] - flat[j][1]) <= TOL:
                union(i, j)

    comps = defaultdict(list)
    for i in range(n):
        comps[find(i)].append(i)

    sites = []
    for members in comps.values():
        per_agent = defaultdict(list)  # agent -> [{type,quote,seg_id}]
        segs = []
        for m in members:
            ag, sid, typ, quote = flat[m]
            per_agent[ag].append({"type": typ, "quote": quote, "seg_id": sid})
            segs.append(sid)
        anchor = Counter(segs).most_common(1)[0][0]
        sites.append({"seg_id": anchor, "per_agent": dict(per_agent), "segs": sorted(set(segs))})
    return sites


def site_answers(site, present):
    """Ответ каждого присутствующего агента: множество типов или {'∅'} если молчал."""
    ans = {}
    for ag in present:
        types = sorted({x["type"] for x in site["per_agent"].get(ag, [])})
        ans[ag] = types if types else ["∅"]
    return ans


def priority(ans, present):
    llm = [a for a in present if a != "regex"]
    llm_typed = [a for a in llm if ans[a] != ["∅"]]
    llm_type_set = {tuple(ans[a]) for a in llm_typed}
    if len(llm_typed) >= 2 and len(llm_type_set) >= 2:
        return 4  # type_mismatch между LLM
    if len(llm) >= 2 and any(ans[a] == ["∅"] for a in llm) and llm_typed:
        return 3  # presence-split между LLM
    if llm_typed and ans.get("regex", ["∅"]) != ["∅"] and \
       any(tuple(ans[a]) != tuple(ans["regex"]) for a in llm_typed):
        return 2  # LLM vs regex
    return 1      # одиночное/regex-only


def window(pay_segs_by_id, seg_id, radius):
    ids = [i for i in range(seg_id - radius, seg_id + radius + 1) if i in pay_segs_by_id]
    return [{"seg_id": i, "text": pay_segs_by_id[i]} for i in ids]


def stable_id(cell, qid, site, ans):
    key = f"{cell}|{san_qid(qid)}|s{site['seg_id']}|" + \
          "|".join(f"{a}:{','.join(ans[a])}" for a in sorted(ans))
    return f"{cell}|{san_qid(qid)}|s{site['seg_id']}|{hashlib.md5(key.encode()).hexdigest()[:8]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE, "toloka"))
    ap.add_argument("--cap", type=int, default=6000, help="сколько сайтов уходит во вьюер (топ по приоритету)")
    ap.add_argument("--radius", type=int, default=12, help="контекст ±R сегментов, встроенный в сайт")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число трасс (отладка)")
    args = ap.parse_args()

    outdata = os.path.join(args.out, "data")
    os.makedirs(outdata, exist_ok=True)

    # ---- список трасс по срезам ----
    def keys_dir(d, strip_model):
        ks = {}
        for f in glob.glob(os.path.join(d, "*.json")):
            b = os.path.basename(f)[:-5]
            if b.startswith("_"):
                continue
            p = b.split("__")
            if strip_model and len(p) == 2:
                ks[(p[0], p[1])] = f          # (bench, qid) -> path (qwen)
            elif not strip_model and len(p) == 3:
                ks[(p[0], p[1], p[2])] = f    # (model, bench, qid) -> path
        return ks

    Kc = keys_dir(GOLD_CLAUDE, False)
    Kd = keys_dir(GOLD_DEEPSEEK, False)
    # qwen: ключ по _meta.trace_model
    Kq = {}
    for f in glob.glob(os.path.join(GOLD_QWEN, "*.json")):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            dq = json.load(open(f))
            m = dq["_meta"]
        except Exception:
            continue
        short = {v: k for k, v in MODEL_FULL.items()}.get(m.get("trace_model"), "gptoss")
        Kq[(short, m.get("benchmark"), dq["question_id"])] = f

    slice_a = sorted(set(Kc) & set(Kd))                 # regex+claude+deepseek
    slice_b = sorted(Kq)                                # regex+qwen
    targets = [("A", k, {"claude": Kc[k], "deepseek": Kd[k]}) for k in slice_a] + \
              [("B", k, {"qwen": Kq[k]}) for k in slice_b]
    if args.limit:
        targets = targets[:args.limit]
    print(f"трасс к обработке: {len(targets)}  (slice A={len(slice_a)}, slice B={len(slice_b)})")

    all_sites = []          # для jsonl
    per_agent_totals = Counter()
    kind_counter = Counter()
    n_traces = 0

    for sl, key, lab_paths in targets:
        model_short, bench, qid = key
        pp = payload_for(model_short, bench, qid)
        if not pp:
            continue
        try:
            pay = json.load(open(pp))
        except Exception:
            continue
        segs = pay["segments"]
        segs_by_id = {s["seg_id"]: s["text"] for s in segs}
        dom = dom_of(pay)
        model_full = MODEL_FULL.get(model_short, model_short)
        cell = f"{model_full}__{bench}"

        agent_events = {"regex": regex_events_for(pay, dom)}
        # claude/deepseek quote = trigger_quote; qwen тоже
        for ag, path in lab_paths.items():
            evs = load_events(path, "trigger_quote")
            if evs is None:
                continue
            # оставляем только события, чей seg_id есть в трассе
            agent_events[ag] = [e for e in evs if e["seg_id"] in segs_by_id]

        present = [a for a in ["regex", "claude", "deepseek", "qwen"] if a in agent_events]
        if len(present) < 2:
            continue
        n_traces += 1

        sites = build_sites(agent_events)
        for site in sites:
            ans = site_answers(site, present)
            distinct = {tuple(v) for v in ans.values()}
            if len(distinct) <= 1:
                continue  # полное согласие — не конфликт
            prio = priority(ans, present)
            item = {
                "item_id": stable_id(cell, qid, site, ans),
                "slice": sl, "model": model_full, "benchmark": bench,
                "question_id": qid, "domain": dom, "cell": cell,
                "seg_id": site["seg_id"], "segs": site["segs"],
                "agents_present": present,
                "per_agent": {a: ans[a] for a in present},
                "quotes": {a: [x["quote"] for x in site["per_agent"].get(a, []) if x["quote"]][:1]
                           for a in present},
                "priority": prio,
                "verdict": None, "corrected_type": None, "notes": None,
            }
            all_sites.append(item)
            kind_counter[f"prio{prio}"] += 1
            for a in present:
                per_agent_totals[a] += 1

    # ---- полный jsonl (НЕ публикуется, лежит в build/, для честных знаменателей скоринга) ----
    full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites_full.jsonl")
    with open(full_path, "w") as f:
        for s in all_sites:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # ---- стратифицированный отбор во вьюер: квота по срезу A/B, внутсреза — тиры приоритета ----
    # slice B (qwen) обязательно представлен, иначе qwen нечем скорить.
    buckets = defaultdict(list)   # (slice, prio) -> [sites]
    for s in all_sites:
        buckets[(s["slice"], s["priority"])].append(s)
    for k in buckets:
        buckets[k].sort(key=lambda s: (s["cell"], s["question_id"], s["seg_id"]))

    quota = {"A": int(args.cap * 0.70), "B": args.cap - int(args.cap * 0.70)}
    frac = {4: 0.45, 3: 0.30, 2: 0.10, 1: 0.15}  # чтобы адъюдицировались ВСЕ режимы ошибок
    top = []
    for sl in ("A", "B"):
        cap_sl = quota[sl]
        used = set()
        # первый проход — по целевым долям тиров
        for prio in (4, 3, 2, 1):
            target = int(cap_sl * frac[prio])
            for s in buckets.get((sl, prio), [])[:target]:
                top.append(s); used.add(s["item_id"])
        # второй проход — добить остаток среза из любых тиров (пустые тиры не мешают)
        left = cap_sl - sum(1 for s in top if s["slice"] == sl)
        if left > 0:
            for prio in (4, 3, 2, 1):
                if left <= 0:
                    break
                for s in buckets.get((sl, prio), []):
                    if s["item_id"] in used:
                        continue
                    top.append(s); used.add(s["item_id"]); left -= 1
                    if left <= 0:
                        break
    top.sort(key=lambda s: (-s["priority"], s["slice"], s["cell"], s["question_id"], s["seg_id"]))

    # встроенный context_window ±radius (self-contained; пофайловые трассы не публикуем)
    payload_cache = {}
    for s in top:
        mshort = {v: k for k, v in MODEL_FULL.items()}.get(s["model"], s["model"])
        pp = payload_for(mshort, s["benchmark"], s["question_id"])
        if pp not in payload_cache:
            payload_cache[pp] = json.load(open(pp))
        pay = payload_cache[pp]
        segs_by_id = {seg["seg_id"]: seg["text"] for seg in pay["segments"]}
        s["context_window"] = window(segs_by_id, s["seg_id"], args.radius)
        s["n_segments"] = len(pay["segments"])

    json.dump(top, open(os.path.join(outdata, "conflicts.json"), "w"), ensure_ascii=False)

    # event_types
    import shutil
    shutil.copy(EVENT_TYPES, os.path.join(outdata, "event_types.json"))

    summary = {
        "traces_processed": n_traces,
        "sites_total": len(all_sites),
        "sites_in_viewer": len(top),
        "by_priority": dict(kind_counter),
        "per_agent_site_participation": dict(per_agent_totals),
        "slices": {"A_regex_claude_deepseek": len(slice_a), "B_regex_qwen": len(slice_b)},
        "note": "4-way на общих трассах отсутствует; см. README",
    }
    json.dump(summary, open(os.path.join(outdata, "build_summary.json"), "w"), ensure_ascii=False, indent=1)
    print("сайтов всего:", len(all_sites), "| во вьюере:", len(top))
    print("по приоритету:", dict(kind_counter))
    print("участие агентов:", dict(per_agent_totals))


if __name__ == "__main__":
    main()
