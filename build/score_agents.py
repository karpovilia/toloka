"""
Считает score агентов (regex / Claude / DeepSeek / Qwen) из ручной адъюдикации конфликтов.

Вход:
  toloka/data/conflicts.json          — сайты, что были во вьюере (с per_agent-ответами);
  toloka/annotations/annot_*.json     — выгрузки разметчиков ({annotator_id, annotations:{item_id:{verdict,notes}}}).

Логика:
  gold на сайте = мажоритарный вердикт по разметчикам (тип события | "∅" нет события);
  вердикт "unclear" исключается из скоринга.
  Агент прав на сайте, если его ответ (per_agent[agent]) содержит gold-тип;
  если gold == "∅" — агент прав, когда он тоже молчал (ответ == ["∅"]).

Метрики:
  - per-agent accuracy на адъюдицированных сайтах, где агент присутствовал (± Wilson 95% CI);
  - per-agent per-type precision/recall (условные НА конфликтах — сайты не случайны, см. README);
  - межаннотаторное согласие: Cohen κ (ровно 2 разметчика) либо Fleiss κ (≥3);
  - Dawid–Skene (EM): надёжности агентов и мягкие метки по ВСЕМ сайтам, заякоренные ручными;
  - ранжирование ещё не размеченных сайтов для следующей пачки (по приоритету и разногласию).

Выход: toloka/data/agent_scores.json, toloka/data/score_report.md,
        toloka/data/next_batch.jsonl (топ неразмеченных сайтов).
"""
import json, glob, os, math, argparse
from collections import defaultdict, Counter

