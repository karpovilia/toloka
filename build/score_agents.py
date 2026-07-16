"""
Скоринг агентов под модель верификации событий (Toloka v2).

Вход:
  toloka/data/traces/*.json          — трассы с events[] (для реконструкции кандидатов);
  toloka/annotations/annot_*.json    — выгрузки разметчиков; ключ = candId "<trace_file>|s<seg>|<type>",
                                        verdict ∈ {"✓" подтверждён, "✗" ложный, "<тип>" переразмечен}.

Кандидат события = кластер событий одного типа в окне ±1 сегмента (как во вьюере), agents = union.
gold по кандидату = мажоритарный вердикт разметчиков.

Метрики на агента (по кандидатам, где агент участвовал):
  detection precision = confirmed / (confirmed + rejected)   [реальное событие vs ложное]
  type accuracy       = confirmed_correct_type / real         [дал ли агент верный тип, если событие реально]
  (recall не считаем — пропущенные события в этой модели не размечаются).
confirmed = ✓; rejected = ✗; retyped = <тип> (событие реально, но тип другой -> detection TP, type wrong).

Плюс межаннотаторное согласие (Cohen κ для 2 / Fleiss для ≥3).
Выход: toloka/data/agent_scores.json, toloka/data/score_report.md.
"""
import json, glob, os, math, argparse
from collections import Counter, defaultdict

AGENTS = ["regex", "claude", "deepseek", "qwen"]
CONFIRM, REJECT = "✓", "✗"
TOL = 1


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (p, (c - half) / d, (c + half) / d)


def cohen_kappa(pairs, labels):
    n = len(pairs)
    if not n:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs); cb = Counter(b for _, b in pairs)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in labels)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def fleiss_kappa(rows, labels):
    labels = list(labels); N = len(rows)
    if not N:
        return None
    m = sum(rows[0].values())
    if m < 2 or any(sum(r.values()) != m for r in rows):
        return None
    P = [sum(r[l] * (r[l] - 1) for l in labels) / (m * (m - 1)) for r in rows]
    Pbar = sum(P) / N
    pj = {l: sum(r[l] for r in rows) / (N * m) for l in labels}
    Pe = sum(v * v for v in pj.values())
    return (Pbar - Pe) / (1 - Pe) if Pe < 1 else 1.0


