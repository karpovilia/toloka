#!/usr/bin/env python3
"""Проверяет, что срез native-PO не опубликовал ничего из GPQA-diamond.

Репозиторий и GitHub Pages публичные, а условия доступа к GPQA-diamond закрытые, поэтому
трассы по вопросам этого бенчмарка в срез native-PO не попадают вовсе: ни идентификатор
вопроса, ни его текст, ни текст рассуждения модели. Скрипт сверяет опубликованный снимок
с исходным корпусом internal_signals_poc/native_po_2026_08_11 по трём каналам:

  1. имена файлов, поля benchmark и question_id в трассах среза, в его строках конфликтов
     и в строках индекса вьюера;
  2. идентификаторы GPQA-вопросов корпуса — их не должно быть ни в одном файле среза;
  3. тексты GPQA-трасс (вопрос и сегменты рассуждения) — берутся длинные фрагменты, из них
     вычитаются фрагменты, встречающиеся и в не-GPQA трассах корпуса (общие шаблонные куски
     инструкции), остаток ищется в опубликованных трассах среза. Это ловит утечку текста
     даже без идентификатора вопроса.

ВАЖНАЯ ОГОВОРКА. Старый корпус вьюера (модели gemma/qwen/gpt-oss, имена файлов без префикса
npo-) содержит собственные gpqa_diamond-трассы, опубликованные ещё в июле, и часть вопросов
у двух корпусов общая. Эта проверка их не трогает — она отвечает только за срез native-PO;
объём старой публикации печатается отдельной справочной строкой.

Запуск: python3 build/check_no_gpqa.py  (код возврата 0 = срез чист).
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TR = DATA / "traces"
PREFIX = "npo-"
CORPUS = "native_po_2026_08_11"
ARM = ROOT.parent / "internal_signals_poc" / CORPUS
JUDGE_PAYLOADS = ARM / "judge_payloads"
FRAGMENT = 120         # длина фрагмента текста, по которому ищется утечка
IGNORED_PARTS = {".git", "node_modules", "__pycache__"}
IGNORED_PATHS = {ROOT / "build" / "sites_full.jsonl"}     # в gitignore, не публикуется


def norm(text: str) -> str:
    return " ".join((text or "").split())


def gpqa_source():
    """Идентификаторы и текстовые фрагменты GPQA-трасс, уникальные для GPQA.

    Модели цитируют в рассуждении общую инструкцию про формат ответа, поэтому фрагмент,
    который встречается и в не-GPQA трассах корпуса, уликой быть не может: он вычитается
    (подстрокой, а не только по началу сегмента), иначе проверка ловит шаблон вместо утечки.
    """
    qids, gpqa_frags, other_texts = set(), set(), []
    for path in sorted(JUDGE_PAYLOADS.glob("*/*.json")):
        if path.name.startswith("_"):
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        is_gpqa = rec.get("benchmark") == "gpqa_diamond"
        if is_gpqa:
            qids.add(rec["question_id"])
        for text in [rec.get("question", "")] + [s["text"] for s in rec.get("segments", [])]:
            text = norm(text)
            if len(text) < FRAGMENT:
                continue
            if is_gpqa:
                gpqa_frags.add(text[:FRAGMENT])
            else:
                other_texts.append(text)
    public_blob = "\n".join(other_texts)
    return qids, {frag for frag in gpqa_frags if frag not in public_blob}


def slice_files():
    """Файлы, целиком принадлежащие срезу native-PO."""
    return sorted(TR.glob(PREFIX + "*.json"))


def published_files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path not in IGNORED_PATHS \
           and not IGNORED_PARTS.intersection(path.parts):
            yield path


def main() -> None:
    # В CI исходного корпуса нет (он лежит вне репозитория), поэтому там остаётся структурная
    # часть проверки: имена файлов, поля benchmark и сводка сборки. Полная сверка с текстами
    # и идентификаторами вопросов делается локально, где корпус доступен.
    have_source = JUDGE_PAYLOADS.is_dir()
    if have_source:
        qids, fragments = gpqa_source()
        if not qids:
            raise SystemExit("в исходном корпусе не нашлось GPQA-вопросов — проверка бессмысленна")
    else:
        qids, fragments = set(), set()
        print(f"исходного корпуса нет ({JUDGE_PAYLOADS}) — структурная часть проверки")

    errors: list[str] = []

    # --- 1. трассы среза: имя файла, benchmark, идентификаторы, тексты ---
    traces = slice_files()
    for path in traces:
        if "gpqa" in path.name.lower():
            errors.append(f"имя файла среза содержит gpqa: {path.name}")
        blob = path.read_text(encoding="utf-8")
        for qid in qids:
            if qid in blob:
                errors.append(f"{path.name}: идентификатор GPQA-вопроса {qid}")
        trace = json.loads(blob)
        if trace.get("benchmark") == "gpqa_diamond":
            errors.append(f"{path.name}: benchmark=gpqa_diamond")
        text = norm(" ".join([trace.get("question", "")]
                             + [s["text"] for s in trace.get("segments", [])]))
        for frag in fragments:
            if frag in text:
                errors.append(f"{path.name}: текст GPQA-трассы — «{frag[:50]}…»")

    # --- 2. строки среза в общих файлах снимка ---
    conflicts = json.loads((DATA / "conflicts.json").read_text(encoding="utf-8"))
    rows = [row for row in conflicts if row.get("corpus") == CORPUS]
    for row in rows:
        blob = json.dumps(row, ensure_ascii=False)
        if row.get("benchmark") == "gpqa_diamond" or any(qid in blob for qid in qids):
            errors.append(f"строка конфликта среза ссылается на GPQA: {row.get('item_id')}")

    index = json.loads((DATA / "traces_index.json").read_text(encoding="utf-8"))
    index_rows = [row for row in index if str(row.get("trace_file", "")).startswith(PREFIX)]
    for row in index_rows:
        blob = json.dumps(row, ensure_ascii=False)
        if row.get("benchmark") == "gpqa_diamond" or any(qid in blob for qid in qids):
            errors.append(f"строка индекса среза ссылается на GPQA: {row.get('trace_file')}")

    summary = json.loads((DATA / "build_summary.json").read_text(encoding="utf-8"))
    npo = summary.get("nativepo") or {}
    if "gpqa_diamond" in (npo.get("benchmarks") or {}):
        errors.append("build_summary.nativepo.benchmarks содержит gpqa_diamond")

    # --- 3. справочно: сколько GPQA-идентификаторов есть в публикации СТАРОГО корпуса ---
    legacy_traces = sorted(TR.glob("*__gpqa_diamond__*.json"))
    legacy_hits = set()
    for path in published_files():
        if path in traces:
            continue
        try:
            blob = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        legacy_hits.update(qid for qid in qids if qid in blob)

    print(f"срез native-PO: трасс {len(traces)}, строк конфликтов {len(rows)}, "
          f"строк индекса {len(index_rows)}, бенчмарки {npo.get('benchmarks')}")
    if have_source:
        print(f"эталон: GPQA-вопросов в исходном корпусе {len(qids)}, "
              f"уникальных текстовых фрагментов для сверки {len(fragments)}")
        print(f"справочно (не ошибка): в СТАРОМ корпусе снимка {len(legacy_traces)} "
              f"gpqa_diamond-трасс, из общих с native-PO вопросов там встречается "
              f"{len(legacy_hits)} идентификаторов — это публикация июля, к срезу отношения не имеет")
    if errors:
        raise SystemExit("ПРОВЕРКА GPQA НЕ ПРОЙДЕНА:\n- " + "\n- ".join(errors[:50]))
    print("GPQA в срезе native-PO НЕ НАЙДЕНО: ни идентификаторов вопросов, ни текстов "
          "вопросов, ни текстов рассуждений" if have_source else
          "структурно чисто: в срезе нет ни файла, ни строки с gpqa_diamond")


if __name__ == "__main__":
    main()