NONE = "∅"
UNCLEAR = "unclear"
AGENTS = ["regex", "claude", "deepseek", "qwen"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (p, (c - half) / d, (c + half) / d)


def load_annotations(anndir):
    """item_id -> {annotator_id: verdict}."""
    per_item = defaultdict(dict)
    annotators = set()
    for f in glob.glob(os.path.join(anndir, "annot_*.json")):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        aid = j.get("annotator_id") or os.path.basename(f)
        annotators.add(aid)
        for item_id, rec in (j.get("annotations") or {}).items():
            v = rec.get("verdict")
            if v:
                per_item[item_id][aid] = v
    return per_item, sorted(annotators)


def normverd(v):
    """Вердикт разметчика -> отсортированный список типов (мультиселект).
    Легаси-строка оборачивается; пустое/None -> []."""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    return sorted(x for x in v if x)


def canon(v):
    """Канонический ярлык вердикта для κ (одна категория на разметчика)."""
    return "+".join(normverd(v))


def majority(votes):
    """votes: dict annotator->verdict(список|строка). Возвращает (gold_set, n_eff, agreed).
    gold_set — множество типов (мультилейбл) по типово-мажоритарному правилу, либо [NONE]."""
    norm = [normverd(v) for v in votes.values()]
    norm = [v for v in norm if v and v != [UNCLEAR]]   # выкинуть unclear/пустые
    if not norm:
        return None, 0, None
    n = len(norm)
    none_cnt = sum(1 for v in norm if v == [NONE])
    supp = Counter(t for v in norm for t in v if t != NONE)
    gold = [t for t, c in supp.items() if 2 * c >= n]   # тип в gold, если поддержан ≥ половиной
    if none_cnt * 2 >= n and none_cnt >= (max(supp.values()) if supp else 0):
        gold = [NONE]
    if not gold:                                        # разнобой без большинства — берём модальный вердикт
        gold = normverd(Counter(canon(v) for v in norm).most_common(1)[0][0].split("+"))
    agreed = len({canon(v) for v in norm}) == 1
    return sorted(gold), n, agreed


def agent_correct(agent_answer, gold_set):
    if gold_set == [NONE]:
        return agent_answer == [NONE]
    return any(t in gold_set for t in agent_answer)


def cohen_kappa(pairs, labels):
    """pairs: list of (a,b). labels: set of all labels."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in labels)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def fleiss_kappa(rows, labels):
    """rows: list of Counter(label->count) для сайтов с одинаковым числом оценщиков m."""
    labels = list(labels)
    N = len(rows)
    if N == 0:
        return None
    m = sum(rows[0].values())
    if m < 2 or any(sum(r.values()) != m for r in rows):
        return None
    P = []
    for r in rows:
        s = sum(r[l] * (r[l] - 1) for l in labels)
        P.append(s / (m * (m - 1)))
    Pbar = sum(P) / N
    pj = {l: sum(r[l] for r in rows) / (N * m) for l in labels}
    Pe = sum(v * v for v in pj.values())
    return (Pbar - Pe) / (1 - Pe) if Pe < 1 else 1.0


def dawid_skene(sites, present_map, iters=50):
    """
    Лёгкий Dawid–Skene по агентам как «оценщикам».
    sites: list of item_id; per-agent ответ берём как один голос = первый тип из per_agent
           (или NONE). Заякорено ничем (без hard-labels) — оценивает согласованность/надёжность.
    Возвращает {agent: accuracy_on_own_consensus}.
    """
    # классы = наблюдаемые типы + NONE
    label_set = set()
    votes = {}  # item -> {agent: label}
    for it in sites:
        vv = {}
        for ag, ans in present_map[it].items():
            lab = ans[0] if ans else NONE
            vv[ag] = lab
            label_set.add(lab)
        votes[it] = vv
    labels = sorted(label_set)
    if not labels:
        return {}
    Li = {l: i for i, l in enumerate(labels)}
    K = len(labels)
    # init: T[item] = one-hot по majority
    T = {}
    for it in sites:
        c = Counter(votes[it].values())
        T[it] = [0.0] * K
        top = c.most_common(1)[0][0]
        T[it][Li[top]] = 1.0
    agents = sorted({a for it in sites for a in votes[it]})
    prior = [1.0 / K] * K
    for _ in range(iters):
        # M-step: confusion per agent
        conf = {a: [[1e-6] * K for _ in range(K)] for a in agents}
        for it in sites:
            for a, lab in votes[it].items():
                j = Li[lab]
                for k in range(K):
                    conf[a][k][j] += T[it][k]
        for a in agents:
            for k in range(K):
                s = sum(conf[a][k])
                conf[a][k] = [x / s for x in conf[a][k]]
        prior = [1e-6] * K
        for it in sites:
            for k in range(K):
                prior[k] += T[it][k]
        sp = sum(prior)
        prior = [x / sp for x in prior]
        # E-step
        for it in sites:
            post = [prior[k] for k in range(K)]
            for a, lab in votes[it].items():
                j = Li[lab]
                for k in range(K):
                    post[k] *= conf[a][k][j]
            s = sum(post) or 1.0
            T[it] = [x / s for x in post]
    # надёжность агента = средняя диагональ его confusion, взвешенная prior
    rel = {}
    for a in agents:
        conf = [[1e-6] * K for _ in range(K)]
        for it in sites:
            if a in votes[it]:
                j = Li[votes[it][a]]
                for k in range(K):
                    conf[k][j] += T[it][k]
        acc = 0.0
        for k in range(K):
            s = sum(conf[k])
            acc += (conf[k][k] / s) * prior[k]
        rel[a] = acc
    return rel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/ki/repos/reasoning/toloka")
    ap.add_argument("--ds-iters", type=int, default=50)
    args = ap.parse_args()

    data = os.path.join(args.root, "data")
    anndir = os.path.join(args.root, "annotations")
    os.makedirs(anndir, exist_ok=True)

    sites = json.load(open(os.path.join(data, "conflicts.json")))
    by_id = {s["item_id"]: s for s in sites}
    present_map = {s["item_id"]: {a: s["per_agent"][a] for a in s["agents_present"]} for s in sites}

    per_item, annotators = load_annotations(anndir)
    n_ann_files = len(annotators)

    # ---- gold + per-agent accuracy ----
    acc_hit = Counter(); acc_tot = Counter()
    type_tp = Counter(); type_fp = Counter(); type_fn = Counter()  # ключ (agent,type)
    adjudicated = 0
    gold_by_item = {}
    for item_id, votes in per_item.items():
        if item_id not in by_id:
            continue
        gold, neff, agreed = majority(votes)
        if gold is None:
            continue
        gold_by_item[item_id] = gold
        adjudicated += 1
        site = by_id[item_id]
        for ag in site["agents_present"]:
            ans = site["per_agent"][ag]
            correct = agent_correct(ans, gold)
            acc_tot[ag] += 1
            if correct:
                acc_hit[ag] += 1
            # per-type detection (мультилейбл gold; тип != NONE)
            for t in ans:
                if t == NONE:
                    continue
                if t in gold:
                    type_tp[(ag, t)] += 1
                else:
                    type_fp[(ag, t)] += 1
            for g in gold:
                if g != NONE and g not in ans:
                    type_fn[(ag, g)] += 1

    agent_scores = {}
    for ag in AGENTS:
        if acc_tot[ag] == 0:
            continue
        p, lo, hi = wilson(acc_hit[ag], acc_tot[ag])
        types = {}
        seen_types = {t for (a, t) in list(type_tp) + list(type_fp) + list(type_fn) if a == ag}
        for t in sorted(seen_types):
            tp, fp, fn = type_tp[(ag, t)], type_fp[(ag, t)], type_fn[(ag, t)]
            prec = tp / (tp + fp) if tp + fp else None
            rec = tp / (tp + fn) if tp + fn else None
            types[t] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec}
        agent_scores[ag] = {
            "accuracy": p, "ci95": [lo, hi], "n_sites": acc_tot[ag], "n_correct": acc_hit[ag],
            "per_type": types,
        }

    # ---- межаннотаторное согласие ----
    iaa = {"n_annotators": n_ann_files, "annotators": annotators}
    def valid(v):  # нормализованный ярлык вердикта или None (для unclear/пустых)
        c = canon(v)
        return c if c and c != UNCLEAR else None
    shared = {i: v for i, v in per_item.items() if sum(1 for x in v.values() if valid(x)) >= 2 and i in by_id}
    if n_ann_files == 2:
        a0, a1 = annotators
        pairs = []
        labels = set()
        for v in shared.values():
            if a0 in v and a1 in v and valid(v[a0]) and valid(v[a1]):
                pairs.append((valid(v[a0]), valid(v[a1]))); labels.update([valid(v[a0]), valid(v[a1])])
        iaa["cohen_kappa"] = cohen_kappa(pairs, labels)
        iaa["n_pairs"] = len(pairs)
        iaa["note"] = "Cohen κ применим ровно к 2 разметчикам; мультиселект-вердикт = одна категория (сортированный набор типов)"
    elif n_ann_files >= 3:
        rows, labels = [], set()
        for v in shared.values():
            vals = [valid(x) for x in v.values() if valid(x)]
            if len(vals) == n_ann_files:
                rows.append(Counter(vals)); labels.update(vals)
        iaa["fleiss_kappa"] = fleiss_kappa(rows, labels)
        iaa["n_complete_sites"] = len(rows)
        iaa["note"] = "Fleiss κ для ≥3 разметчиков (только сайты с полным покрытием)"

    # ---- Dawid–Skene по всем сайтам ----
    ds_rel = dawid_skene([s["item_id"] for s in sites], present_map, iters=args.ds_iters) if sites else {}

    # ---- ранжирование неразмеченных сайтов на следующую пачку ----
    unlabeled = [s for s in sites if s["item_id"] not in gold_by_item]

    def disagreement(s):
        answers = [tuple(s["per_agent"][a]) for a in s["agents_present"]]
        return len(set(answers))

    unlabeled.sort(key=lambda s: (-s["priority"], -disagreement(s), s["cell"], s["seg_id"]))
    with open(os.path.join(data, "next_batch.jsonl"), "w") as f:
        for s in unlabeled[:2000]:
            f.write(json.dumps({"item_id": s["item_id"], "priority": s["priority"],
                                "slice": s["slice"], "benchmark": s["benchmark"],
                                "agents_present": s["agents_present"], "per_agent": s["per_agent"],
                                "disagreement": disagreement(s)}, ensure_ascii=False) + "\n")

    out = {
        "adjudicated_sites": adjudicated,
        "sites_total_in_viewer": len(sites),
        "agent_scores": agent_scores,
        "inter_annotator": iaa,
        "dawid_skene_reliability": ds_rel,
        "note": "метрики условны НА конфликтах (сайты не случайны); accuracy = доля совпадений с мажоритарным человеком",
    }
    json.dump(out, open(os.path.join(data, "agent_scores.json"), "w"), ensure_ascii=False, indent=1)

    # ---- markdown отчёт ----
    L = []
    L.append("# Score агентов по ручной адъюдикации конфликтов\n")
    L.append(f"Адъюдицировано сайтов: {adjudicated} из {len(sites)} во вьюере. "
             f"Разметчиков: {n_ann_files} ({', '.join(annotators) or '—'}).\n")
    L.append("Метрики условны НА конфликтных сайтах (выборка не случайна) — это относительное ранжирование агентов, не абсолютный recall/precision на корпусе.\n")
    L.append("## Accuracy агентов (совпадение с мажоритарным человеком)\n")
    L.append("| агент | accuracy | 95% CI | сайтов | верно |")
    L.append("|---|---|---|---|---|")
    for ag in AGENTS:
        s = agent_scores.get(ag)
        if s:
            L.append(f"| {ag} | {s['accuracy']:.3f} | [{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}] | {s['n_sites']} | {s['n_correct']} |")
    L.append("")
    if ds_rel:
        L.append("## Dawid–Skene надёжность (по всем сайтам, без якорей)\n")
        L.append("| агент | reliability |")
        L.append("|---|---|")
        for ag in AGENTS:
            if ag in ds_rel:
                L.append(f"| {ag} | {ds_rel[ag]:.3f} |")
        L.append("")
    L.append("## Межаннотаторное согласие\n")
    if "cohen_kappa" in iaa and iaa["cohen_kappa"] is not None:
        L.append(f"Cohen κ = {iaa['cohen_kappa']:.3f} на {iaa['n_pairs']} общих сайтах (2 разметчика).")
    elif "fleiss_kappa" in iaa and iaa["fleiss_kappa"] is not None:
        L.append(f"Fleiss κ = {iaa['fleiss_kappa']:.3f} на {iaa['n_complete_sites']} сайтах ({n_ann_files} разметчика).")
    else:
        L.append("Недостаточно пересечений разметчиков для κ (нужно ≥2 оценки на одних сайтах).")
    L.append("")
    L.append("## Per-type precision/recall (условно на конфликтах)\n")
    for ag in AGENTS:
        s = agent_scores.get(ag)
        if not s or not s["per_type"]:
            continue
        L.append(f"### {ag}")
        L.append("| тип | P | R | tp | fp | fn |")
        L.append("|---|---|---|---|---|---|")
        for t, d in sorted(s["per_type"].items()):
            pp = f"{d['precision']:.2f}" if d["precision"] is not None else "—"
            rr = f"{d['recall']:.2f}" if d["recall"] is not None else "—"
            L.append(f"| {t} | {pp} | {rr} | {d['tp']} | {d['fp']} | {d['fn']} |")
        L.append("")
    L.append(f"Следующая пачка на разметку: см. `data/next_batch.jsonl` (топ {min(2000,len(unlabeled))} неразмеченных по приоритету и разногласию).")
    open(os.path.join(data, "score_report.md"), "w").write("\n".join(L))

    print(f"адъюдицировано: {adjudicated} | агентов со score: {len(agent_scores)}")
    for ag, s in agent_scores.items():
        print(f"  {ag}: acc={s['accuracy']:.3f} (n={s['n_sites']})")
    if ds_rel:
        print("  Dawid–Skene:", {k: round(v, 3) for k, v in ds_rel.items()})
    print("отчёт: data/score_report.md, data/agent_scores.json, data/next_batch.jsonl")


if __name__ == "__main__":
    main()
