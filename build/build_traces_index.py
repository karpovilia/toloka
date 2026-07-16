"""
Строит data/traces_index.json — лёгкий список трасс для вьюера v2 (верификация событий на трассе).
Читает уже собранные data/traces/*.json (segments/events/agents) и data/conflicts.json (для slice).
Кандидат события = кластер событий одного типа в окне ±1 сегмента (agents = union).
"""
import json, os, glob
from collections import defaultdict

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TR = os.path.join(DATA, "traces")
TOL = 1


def candidates(events):
    """events: [{s,t,a}] -> список кандидатов [{seg,type,agents}] (кластер по типу ±TOL)."""
    by_type = defaultdict(list)
    for e in events:
        by_type[e["t"]].append(e)
    cands = []
    for t, evs in by_type.items():
        evs.sort(key=lambda e: e["s"])
        used = [False] * len(evs)
        for i in range(len(evs)):
            if used[i]:
                continue
            segs = [evs[i]["s"]]; agents = {evs[i]["a"]}; used[i] = True
            for j in range(i + 1, len(evs)):
                if not used[j] and abs(evs[j]["s"] - evs[i]["s"]) <= TOL:
                    segs.append(evs[j]["s"]); agents.add(evs[j]["a"]); used[j] = True
            anchor = max(set(segs), key=segs.count)
            cands.append({"seg": anchor, "type": t, "agents": sorted(agents)})
    cands.sort(key=lambda c: (c["seg"], c["type"]))
    return cands


def main():
    slice_of = {}
    try:
        for c in json.load(open(os.path.join(DATA, "conflicts.json"))):
            slice_of[c["trace_file"]] = c.get("slice")
    except Exception:
        pass

    idx = []
    for f in sorted(glob.glob(os.path.join(TR, "*.json"))):
        d = json.load(open(f))
        tf = os.path.basename(f)
        cands = candidates(d.get("events", []))
        idx.append({
            "trace_file": tf, "cell": d.get("cell"), "question_id": d.get("question_id"),
            "benchmark": d.get("benchmark"), "domain": d.get("domain"),
            "model": (d.get("cell") or "__").split("__")[0],
            "slice": slice_of.get(tf), "n_segments": len(d.get("segments", [])),
            "n_events": len(d.get("events", [])), "n_candidates": len(cands),
            "agents": d.get("agents", []),
        })
    idx.sort(key=lambda r: (r["benchmark"] or "", r["cell"] or "", r["question_id"] or ""))
    json.dump(idx, open(os.path.join(DATA, "traces_index.json"), "w"), ensure_ascii=False)
    print(f"traces_index.json: {len(idx)} трасс")
    from collections import Counter
    print("по бенчам:", dict(Counter(r["benchmark"] for r in idx)))
    print("по срезам:", dict(Counter(r["slice"] for r in idx)))
    print("кандидатов всего:", sum(r["n_candidates"] for r in idx))


if __name__ == "__main__":
    main()
