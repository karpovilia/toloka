"""
Дописывает в data/traces/<file>.json слой «карта reasoning»:
  - events[] — union событий ВСЕХ агентов трассы (regex/claude/deepseek/qwen): {s:seg_id, t:type, a:agent};
  - spans[]  — операторные спаны (deepseek на срезе A / qwen на срезе B): {a:seg_start, b:seg_end, op:operator};
  - lam[]    — интенсивность по Хоксу на сегмент (univariate self-exciting, фит на пуле событий корпуса);
  - agents[] — какие агенты размечали трассу.
Плюс data/trace_maps_meta.json — параметры Хокса (mu,alpha,beta), нормировки, словарь операторов.

Интенсивность «измеренная»: μ,α,β фитятся MLE (экспоненциальное ядро, O(N)-рекурсия) на пуле
seg-времён событий по всем публикуемым трассам; затем λ считается по каждой трассе.
Развилки не отдельное поле — вьюер помечает их по типам событий (branch/backtrack/failed_attempt).
"""
import json, os, glob, sys
import numpy as np
from scipy.optimize import minimize

BUILD = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BUILD)
import build_conflicts as bc  # payload_for, regex_events_for, load_events, MODEL_FULL, san_qid, dom_of

ROOT = bc.BASE
POC = bc.POC
DATA = os.path.join(ROOT, "toloka", "data")
TR = os.path.join(DATA, "traces")

AGENT_DIRS = {"claude": bc.GOLD_CLAUDE, "deepseek": bc.GOLD_DEEPSEEK}
SHORT2FULL = bc.MODEL_FULL
FULL2SHORT = {v: k for k, v in SHORT2FULL.items()}


def qwen_index():
    """(short,bench,qid) -> path к qwen dual-файлу (трассы gpt-oss)."""
    idx = {}
    for f in glob.glob(os.path.join(bc.GOLD_QWEN, "*.json")):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            d = json.load(open(f)); m = d["_meta"]
        except Exception:
            continue
        short = FULL2SHORT.get(m.get("trace_model"), "gptoss")
        idx[(short, m.get("benchmark"), d["question_id"])] = f
    return idx


def events_union(model_short, bench, qid, pay, dom, qwen_idx):
    """Все события всех агентов: [{s,t,a}]. И какие агенты присутствуют."""
    segset = {s["seg_id"] for s in pay["segments"]}
    ev, agents = [], []
    # regex — пересчёт тем же детектором
    for e in bc.regex_events_for(pay, dom):
        if e["seg_id"] in segset:
            ev.append({"s": e["seg_id"], "t": e["type"], "a": "regex"})
    agents.append("regex")
    # claude / deepseek — по имени <short>__<bench>__<qid>
    for ag, d in AGENT_DIRS.items():
        p = os.path.join(d, f"{model_short}__{bench}__{bc.san_qid(qid)}.json")
        if not os.path.exists(p):
            p = os.path.join(d, f"{model_short}__{bench}__{qid}.json")
        evs = bc.load_events(p, "trigger_quote") if os.path.exists(p) else None
        if evs:
            for e in evs:
                if e["seg_id"] in segset:
                    ev.append({"s": e["seg_id"], "t": e["type"], "a": ag})
            agents.append(ag)
    # qwen — по _meta-индексу (gpt-oss)
    qp = qwen_idx.get((model_short, bench, qid))
    if qp:
        evs = bc.load_events(qp, "trigger_quote")
        if evs:
            for e in evs:
                if e["seg_id"] in segset:
                    ev.append({"s": e["seg_id"], "t": e["type"], "a": "qwen"})
            agents.append("qwen")
    return ev, agents


def spans_for(model_short, bench, qid, qwen_idx):
    """Операторные спаны: deepseek (если есть) иначе qwen. [{a,b,op}]."""
    p = os.path.join(bc.GOLD_DEEPSEEK, f"{model_short}__{bench}__{bc.san_qid(qid)}.json")
    src = p if os.path.exists(p) else qwen_idx.get((model_short, bench, qid))
    if not src or not os.path.exists(src):
        return []
    try:
        d = json.load(open(src))
    except Exception:
        return []
    out = []
    for s in d.get("spans", []):
        if "seg_start" in s and "seg_end" in s and "operator" in s:
            out.append({"a": int(s["seg_start"]), "b": int(s["seg_end"]), "op": s["operator"]})
    return sorted(out, key=lambda x: x["a"])


