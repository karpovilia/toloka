"""
Строит N-way конфликт-корпус для ручной адъюдикации разметок нескольких «агентов»
(regex / Claude / DeepSeek / Qwen) на reasoning-трассах.

СНЯТ С ПУБЛИКАЦИИ 2026-08-13. Корпус, который строит этот скрипт, публиковал 170 трасс
закрытого GPQA-diamond, поэтому он вынесен из вьюера в приватный архив
internal_signals_poc/toloka_archive_2026_08_13, а верифицируется людьми только срез
native-PO (build_nativepo.py). Модуль остаётся как библиотека: build_nativepo.py берёт
отсюда чтение payload, регулярочный детектор, сборку сайтов и канонизацию идентификаторов.
Запуск как скрипта запрещён — он бы вернул старый корпус в публичный снимок и удалил из
data/traces чужие для него файлы среза native-PO.

Реальность данных (важно, см. README): корпус N-way, и на каждой трассе присутствуют
ровно те LLM-разметчики, которые действительно её обрабатывали. После полного Qwen-прогона
есть непустые пересечения CDQ/CDQR; R означает DeepSeek-R1.

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

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.abspath(os.environ.get("REASONING_ROOT", os.path.dirname(TOOL_ROOT)))
POC = os.path.join(BASE, "internal_signals_poc")
GOLD_CLAUDE = os.path.join(POC, "gold")
GOLD_DEEPSEEK = os.path.join(POC, "gold_deepseek_dual")
GOLD_QWEN = os.path.join(POC, "gold_qwen35_dual")
GOLD_R1 = os.path.join(POC, "gold_deepseek_r1_dual_short")  # deepseek-reasoner (R1) перепроход
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


def canonical_key(model_short, bench, qid):
    """Единый ключ независимо от qid в виде path/to/id.json или path_to_id_json."""
    return model_short, bench, san_qid(qid)


def annotation_key(path):
    """Возвращает канонический (model, benchmark, qid) для любого формата gold-файла.

    В корпусе сосуществуют ``model__benchmark__qid.json`` для grid-трасс и
    ``benchmark__qid.json`` для gpt-oss. Явный префикс имени надёжнее старых
    ``_meta.trace_model`` (в 300 Claude-файлах это поле ошибочно равно gpt-oss).
    Имена уже санитизированы сборщиком payload, поэтому для индекса JSON читать не нужно.
    """
    stem = os.path.basename(path)[:-5]
    parts = stem.split("__", 2)
    prefixed = parts[0] in MODEL_FULL and len(parts) == 3
    model_short = parts[0] if prefixed else "gptoss"
    benchmark = parts[1] if prefixed else parts[0]
    qid = parts[2] if prefixed else stem.split("__", 1)[1] if "__" in stem else ""
    if not benchmark or not qid:
        raise RuntimeError(f"в gold-файле нет benchmark/question_id: {path}")
    return canonical_key(model_short, benchmark, qid)


def annotation_index(directory, agent):
    """Индексирует gold-каталог и останавливается на канонических коллизиях."""
    index = {}
    for path in glob.glob(os.path.join(directory, "*.json")):
        if os.path.basename(path).startswith("_"):
            continue
        key = annotation_key(path)
        old = index.get(key)
        if old and os.path.realpath(old) != os.path.realpath(path):
            raise RuntimeError(f"коллизия файлов {agent} для канонического ключа {key}: {old} / {path}")
        index[key] = path
    return index


def load_events(path, quote_key):
    """Читает events из файла разметки -> [{seg_id, type, quote}]."""
    try:
        d = json.load(open(path))
    except Exception as exc:
        raise RuntimeError(f"не удалось прочитать разметку {path}: {exc}") from exc
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


_ALLOWED_TYPES = {}


def allowed_types(dom):
    """Типы, определённые моделью событий для домена трассы.

    LLM-разметчик иногда ставит тип чужого домена (например branch домена M на
    retrieval-трассе домена R). Вьюер такой тип отрисовать не может — событие
    отбрасывается на сборке, чтобы снимок оставался согласован с event_types.json.
    """
    if not _ALLOWED_TYPES:
        model = json.load(open(EVENT_TYPES))
        for name, definition in (model.get("domains") or {}).items():
            _ALLOWED_TYPES[name] = set((definition.get("types") or {}).keys())
    return _ALLOWED_TYPES.get(dom, set())


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


def stable_id(cell, qid, site, ans, seen=None):
    """Идентификатор сайта. Один агент может сработать на сегменте дважды одним типом —
    базовый ключ тогда совпадает, и повтору дописывается порядковый суффикс, иначе
    item_id перестаёт быть первичным ключом корпуса."""
    key = f"{cell}|{san_qid(qid)}|s{site['seg_id']}|" + \
          "|".join(f"{a}:{','.join(ans[a])}" for a in sorted(ans))
    base = f"{cell}|{san_qid(qid)}|s{site['seg_id']}|{hashlib.md5(key.encode()).hexdigest()[:8]}"
    if seen is None:
        return base
    n = seen[base]
    seen[base] += 1
    return base if n == 0 else f"{base}#{n + 1}"


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
    Kc = annotation_index(GOLD_CLAUDE, "claude")
    Kd = annotation_index(GOLD_DEEPSEEK, "deepseek")
    Kr = annotation_index(GOLD_R1, "r1")
    Kq = annotation_index(GOLD_QWEN, "qwen")

    # Срез = множество LLM-агентов, реально разметивших трассу (C=Claude, D=DeepSeek, Q=Qwen);
    # regex есть везде. После полного Qwen-прогона корпуса (2026-07) 4-way (CDQ) непуст.
    INITIAL = {"claude": "C", "deepseek": "D", "qwen": "Q", "r1": "R"}
    lab_by_key = {}
    for k in set(Kc) | set(Kd) | set(Kq) | set(Kr):
        d = {}
        if k in Kc:
            d["claude"] = Kc[k]
        if k in Kd:
            d["deepseek"] = Kd[k]
        if k in Kq:
            d["qwen"] = Kq[k]
        if k in Kr:
            d["r1"] = Kr[k]
        lab_by_key[k] = d
    targets = [("".join(sorted(INITIAL[a] for a in d)), k, d)
               for k, d in sorted(lab_by_key.items())]
    if args.limit:
        targets = targets[:args.limit]
    slice_traces = Counter(sl for sl, _, _ in targets)
    print(f"трасс к обработке: {len(targets)}  срезы: {dict(slice_traces)}")

    all_sites = []          # для jsonl
    per_agent_totals = Counter()
    kind_counter = Counter()
    seen_ids = Counter()    # база item_id -> сколько раз уже встречалась
    off_domain = Counter()  # (домен, тип, агент) -> сколько событий чужого домена отброшено
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

        allowed = allowed_types(dom)
        agent_events = {"regex": regex_events_for(pay, dom)}
        # claude/deepseek quote = trigger_quote; qwen тоже
        for ag, path in lab_paths.items():
            evs = load_events(path, "trigger_quote")
            if evs is None:
                continue
            # оставляем только события, чей seg_id есть в трассе и чей тип определён в домене
            kept = []
            for e in evs:
                if e["seg_id"] not in segs_by_id:
                    continue
                if allowed and e["type"] not in allowed:
                    off_domain[(dom, e["type"], ag)] += 1
                    continue
                kept.append(e)
            agent_events[ag] = kept

        present = [a for a in ["regex", "claude", "deepseek", "qwen", "r1"] if a in agent_events]
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
                "item_id": stable_id(cell, qid, site, ans, seen_ids),
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

    # ---- стратифицированный отбор во вьюер: квота по срезу, внутри — тиры приоритета ----
    buckets = defaultdict(list)   # (slice, prio) -> [sites]
    for s in all_sites:
        buckets[(s["slice"], s["priority"])].append(s)

    def interleave_by_cell(sites):
        """Round-robin по cell: иначе кап срезает хвост алфавита (musique, qwen-модели)."""
        by_cell = defaultdict(list)
        for s in sites:
            by_cell[s["cell"]].append(s)
        for c in by_cell:
            by_cell[c].sort(key=lambda s: (s["question_id"], s["seg_id"]))
        out, cells = [], sorted(by_cell)
        while len(out) < len(sites):
            for c in cells:
                if by_cell[c]:
                    out.append(by_cell[c].pop(0))
        return out

    for k in buckets:
        buckets[k] = interleave_by_cell(buckets[k])

    # Квота среза: пол MIN_SLICE каждому (чтобы скорить всех агентов), остаток — пропорционально
    # числу LLM-vs-LLM конфликтов (prio>=3): срезы с одним LLM (Q, D) дают только шумные prio1/2
    # и не должны съедать вьюер объёмом.
    slice_sites = Counter(s["slice"] for s in all_sites)
    slice_hi = Counter(s["slice"] for s in all_sites if s["priority"] >= 3)
    slices = sorted(slice_sites)
    MIN_SLICE = 300
    target_total = min(args.cap, sum(slice_sites.values()))
    quota = {sl: 0 for sl in slices}
    # При маленьком --cap раздаём базовую квоту round-robin, не превышая общий cap.
    for _ in range(MIN_SLICE):
        for sl in slices:
            if sum(quota.values()) >= target_total:
                break
            if quota[sl] < slice_sites[sl]:
                quota[sl] += 1
    # Остаток — пропорционально числу содержательных LLM-vs-LLM конфликтов.
    while sum(quota.values()) < target_total:
        eligible = [sl for sl in slices if quota[sl] < slice_sites[sl]]
        if not eligible:
            break
        weight_sum = sum(slice_hi[sl] for sl in eligible)
        before = sum(quota.values())
        room = target_total - before
        if weight_sum:
            for sl in eligible:
                add = min(slice_sites[sl] - quota[sl], int(room * slice_hi[sl] / weight_sum))
                quota[sl] += add
        # Округлённый остаток (или все нулевые веса) раздаём детерминированно.
        if sum(quota.values()) == before:
            for sl in sorted(eligible, key=lambda x: (-slice_hi[x], x)):
                if sum(quota.values()) >= target_total:
                    break
                quota[sl] += 1
    frac = {4: 0.45, 3: 0.30, 2: 0.10, 1: 0.15}  # чтобы адъюдицировались ВСЕ режимы ошибок
    top = []
    for sl in slices:
        cap_sl = min(quota[sl], slice_sites[sl])
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

    # встроенный context_window ±radius (мгновенная отрисовка) + слим-трасса целиком
    # (data/traces/<cell>__<qid>.json, segments) для ленивой подгрузки контекста по скроллу.
    outtr = os.path.join(outdata, "traces")
    os.makedirs(outtr, exist_ok=True)
    payload_cache = {}
    written = set()
    for s in top:
        mshort = {v: k for k, v in MODEL_FULL.items()}.get(s["model"], s["model"])
        pp = payload_for(mshort, s["benchmark"], s["question_id"])
        if pp not in payload_cache:
            payload_cache[pp] = json.load(open(pp))
        pay = payload_cache[pp]
        segs_by_id = {seg["seg_id"]: seg["text"] for seg in pay["segments"]}
        s["context_window"] = window(segs_by_id, s["seg_id"], args.radius)
        s["n_segments"] = len(pay["segments"])
        s["trace_file"] = f"{s['cell']}__{san_qid(s['question_id'])}.json"
        tpath = os.path.join(outtr, s["trace_file"])
        if s["trace_file"] not in written:
            json.dump({"cell": s["cell"], "question_id": s["question_id"],
                       "benchmark": s["benchmark"], "domain": s["domain"],
                       "question": pay.get("question", ""),
                       "segments": [{"seg_id": seg["seg_id"], "text": seg["text"]} for seg in pay["segments"]]},
                      open(tpath, "w"), ensure_ascii=False)
        written.add(s["trace_file"])

    # data/traces — генерируемый снимок ровно текущей выборки. Без очистки старые
    # файлы попадали в traces_index и раздували вьюер после каждой пересборки.
    stale = []
    for tpath in glob.glob(os.path.join(outtr, "*.json")):
        if os.path.basename(tpath) not in written:
            os.unlink(tpath)
            stale.append(os.path.basename(tpath))

    json.dump(top, open(os.path.join(outdata, "conflicts.json"), "w"), ensure_ascii=False)

    # event_types
    import shutil
    shutil.copy(EVENT_TYPES, os.path.join(outdata, "event_types.json"))

    summary = {
        "traces_processed": n_traces,
        "sites_total": len(all_sites),
        "sites_in_viewer": len(top),
        "traces_in_viewer": len(written),
        "stale_traces_removed": len(stale),
        "off_domain_events_dropped": sum(off_domain.values()),
        "by_priority": dict(kind_counter),
        "per_agent_site_participation": dict(per_agent_totals),
        "slices_traces": dict(slice_traces),
        "slices_sites_selected": dict(Counter(s["slice"] for s in top)),
        "note": "срез = множество LLM-агентов трассы (C=Claude, D=DeepSeek, Q=Qwen; regex везде); 4-way CDQ непуст после полного Qwen-прогона корпуса",
    }
    json.dump(summary, open(os.path.join(outdata, "build_summary.json"), "w"), ensure_ascii=False, indent=1)
    print("сайтов всего:", len(all_sites), "| во вьюере:", len(top),
          "| трасс:", len(written), "| удалено устаревших:", len(stale))
    print("по приоритету:", dict(kind_counter))
    print("участие агентов:", dict(per_agent_totals))
    if off_domain:
        print("отброшено событий чужого домена:", sum(off_domain.values()),
              {f"{d}/{t}/{a}": n for (d, t, a), n in off_domain.most_common(10)})


if __name__ == "__main__":
    raise SystemExit(
        "старый конфликт-корпус снят с публикации 2026-08-13 (в нём 170 трасс закрытого "
        "GPQA-diamond). Его копия — internal_signals_poc/toloka_archive_2026_08_13, "
        "публикуется только срез native-PO: python3 build/build_nativepo.py. "
        "Модуль доступен как библиотека для build_nativepo.py; если корпус всё же нужно "
        "пересобрать локально, вызывайте main() явно и не коммитьте результат."
    )
