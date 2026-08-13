"""
Срез конфликтов «судья против regex» на корпусе native-PO (задание 2026-08-11).

Что это. На корпусе native_po_2026_08_11 восемь моделей четырёх семейств прошли один
и тот же набор вопросов в базовом плече свободного рассуждения (лимит 8192 токена, без
принудительной остановки). Каждую трассу разметил судья deepseek-chat (события + операторные
спаны, каталог internal_signals_poc/native_po_2026_08_11/judge_labels/), а второй слой —
канонический регулярочный детектор detectors.detect_events, который здесь пересчитывается
по склейке сегментов ровно так же, как в build_conflicts.py. Новизна среза по сравнению со
старым корпусом вьюера: он мультимодельный — источник трассы (какая модель рассуждала)
указан в каждой трассе, поэтому расхождение слоёв можно смотреть по моделям.

Конфликт-сайт = кластер событий двух слоёв в окне ±1 сегмента, на котором ответы слоёв
не совпадают. Виды конфликта и приоритет адъюдикации:
  4  type_mismatch — сработали оба слоя, но типы разные;
  3  judge_only    — судья видит событие, регулярки молчат (кандидат в пропуск регулярок);
  2  regex_only    — сработали регулярки, судья события не видит (кандидат в ложное срабатывание).
Внутри вида сайты упорядочены по типу события: первыми идут типы, на которых слои расходятся
сильнее всего (порядок берётся из отношения разбросов между моделями regex/судья в
analysis_2026_08_12/outputs/judge_vs_regex.json; там backtrack даёт разброс x88.5 у регулярок
против x1.6 у судьи).

GPQA-diamond НЕ ПУБЛИКУЕТСЯ. Репозиторий и Pages публичные, а условия доступа к GPQA-diamond
закрытые, поэтому трассы по вопросам этого бенчмарка исключаются целиком — ни текста вопроса,
ни текста рассуждения. Остаются MATH500 и BBH, оба публичные. Проверка: build/check_no_gpqa.py.

С 2026-08-13 это единственный корпус вьюера: старый N-way конфликт-корпус (агенты
Claude/DeepSeek/Qwen/DeepSeek-R1) снят с публикации, потому что публиковал 170 трасс
закрытого GPQA-diamond, и унесён в приватный архив
internal_signals_poc/toloka_archive_2026_08_13. Скрипт идемпотентен: свои артефакты
(data/traces/npo-*.json и строки среза в data/conflicts.json) он сначала удаляет, потом
пишет заново. Порядок сборки:
  python3 build/build_nativepo.py
  python3 build/build_traces_index.py     # индекс вьюера
  python3 build/validate_data.py
  python3 build/check_no_gpqa.py          # запрет публикации GPQA на всём снимке
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

BUILD = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BUILD)
import build_conflicts as bc          # noqa: E402  payload/regex/сайты/идентификаторы
import build_trace_maps as btm        # noqa: E402  lam_total по зафиксированным параметрам Хокса

TOOL_ROOT = bc.TOOL_ROOT
DATA = os.path.join(TOOL_ROOT, "data")
TR = os.path.join(DATA, "traces")
ARM = os.path.join(bc.POC, "native_po_2026_08_11")
JUDGE_LABELS = os.path.join(ARM, "judge_labels")
JUDGE_PAYLOADS = os.path.join(ARM, "judge_payloads")
JUDGE_VS_REGEX = os.path.join(ARM, "analysis_2026_08_12", "outputs", "judge_vs_regex.json")

CORPUS = "native_po_2026_08_11"
SLICE = "npoJ"                 # J = судья deepseek-chat; регулярки есть на каждой трассе
PREFIX = "npo-"                # префикс модели в cell -> и в имени файла трассы
JUDGE_AGENT = "judge"
DROP_BENCHMARKS = {"gpqa_diamond"}   # закрытый доступ, публиковать нельзя
DOMAIN = "M"

# тег корпуса -> человекочитаемое имя модели (npo_common.MODELS)
TAG_LABEL = {
    "gptoss20": "gpt-oss-20b",
    "gptoss120": "gpt-oss-120b",
    "qwen3_8b": "Qwen3-8B",
    "qwen3_4b": "Qwen3-4B",
    "qwen35_122b": "Qwen3.5-122B-A10B",
    "r1q7b": "R1-Distill-Qwen-7B",
    "r1l8b": "R1-Distill-Llama-8B",
    "r1l70b": "R1-Distill-Llama-70B",
}
ORDER = ["gptoss20", "gptoss120", "qwen3_8b", "qwen3_4b", "qwen35_122b",
         "r1q7b", "r1l8b", "r1l70b"]
KIND_PRIORITY = {"type_mismatch": 4, "judge_only": 3, "regex_only": 2}


def type_rank():
    """Типы событий по силе расхождения слоёв: отношение разбросов между моделями.

    Разброс = max/min плотности типа по восьми моделям. У регулярок он огромен там, где
    лексический маркер зависит от стиля модели (backtrack), у судьи почти отсутствует.
    Чем больше отношение, тем раньше тип идёт в очереди на адъюдикацию.
    """
    try:
        per_type = json.load(open(JUDGE_VS_REGEX))["per_type"]
    except Exception:
        return {}
    score = {t: v["regex_spread"] / max(1e-9, v["judge_spread"]) for t, v in per_type.items()}
    return {t: i for i, t in enumerate(sorted(score, key=lambda x: -score[x]))}


def load_trace(tag, payload_path):
    """Читает пару payload + судейская разметка. Возвращает None для непубликуемых трасс."""
    pay = json.load(open(payload_path, encoding="utf-8"))
    if pay.get("benchmark") in DROP_BENCHMARKS:
        return None
    label_path = os.path.join(JUDGE_LABELS, tag, os.path.basename(payload_path))
    if not os.path.exists(label_path):
        return None
    lab = json.load(open(label_path, encoding="utf-8"))
    segs = pay.get("segments") or []
    if not segs:
        return None
    seg_ids = [s["seg_id"] for s in segs]
    if seg_ids != list(range(len(segs))):
        raise RuntimeError(f"{payload_path}: seg_id не непрерывная ось 0..N-1")
    return pay, lab


def judge_events(lab, n_segments, allowed, drops):
    out = []
    for e in lab.get("events", []):
        try:
            seg = int(e.get("seg_id"))
        except (TypeError, ValueError):
            drops["bad_seg"] += 1
            continue
        if not 0 <= seg < n_segments:
            drops["out_of_range"] += 1
            continue
        if e.get("type") not in allowed:
            drops["bad_type"] += 1
            continue
        out.append({"seg_id": seg, "type": e["type"], "quote": e.get("trigger_quote") or ""})
    return out


def judge_spans(lab, n_segments, drops):
    out = []
    for s in lab.get("spans", []):
        try:
            a, b = int(s["seg_start"]), int(s["seg_end"])
        except (KeyError, TypeError, ValueError):
            drops["bad_bounds"] += 1
            continue
        a = max(0, min(a, n_segments - 1))
        b = max(a + 1, min(b, n_segments))
        op = s.get("operator")
        if not op:
            drops["bad_operator"] += 1
            continue
        out.append({"a": a, "b": b, "op": op})
    return sorted(out, key=lambda x: x["a"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="ограничить число трасс на модель (отладка)")
    args = ap.parse_args()

    allowed = bc.allowed_types(DOMAIN)
    ranks = type_rank()
    hawkes = json.load(open(os.path.join(DATA, "trace_maps_meta.json")))["hawkes_by_type"]

    # --- 1. чистим прошлый прогон: свои трассы и свои строки конфликтов ---
    old_traces = [p for p in glob.glob(os.path.join(TR, PREFIX + "*.json"))]
    for p in old_traces:
        os.unlink(p)
    conflicts = json.load(open(os.path.join(DATA, "conflicts.json")))
    legacy_conflicts = [c for c in conflicts if c.get("corpus") != CORPUS]

    # --- 2. проходим корпус ---
    seen_ids = Counter()
    sites_out, trace_rows = [], {}
    ev_drops, sp_drops = Counter(), Counter()
    stat_kind, stat_model, stat_type = Counter(), Counter(), Counter()
    judge_ev, regex_ev = Counter(), Counter()
    n_traces = n_dropped_gpqa = 0
    benchmarks = Counter()

    for tag in ORDER:
        label = TAG_LABEL[tag]
        model = PREFIX + label
        files = sorted(glob.glob(os.path.join(JUDGE_PAYLOADS, tag, "*.json")))
        files = [f for f in files if not os.path.basename(f).startswith("_")]
        if args.limit:
            files = files[:args.limit]
        per_model_sites = 0
        for pf in files:
            loaded = load_trace(tag, pf)
            if loaded is None:
                n_dropped_gpqa += 1
                continue
            pay, lab = loaded
            segs = pay["segments"]
            n = len(segs)
            bench = pay["benchmark"]
            qid = pay["question_id"]
            cell = f"{model}__{bench}"
            trace_file = f"{cell}__{bc.san_qid(qid)}.json"

            rex = [e for e in bc.regex_events_for(pay, DOMAIN) if 0 <= e["seg_id"] < n]
            jud = judge_events(lab, n, allowed, ev_drops)
            spans = judge_spans(lab, n, sp_drops)
            for e in rex:
                regex_ev[e["type"]] += 1
            for e in jud:
                judge_ev[e["type"]] += 1

            agent_events = {"regex": rex, JUDGE_AGENT: jud}
            present = ["regex", JUDGE_AGENT]
            trace_sites = []
            for site in bc.build_sites(agent_events):
                ans = bc.site_answers(site, present)
                if len({tuple(v) for v in ans.values()}) <= 1:
                    continue        # слои согласны — не конфликт
                j_typed = ans[JUDGE_AGENT] != ["∅"]
                r_typed = ans["regex"] != ["∅"]
                kind = ("type_mismatch" if j_typed and r_typed
                        else "judge_only" if j_typed else "regex_only")
                types = [t for t in (ans[JUDGE_AGENT] + ans["regex"]) if t != "∅"]
                trace_sites.append({
                    "item_id": bc.stable_id(cell, qid, site, ans, seen_ids),
                    "corpus": CORPUS, "slice": SLICE, "model": model, "trace_model": pay.get("model"),
                    "tag": tag, "benchmark": bench, "question_id": qid, "domain": DOMAIN,
                    "cell": cell, "seg_id": site["seg_id"], "segs": site["segs"],
                    "agents_present": present,
                    "per_agent": {a: ans[a] for a in present},
                    "quotes": {a: [x["quote"] for x in site["per_agent"].get(a, []) if x["quote"]][:1]
                               for a in present},
                    "conflict_kind": kind,
                    "priority": KIND_PRIORITY[kind],
                    "type_rank": min((ranks.get(t, 99) for t in types), default=99),
                    "trace_file": trace_file, "n_segments": n,
                    "verdict": None, "corrected_type": None, "notes": None,
                })
                stat_kind[kind] += 1
                for t in set(types):
                    stat_type[t] += 1
            if not trace_sites:
                continue            # трасса без конфликтов во вьюер не идёт
            n_traces += 1
            benchmarks[bench] += 1
            per_model_sites += len(trace_sites)
            sites_out.extend(trace_sites)
            events = ([{"s": e["seg_id"], "t": e["type"], "a": "regex"} for e in rex]
                      + [{"s": e["seg_id"], "t": e["type"], "a": JUDGE_AGENT} for e in jud])
            events.sort(key=lambda x: (x["s"], x["a"]))
            trace_rows[trace_file] = {
                "cell": cell, "question_id": qid, "benchmark": bench, "domain": DOMAIN,
                "question": pay.get("question", ""),
                "segments": [{"seg_id": s["seg_id"], "text": s["text"]} for s in segs],
                "events": events, "spans": spans, "agents": present,
                "lam": btm.lam_total(events, n, hawkes),
                "corpus": CORPUS, "trace_model": pay.get("model"), "tag": tag,
                "model_label": label, "correct": pay.get("correct"),
                "cap": pay.get("cap"), "arm": pay.get("arm"),
            }
        stat_model[model] = per_model_sites

    # --- 3. проверка запрета GPQA до записи ---
    bad = [tf for tf in trace_rows if "gpqa" in tf.lower()]
    bad += [c["item_id"] for c in sites_out if "gpqa" in json.dumps(c, ensure_ascii=False).lower()]
    if bad:
        raise SystemExit(f"в срез попал GPQA: {bad[:5]}")

    # --- 4. запись ---
    for tf, obj in trace_rows.items():
        json.dump(obj, open(os.path.join(TR, tf), "w"), ensure_ascii=False)
    sites_out.sort(key=lambda s: (-s["priority"], s["type_rank"], s["model"],
                                  s["benchmark"], s["question_id"], s["seg_id"]))
    merged = legacy_conflicts + sites_out
    json.dump(merged, open(os.path.join(DATA, "conflicts.json"), "w"), ensure_ascii=False)

    trace_files = glob.glob(os.path.join(TR, "*.json"))
    summary_path = os.path.join(DATA, "build_summary.json")
    archived = json.load(open(summary_path)).get("archived_2026_08_13", "")
    summary = {
        "corpus": CORPUS, "slice": SLICE, "judge_model": "deepseek-chat",
        "agents": ["regex", JUDGE_AGENT],
        "traces_in_viewer": len(trace_files), "sites_in_viewer": len(merged),
        "traces_dropped_gpqa": n_dropped_gpqa,
        "benchmarks": dict(benchmarks),
        "by_kind": dict(stat_kind),
        "by_model": dict(stat_model),
        "by_type": dict(stat_type),
        "judge_events": dict(judge_ev), "regex_events": dict(regex_ev),
        "event_drops": dict(ev_drops), "span_drops": dict(sp_drops),
        "note": ("срез судья-против-регулярок на восьми моделях одного корпуса; GPQA-diamond "
                 "исключён целиком (закрытый доступ), остаются MATH500 и BBH"),
        "archived_2026_08_13": archived,
    }
    json.dump(summary, open(summary_path, "w"), ensure_ascii=False, indent=1)

    # λ среза считается по параметрам Хокса, зафиксированным на старом корпусе (он в приватном
    # архиве): так шкала интенсивности остаётся сравнимой с уже посчитанной, параметры не
    # перефитятся.
    meta_path = os.path.join(DATA, "trace_maps_meta.json")
    meta = json.load(open(meta_path))
    lam_max = meta.get("lam_max", 0.0)
    for obj in trace_rows.values():
        if obj["lam"]:
            lam_max = max(lam_max, max(obj["lam"]))
    meta["n_traces"] = len(trace_files)
    meta["lam_max"] = round(float(lam_max), 3)
    ops = set(meta.get("operators") or [])
    for obj in trace_rows.values():
        ops.update(s["op"] for s in obj["spans"])
    meta["operators"] = sorted(ops)
    meta["note"] = ("пер-типовый univariate Hawkes MLE (μ,α,β на тип) на union-событиях старого "
                    "корпуса вьюера, снятого с публикации 2026-08-13; параметры зафиксированы и "
                    "не перефитятся, чтобы шкала λ среза native-PO осталась сравнимой с уже "
                    "посчитанной; λ(s)=Σ_t λ_t(s); развилки = branch/backtrack/failed_attempt")
    json.dump(meta, open(meta_path, "w"), ensure_ascii=False, indent=1)

    print(f"native-PO: трасс {n_traces} (исключено GPQA {n_dropped_gpqa}), конфликт-сайтов {len(sites_out)}")
    print("  по виду конфликта:", dict(stat_kind))
    print("  по моделям:", dict(stat_model))
    print("  по типам событий:", dict(stat_type))
    print("  бенчмарки:", dict(benchmarks))
    print(f"  событий: судья {sum(judge_ev.values())} / регулярки {sum(regex_ev.values())}")
    print(f"  снимок: трасс {len(trace_files)}, сайтов {len(merged)}")


if __name__ == "__main__":
    main()