# ---------- univariate Hawkes (exp kernel), устойчивый ----------
# Параметры зажаты, чтобы MLE не вырождался: события разных агентов часто в одном seg_id,
# совпадающие времена гонят beta->inf. Совпадения разносим джиттером внутри сегмента,
# beta ограничиваем разумным диапазоном затухания по оси сегментов.
BETA_LO, BETA_HI = 0.05, 3.0
ALPHA_MAX = 0.98


def jitter(times):
    """Совпадающие seg-времена разносим на eps внутри сегмента -> строго возрастающие."""
    t = sorted(float(x) for x in times)
    out = []
    i = 0
    while i < len(t):
        j = i
        while j < len(t) and t[j] == t[i]:
            j += 1
        k = j - i
        for r in range(k):
            out.append(t[i] + (r + 1) / (k + 1))   # k событий -> k точек внутри [s, s+1)
        i = j
    return np.array(out)


def _unpack(params):
    mu = np.exp(np.clip(params[0], -12, 4))
    alpha = ALPHA_MAX / (1.0 + np.exp(-params[1]))
    beta = BETA_LO + (BETA_HI - BETA_LO) / (1.0 + np.exp(-params[2]))
    return mu, alpha, beta


def nll(params, seqs):
    mu, alpha, beta = _unpack(params)
    total = 0.0
    for t in seqs:
        if len(t) == 0:
            continue
        T = t[-1] + 1.0
        comp = mu * T + alpha * np.sum(1.0 - np.exp(-beta * (T - t)))
        A = 0.0; logsum = 0.0; prev = t[0]
        for j in range(len(t)):
            A = 0.0 if j == 0 else np.exp(-beta * (t[j] - prev)) * (1.0 + A)
            lam = mu + alpha * beta * A
            logsum += np.log(lam if lam > 1e-12 else 1e-12); prev = t[j]
        total += logsum - comp
    return -total