def build_candidates(events):
    by_type = defaultdict(list)
    for e in events:
        by_type[e["t"]].append(e)
    cands = []
    for t, evs in by_type.items():
        evs.sort(key=lambda e: e["s"]); used = [False] * len(evs)
        for i in range(len(evs)):
            if used[i]:
                continue
            segs = [evs[i]["s"]]; agents = {evs[i]["a"]}; used[i] = True
            for j in range(i + 1, len(evs)):
                if not used[j] and abs(evs[j]["s"] - evs[i]["s"]) <= TOL:
                    segs.append(evs[j]["s"]); agents.add(evs[j]["a"]); used[j] = True
            anchor = max(set(segs), key=segs.count)
            cands.append({"seg": anchor, "type": t, "agents": sorted(agents)})
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/ki/repos/reasoning/toloka")
    args = ap.parse_args()
    data = os.path.join(args.root, "data"); anndir = os.path.join(args.root, "annotations")
    os.makedirs(anndir, exist_ok=True)

    # candId -> agents (+ base type) из трасс
    cand_agents = {}; cand_type = {}
    for f in glob.glob(os.path.join(data, "traces", "*.json")):
        tf = os.path.basename(f)
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for c in build_candidates(d.get("events", [])):
            cid = f"{tf}|s{c['seg']}|{c['type']}"
            cand_agents[cid] = c["agents"]; cand_type[cid] = c["type"]

    # разметки
    per_cand = defaultdict(dict)  # cid -> {annotator: verdict}
    annotators = set()
    for f in glob.glob(os.path.join(anndir, "annot_*.json")):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        aid = j.get("annotator_id") or os.path.basename(f); annotators.add(aid)
        for cid, rec in (j.get("annotations") or {}).items():
            v = rec.get("verdict")
            if v:
                per_cand[cid][aid] = v
    annotators = sorted(annotators)

    def majority(votes):
        vals = list(votes.values())
        if not vals:
            return None
        return Counter(vals).most_common(1)[0][0]

    det_tp = Counter(); det_fp = Counter(); type_ok = Counter(); type_real = Counter()
    by_type_tp = defaultdict(Counter); by_type_fp = defaultdict(Counter)
    adjudicated = 0
    for cid, votes in per_cand.items():
        if cid not in cand_agents:
            continue
        gold = majority(votes)
        if gold is None:
            continue
        adjudicated += 1
        agents = cand_agents[cid]; typ = cand_type[cid]
        confirmed = (gold == CONFIRM); rejected = (gold == REJECT); retyped = not confirmed and not rejected
        for ag in agents:
            if rejected:
                det_fp[ag] += 1; by_type_fp[ag][typ] += 1
            else:  # confirmed | retyped -> реальное событие
                det_tp[ag] += 1; by_type_tp[ag][typ] += 1
                type_real[ag] += 1
                if confirmed:
                    type_ok[ag] += 1

    scores = {}
    for ag in AGENTS:
        det_n = det_tp[ag] + det_fp[ag]
        if det_n == 0:
            continue
        p, lo, hi = wilson(det_tp[ag], det_n)
        tacc = (type_ok[ag] / type_real[ag]) if type_real[ag] else None
        scores[ag] = {
            "detection_precision": p, "det_ci95": [lo, hi],
            "confirmed": det_tp[ag], "rejected": det_fp[ag], "n_candidates": det_n,
            "type_accuracy": tacc, "type_real": type_real[ag], "type_ok": type_ok[ag],
        }

    # межаннотаторное согласие
    iaa = {"n_annotators": len(annotators), "annotators": annotators}
    shared = {c: v for c, v in per_cand.items() if len(v) >= 2}
    if len(annotators) == 2:
        a0, a1 = annotators; pairs = []; labels = set()
        for v in shared.values():
            if a0 in v and a1 in v:
                pairs.append((v[a0], v[a1])); labels.update([v[a0], v[a1]])
        iaa["cohen_kappa"] = cohen_kappa(pairs, labels); iaa["n_pairs"] = len(pairs)
    elif len(annotators) >= 3:
        rows, labels = [], set()
        for v in shared.values():
            if len(v) == len(annotators):
                rows.append(Counter(v.values())); labels.update(v.values())
        iaa["fleiss_kappa"] = fleiss_kappa(rows, labels); iaa["n_complete"] = len(rows)

    out = {"adjudicated_candidates": adjudicated, "total_candidates": len(cand_agents),
           "agent_scores": scores, "inter_annotator": iaa,
           "note": "detection precision = доля подтверждённых среди размеченных кандидатов агента; type accuracy = верный тип среди реальных; recall не измеряется (пропуски не размечаются)"}
    json.dump(out, open(os.path.join(data, "agent_scores.json"), "w"), ensure_ascii=False, indent=1)

    L = ["# Score агентов по верификации событий\n",
         f"Адъюдицировано кандидатов: {adjudicated} из {len(cand_agents)}. Разметчиков: {len(annotators)} ({', '.join(annotators) or '—'}).\n",
         "## Detection precision и type accuracy\n",
         "| агент | det.precision | 95% CI | ✓ | ✗ | type acc | тип верен/реальных |",
         "|---|---|---|---|---|---|---|"]
    for ag in AGENTS:
        s = scores.get(ag)
        if s:
            ta = f"{s['type_accuracy']:.3f}" if s["type_accuracy"] is not None else "—"
            L.append(f"| {ag} | {s['detection_precision']:.3f} | [{s['det_ci95'][0]:.3f}, {s['det_ci95'][1]:.3f}] | {s['confirmed']} | {s['rejected']} | {ta} | {s['type_ok']}/{s['type_real']} |")
    L.append("")
    if "cohen_kappa" in iaa and iaa["cohen_kappa"] is not None:
        L.append(f"Cohen κ = {iaa['cohen_kappa']:.3f} на {iaa['n_pairs']} общих кандидатах (2 разметчика).\n")
    elif "fleiss_kappa" in iaa and iaa["fleiss_kappa"] is not None:
        L.append(f"Fleiss κ = {iaa['fleiss_kappa']:.3f} на {iaa['n_complete']} кандидатах.\n")
    else:
        L.append("Недостаточно пересечений разметчиков для κ.\n")
    L.append("Оговорка: recall не измеряется (в этой модели размечаются только события агентов, пропущенные — нет).")
    open(os.path.join(data, "score_report.md"), "w").write("\n".join(L))

    print(f"адъюдицировано: {adjudicated} | агентов со score: {len(scores)}")
    for ag, s in scores.items():
        print(f"  {ag}: det.precision={s['detection_precision']:.3f} (✓{s['confirmed']}/✗{s['rejected']}) type_acc={s['type_accuracy']}")


if __name__ == "__main__":
    main()
