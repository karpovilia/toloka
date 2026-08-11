#!/usr/bin/env python3
"""Проверяет, что опубликованный снимок Toloka самосогласован и безопасен для GitHub."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRACE_DIR = DATA / "traces"
GITHUB_FILE_LIMIT = 100 * 1024 * 1024
GITHUB_WARNING_SIZE = 50 * 1024 * 1024
TOL = 1


def load(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def candidate_count(events: list[dict]) -> int:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        by_type[event["t"]].append(event)
    total = 0
    for events_of_type in by_type.values():
        events_of_type.sort(key=lambda event: event["s"])
        used = [False] * len(events_of_type)
        for i, event in enumerate(events_of_type):
            if used[i]:
                continue
            used[i] = True
            total += 1
            for j in range(i + 1, len(events_of_type)):
                if not used[j] and abs(events_of_type[j]["s"] - event["s"]) <= TOL:
                    used[j] = True
    return total


def published_files():
    ignored_parts = {".git", "node_modules", "__pycache__"}
    ignored_paths = {ROOT / "build" / "sites_full.jsonl"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path in ignored_paths or ignored_parts.intersection(path.parts):
            continue
        yield path


def sanitized_qid(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")


def main() -> None:
    errors: list[str] = []
    warnings: list[str] = []
    required = [
        ROOT / "config.json",
        DATA / "event_types.json",
        DATA / "traces_index.json",
        DATA / "conflicts.json",
        DATA / "build_summary.json",
        DATA / "trace_maps_meta.json",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"нет обязательного файла: {path.relative_to(ROOT)}")
    if errors:
        raise SystemExit("\n".join(errors))

    cfg = load(ROOT / "config.json")
    for key in ("event_types", "traces_index", "traces_dir", "trace_maps_meta"):
        rel = cfg.get(key)
        if not rel or not (ROOT / rel).exists():
            errors.append(f"config.{key} указывает на отсутствующий путь: {rel!r}")

    index = load(DATA / "traces_index.json")
    conflicts = load(DATA / "conflicts.json")
    summary = load(DATA / "build_summary.json")
    meta = load(DATA / "trace_maps_meta.json")
    event_model = load(DATA / "event_types.json")

    if not isinstance(index, list) or not isinstance(conflicts, list):
        raise SystemExit("traces_index.json и conflicts.json должны содержать JSON-массивы")

    index_refs = [row.get("trace_file") for row in index]
    invalid_refs = [value for value in index_refs if not isinstance(value, str) or not value.endswith(".json")]
    if invalid_refs:
        errors.append(f"traces_index содержит некорректные trace_file: {invalid_refs[:5]}")
    if len(index_refs) != len(set(index_refs)):
        errors.append("traces_index содержит повторяющиеся trace_file")
    trace_files = {path.name for path in TRACE_DIR.glob("*.json")}
    index_set = {value for value in index_refs if isinstance(value, str)}
    if trace_files != index_set:
        missing = sorted(index_set - trace_files)[:5]
        stale = sorted(trace_files - index_set)[:5]
        errors.append(f"индекс/файлы расходятся: missing={missing}, stale={stale}")

    conflict_ids = [row.get("item_id") for row in conflicts]
    if len(conflict_ids) != len(set(conflict_ids)):
        errors.append(f"conflicts содержит {len(conflict_ids) - len(set(conflict_ids))} повторов item_id")
    conflict_refs = {row.get("trace_file") for row in conflicts if isinstance(row.get("trace_file"), str)}
    if conflict_refs != trace_files:
        errors.append(
            f"выборка conflicts и traces расходится: без сайтов={len(trace_files-conflict_refs)}, "
            f"без файла={len(conflict_refs-trace_files)}"
        )

    if summary.get("sites_in_viewer") != len(conflicts):
        errors.append(f"build_summary.sites_in_viewer={summary.get('sites_in_viewer')} != {len(conflicts)}")
    if summary.get("traces_in_viewer") != len(trace_files):
        errors.append(f"build_summary.traces_in_viewer={summary.get('traces_in_viewer')} != {len(trace_files)}")
    if meta.get("n_traces") != len(trace_files):
        errors.append(f"trace_maps_meta.n_traces={meta.get('n_traces')} != {len(trace_files)}")

    domain_types = {
        domain: set((definition.get("types") or {}).keys())
        for domain, definition in (event_model.get("domains") or {}).items()
    }
    index_by_file = {row["trace_file"]: row for row in index if isinstance(row.get("trace_file"), str)}
    traces_by_file: dict[str, dict] = {}
    for path in sorted(TRACE_DIR.glob("*.json")):
        trace = load(path)
        traces_by_file[path.name] = trace
        row = index_by_file.get(path.name, {})
        segments = trace.get("segments") or []
        events = trace.get("events") or []
        lam = trace.get("lam")
        for key in ("cell", "question_id", "benchmark", "domain", "segments", "events", "spans", "lam", "agents"):
            if key not in trace:
                errors.append(f"{path.name}: нет поля {key}")
        seg_ids = [segment.get("seg_id") for segment in segments]
        if seg_ids != list(range(len(segments))):
            errors.append(f"{path.name}: seg_id должны быть непрерывной осью 0..{len(segments)-1}")
        if lam is not None and len(lam) != len(segments):
            errors.append(f"{path.name}: len(lam)={len(lam)} != segments={len(segments)}")
        if lam is not None and any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in lam):
            errors.append(f"{path.name}: lam содержит нечисловое/отрицательное значение")
        expected_name = f"{trace.get('cell')}__{sanitized_qid(str(trace.get('question_id') or ''))}.json"
        if path.name != expected_name:
            errors.append(f"{path.name}: имя не соответствует cell/question_id ({expected_name})")
        agents = trace.get("agents") or []
        if len(agents) != len(set(agents)):
            errors.append(f"{path.name}: agents содержит повторы")
        allowed_types = domain_types.get(trace.get("domain"), set())
        segment_set = set(seg_ids)
        for event in events:
            if not {"s", "t", "a"}.issubset(event):
                errors.append(f"{path.name}: событие без s/t/a: {event}")
                continue
            if event["s"] not in segment_set:
                errors.append(f"{path.name}: событие вне оси сегментов: {event}")
            if allowed_types and event["t"] not in allowed_types:
                errors.append(f"{path.name}: неизвестный тип {event['t']} для домена {trace.get('domain')}")
            if event["a"] not in agents:
                errors.append(f"{path.name}: агент события {event['a']} отсутствует в agents")
        for span in trace.get("spans") or []:
            if not {"a", "b", "op"}.issubset(span) or not (0 <= span.get("a", -1) < span.get("b", -1) <= len(segments)):
                errors.append(f"{path.name}: некорректный span: {span}")
        expected = {
            "n_segments": len(segments),
            "n_events": len(events),
            "n_candidates": candidate_count(events),
            "agents": agents,
            "types": sorted({event.get("t") for event in events if event.get("t")}),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                errors.append(f"{path.name}: index.{key}={row.get(key)} != {value}")
        if len(errors) >= 50:
            break

    for conflict in conflicts:
        tf = conflict.get("trace_file")
        trace = traces_by_file.get(tf)
        if not trace:
            continue
        for key in ("cell", "question_id", "benchmark", "domain"):
            if conflict.get(key) != trace.get(key):
                errors.append(f"{conflict.get('item_id')}: {key} расходится с {tf}")
        if conflict.get("seg_id") not in {segment.get("seg_id") for segment in trace.get("segments", [])}:
            errors.append(f"{conflict.get('item_id')}: seg_id вне трассы {tf}")
        if len(errors) >= 50:
            break

    total_bytes = 0
    for path in published_files():
        size = path.stat().st_size
        total_bytes += size
        if size >= GITHUB_FILE_LIMIT:
            errors.append(f"файл достигает hard limit GitHub 100 MiB: {path.relative_to(ROOT)}")
        elif size >= GITHUB_WARNING_SIZE:
            warnings.append(f"файл больше рекомендованных GitHub 50 MiB: {path.relative_to(ROOT)}")

    if errors:
        raise SystemExit("Toloka data validation failed:\n- " + "\n- ".join(errors))
    for warning in warnings:
        print("WARNING:", warning)
    print(
        f"Toloka data OK: {len(trace_files)} traces, {len(conflicts)} selected conflict sites, "
        f"{sum(row['n_candidates'] for row in index)} event candidates, {total_bytes / 2**20:.1f} MiB published snapshot"
    )


if __name__ == "__main__":
    main()