def fit_hawkes(seqs, sub=200):
    seqs = [jitter(s) for s in seqs if len(s) >= 2]
    fit_seqs = seqs[::max(1, len(seqs) // sub)] if len(seqs) > sub else seqs
    x0 = np.array([np.log(0.3), 0.0, 0.0])
    res = minimize(nll, x0, args=(fit_seqs,), method="Nelder-Mead",
                   options={"maxiter": 1200, "xatol": 1e-3, "fatol": 1e-2})
    mu, alpha, beta = _unpack(res.x)
    return float(mu), float(alpha), float(beta), -float(res.fun), len(fit_seqs)


def fit_by_type(per_trace, sub=200):
    """Пер-типовый univariate Hawkes: по каждому типу события свой (μ,α,β).
    Даёт разложение интенсивности по типам (для drill-down)."""
    from collections import defaultdict
    pools = defaultdict(list)   # type -> [array времён по трассам]
    for d in per_trace.values():
        by_t = defaultdict(list)
        for e in d["ev"]:
            by_t[e["t"]].append(float(e["s"]))
        for t, times in by_t.items():
            if len(times) >= 2:
                pools[t].append(np.array(sorted(times)))
    params = {}
    for t, seqs in pools.items():
        if len(seqs) < 3:            # слишком мало трасс с типом — только базовый уровень
            allt = np.concatenate(seqs) if seqs else np.array([0.0])
            params[t] = {"mu": max(1e-3, len(allt) / max(1, sum(len(s) for s in seqs) + 1)), "alpha": 0.0, "beta": 0.5, "n_seqs": len(seqs)}
            continue
        mu, alpha, beta, ll, nfit = fit_hawkes(seqs, sub=sub)
        params[t] = {"mu": round(mu, 4), "alpha": round(alpha, 4), "beta": round(beta, 4), "n_seqs": len(seqs)}
    return params


def lam_total(ev, n, params):
    """Суммарная λ на сегмент = сумма пер-типовых интенсивностей (без джиттера при оценке).
    λ(s) = Σ_t [μ_t + α_t·β_t·Σ_{события типа t, t_i<s} exp(-β_t(s-t_i))]."""
    from collections import defaultdict
    by_t = defaultdict(list)
    for e in ev:
        by_t[e["t"]].append(e["s"])
    floor = sum(p["mu"] for p in params.values())
    tot = np.full(n, float(floor))
    for t, times in by_t.items():
        p = params.get(t)
        if not p or p["alpha"] <= 0:
            continue
        tt = np.array(sorted(times), dtype=float)
        for s in range(n):
            past = tt[tt < s]
            if len(past):
                tot[s] += p["alpha"] * p["beta"] * float(np.sum(np.exp(-p["beta"] * (s - past))))
    return [round(float(x), 3) for x in tot]


def gather_full():
    """Полный сбор: события/спаны из исходных разметок (медленно, гоняет regex-детектор)."""
    conflicts = json.load(open(os.path.join(DATA, "conflicts.json")))
    traces = {}
    for c in conflicts:
        key = (c["cell"], c["question_id"])
        if key not in traces:
            mshort = FULL2SHORT.get(c["model"], c["model"])
            traces[key] = {"model_short": mshort, "model": c["model"], "bench": c["benchmark"],
                           "qid": c["question_id"], "domain": c["domain"], "trace_file": c["trace_file"]}
    qwen_idx = qwen_index()
    per_trace = {}
    for key, tr in traces.items():
        pp = bc.payload_for(tr["model_short"], tr["bench"], tr["qid"])
        if not pp:
            continue
        pay = json.load(open(pp))
        ev, agents = events_union(tr["model_short"], tr["bench"], tr["qid"], pay, tr["domain"], qwen_idx)
        sp = spans_for(tr["model_short"], tr["bench"], tr["qid"], qwen_idx)
        per_trace[key] = {"ev": ev, "sp": sp, "agents": agents, "n": len(pay["segments"]),
                          "trace_file": tr["trace_file"], "write_evspans": True}
    return per_trace


def gather_refit():
    """Быстрый: события/спаны берём из уже записанных trace-файлов, пересчитываем только λ."""
    per_trace = {}
    for tpath in glob.glob(os.path.join(TR, "*.json")):
        d = json.load(open(tpath))
        if "events" not in d:
            continue
        per_trace[tpath] = {"ev": d["events"], "sp": d.get("spans", []), "agents": d.get("agents", []),
                            "n": len(d["segments"]), "trace_file": os.path.basename(tpath),
                            "write_evspans": False}
    return per_trace


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refit", action="store_true", help="пересчитать только λ из записанных trace-файлов")
    args = ap.parse_args()

    per_trace = gather_refit() if args.refit else gather_full()
    print(f"{'refit' if args.refit else 'full'}: трасс {len(per_trace)}")

    params_by_type = fit_by_type(per_trace)
    floor = round(sum(p["mu"] for p in params_by_type.values()), 4)
    print("Hawkes по типам:")
    for t, p in sorted(params_by_type.items(), key=lambda kv: -kv[1]["alpha"]):
        print(f"  {t:20} mu={p['mu']:.3f} alpha={p['alpha']:.3f} beta={p['beta']:.3f} (трасс {p['n_seqs']})")
    print(f"floor (Σμ_t) = {floor}")

    lam_max = 0.0; ops_seen = set(); wrote = 0
    for d in per_trace.values():
        lam = lam_total(d["ev"], d["n"], params_by_type)
        lam_max = max(lam_max, max(lam) if lam else floor)
        for s in d["sp"]:
            ops_seen.add(s["op"])
        tpath = os.path.join(TR, d["trace_file"])
        if not os.path.exists(tpath):
            continue
        obj = json.load(open(tpath))
        if d["write_evspans"]:
            obj["events"] = sorted(d["ev"], key=lambda x: (x["s"], x["a"]))
            obj["spans"] = d["sp"]
            obj["agents"] = d["agents"]
        obj["lam"] = lam
        json.dump(obj, open(tpath, "w"), ensure_ascii=False)
        wrote += 1

    meta = {"hawkes_by_type": params_by_type, "floor": floor,
            "axis": "seg_id", "kernel": "alpha*beta*exp(-beta*dt)",
            "lam_max": round(lam_max, 3), "operators": sorted(ops_seen),
            "n_traces": wrote,
            "note": "пер-типовый univariate Hawkes MLE (μ,α,β на тип) на union-событиях всех агентов; λ(s)=Σ_t λ_t(s); развилки = branch/backtrack/failed_attempt"}
    json.dump(meta, open(os.path.join(DATA, "trace_maps_meta.json"), "w"), ensure_ascii=False, indent=1)
    print(f"обновлено trace-файлов: {wrote} | lam_max={lam_max:.2f} | операторы: {sorted(ops_seen)}")


if __name__ == "__main__":
    main()
