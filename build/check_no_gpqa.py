#!/usr/bin/env python3
"""Проверяет, что публикуемый снимок не содержит ничего из GPQA-diamond.

Репозиторий и GitHub Pages публичные, а условия доступа к GPQA-diamond закрытые, поэтому
трассы по вопросам этого бенчмарка не публикуются вовсе: ни идентификатор вопроса, ни его
текст, ни текст рассуждения модели. С 2026-08-13 проверка отвечает за ВЕСЬ снимок, а не
только за срез native-PO: старый конфликт-корпус вьюера, который публиковал 170
gpqa_diamond-трасс, снят с публикации и унесён в приватный архив
internal_signals_poc/toloka_archive_2026_08_13.

Эталонов два, оба лежат вне репозитория и оба необязательны:

  - сам бенчмарк data/reasonops_benchmarks/gpqa/gpqa_diamond.csv — 198 вопросов: их
    идентификаторы записи, канареечные строки, тексты вопросов и разборов. Эталон не зависит
    от того, какой корпус трасс публикуется, поэтому ловит и замаскированную утечку: трассу
    с подменёнными полями бенчмарка, но с исходным текстом вопроса;
  - исходный корпус среза native_po_2026_08_11 — тексты рассуждений его моделей на
    GPQA-вопросах. Из улик вычитаются фрагменты, встречающиеся и в не-GPQA трассах корпуса:
    модели цитируют общую инструкцию про формат ответа, такой кусок уликой быть не может.

Что проверяется:

  1. Структурно, без эталонов (эта часть работает и в CI):
     - ни один файл трассы не назван так, что в имени есть gpqa;
     - ни у одной трассы, ни у одной строки конфликтов и индекса нет benchmark=gpqa_diamond;
     - каждая опубликованная трасса принадлежит срезу native-PO (поле corpus): трасса чужого
       корпуса — это и есть возврат старого корпуса, который публиковал GPQA;
     - в сводке сборки нет бенчмарка gpqa_diamond.
  2. По эталонам: идентификаторы записей, канареечные строки и идентификаторы вопросов не
     встречаются ни в одном публикуемом файле; тексты вопросов, разборов и рассуждений не
     встречаются в публикуемых текстах — ни в трассах, ни в цитатах конфликт-сайтов, ни в
     остальных файлах снимка.

Крупные файлы снимка проверяются по своей структуре, мелкие сканируются подстрокой целиком;
файл неизвестной структуры крупнее порога — ошибка, чтобы проверка не молчала о непокрытом
месте.

Запуск: python3 build/check_no_gpqa.py  (код возврата 0 = снимок чист).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TR = DATA / "traces"
CORPUS = "native_po_2026_08_11"
BASE = ROOT.parent
BENCHMARK = BASE / "data" / "reasonops_benchmarks" / "gpqa" / "gpqa_diamond.csv"
JUDGE_PAYLOADS = BASE / "internal_signals_poc" / CORPUS / "judge_payloads"
FRAGMENT = 120          # длина фрагмента текста, по которому ищется утечка
QUOTE_MIN = 60          # короткие цитаты не улика: это общие обороты рассуждения
SMALL_FILE = 2 * 2**20  # файлы меньше порога сканируются подстрокой целиком
IGNORED_PARTS = {".git", "node_modules", "__pycache__"}
IGNORED_PATHS = {ROOT / "build" / "sites_full.jsonl"}     # в gitignore, не публикуется
STRUCTURED_BIG = {DATA / "conflicts.json"}                # проверяется по своей структуре
BENCH_TEXT_COLUMNS = ("Question", "Explanation", "Pre-Revision Question",
                      "Pre-Revision Explanation", "Extra Revised Question",
                      "Extra Revised Explanation")


def norm(text: str) -> str:
    return " ".join((text or "").split())


def benchmark_source():
    """Идентификаторы и текстовые фрагменты самого GPQA-diamond."""
    ids, fragments = set(), set()
    csv.field_size_limit(10**7)
    with BENCHMARK.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for key in ("Record ID", "Canary String"):
                value = norm(row.get(key, ""))
                if value:
                    ids.add(value)
            for key in BENCH_TEXT_COLUMNS:
                text = norm(row.get(key, ""))
                if len(text) >= FRAGMENT:
                    fragments.add(text[:FRAGMENT])
    return ids, fragments


def corpus_source():
    """Идентификаторы, тексты и начальные фрагменты GPQA-трасс исходного корпуса среза."""
    qids, gpqa_texts, other_texts = set(), set(), []
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
                gpqa_texts.add(text)
            else:
                other_texts.append(text)
    public_blob = "\n".join(other_texts)
    gpqa_texts = {text for text in gpqa_texts if text[:FRAGMENT] not in public_blob}
    return qids, gpqa_texts


def published_files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path not in IGNORED_PATHS \
           and not IGNORED_PARTS.intersection(path.parts):
            yield path


def main() -> None:
    errors: list[str] = []
    identifiers: set[str] = set()      # ищутся подстрокой в любом публикуемом файле
    fragments: set[str] = set()        # ищутся подстрокой в публикуемых текстах
    exact_texts: set[str] = set()      # сверяются на равенство с текстом сегмента
    prefixes: set[str] = set()         # сверяются с началом текста сегмента

    if BENCHMARK.is_file():
        bench_ids, bench_frags = benchmark_source()
        if not bench_ids:
            raise SystemExit(f"эталон-бенчмарк пуст: {BENCHMARK}")
        identifiers |= bench_ids
        fragments |= bench_frags
        print(f"эталон-бенчмарк {BENCHMARK}: идентификаторов {len(bench_ids)}, "
              f"текстовых фрагментов {len(bench_frags)}")
    else:
        print(f"эталона-бенчмарка нет ({BENCHMARK}) — эта часть проверки пропущена")

    if JUDGE_PAYLOADS.is_dir():
        corpus_qids, corpus_texts = corpus_source()
        if not corpus_qids:
            raise SystemExit("в исходном корпусе не нашлось GPQA-вопросов — эталон бессмысленен")
        identifiers |= corpus_qids
        exact_texts |= corpus_texts
        prefixes |= {text[:FRAGMENT] for text in corpus_texts}
        print(f"эталон-корпус {CORPUS}: GPQA-вопросов {len(corpus_qids)}, текстов рассуждений "
              f"{len(corpus_texts)}")
    else:
        corpus_texts = set()
        print(f"эталона-корпуса нет ({JUDGE_PAYLOADS}) — эта часть проверки пропущена")

    if not identifiers and not fragments:
        print("эталонов нет — остаётся структурная часть проверки")
    gpqa_blob = "\n".join(sorted(corpus_texts | fragments))

    def check_text(where: str, text: str) -> None:
        text = norm(text)
        if not text:
            return
        if text in exact_texts or (len(text) >= FRAGMENT and text[:FRAGMENT] in prefixes):
            errors.append(f"{where}: текст GPQA-трассы — «{text[:50]}…»")
            return
        for fragment in fragments:
            if fragment in text:
                errors.append(f"{where}: текст вопроса GPQA — «{fragment[:50]}…»")
                return

    def check_ids(where: str, blob: str) -> None:
        for identifier in identifiers:
            if identifier in blob:
                errors.append(f"{where}: идентификатор GPQA {identifier}")

    # --- 1. трассы снимка: имя файла, бенчмарк, корпус, идентификаторы, тексты ---
    traces = sorted(TR.glob("*.json"))
    for path in traces:
        if "gpqa" in path.name.lower():
            errors.append(f"имя файла трассы содержит gpqa: {path.name}")
        blob = path.read_text(encoding="utf-8")
        check_ids(path.name, blob)
        trace = json.loads(blob)
        if trace.get("benchmark") == "gpqa_diamond":
            errors.append(f"{path.name}: benchmark=gpqa_diamond")
        if trace.get("corpus") != CORPUS:
            errors.append(f"{path.name}: трасса вне среза native-PO (corpus={trace.get('corpus')!r}) "
                          "— публикуется только срез, старый корпус в приватном архиве")
        check_text(path.name, trace.get("question", ""))
        for segment in trace.get("segments", []):
            check_text(path.name, segment.get("text", ""))

    # --- 2. строки конфликтов: бенчмарк, корпус, идентификаторы, цитаты ---
    conflicts_blob = (DATA / "conflicts.json").read_text(encoding="utf-8")
    check_ids("data/conflicts.json", conflicts_blob)
    conflicts = json.loads(conflicts_blob)
    for row in conflicts:
        item = row.get("item_id")
        if row.get("benchmark") == "gpqa_diamond":
            errors.append(f"строка конфликта на GPQA: {item}")
        if row.get("corpus") != CORPUS:
            errors.append(f"строка конфликта вне среза native-PO: {item} (corpus={row.get('corpus')!r})")
        for quotes in (row.get("quotes") or {}).values():
            for quote in quotes or []:
                quote = norm(quote)
                if len(quote) < QUOTE_MIN:
                    continue          # короткая цитата не улика: это общий оборот рассуждения
                check_text(f"цитата в {item}", quote)     # эталонный фрагмент внутри цитаты
                if quote in gpqa_blob:                    # цитата внутри эталонного текста
                    errors.append(f"{item}: цитата из GPQA-трассы — «{quote[:50]}…»")

    # --- 3. строки индекса вьюера ---
    index = json.loads((DATA / "traces_index.json").read_text(encoding="utf-8"))
    for row in index:
        trace_file = str(row.get("trace_file", ""))
        if row.get("benchmark") == "gpqa_diamond" or "gpqa" in trace_file.lower():
            errors.append(f"строка индекса ссылается на GPQA: {trace_file}")

    # --- 4. сводка сборки ---
    summary = json.loads((DATA / "build_summary.json").read_text(encoding="utf-8"))
    if "gpqa_diamond" in (summary.get("benchmarks") or {}):
        errors.append("build_summary.benchmarks содержит gpqa_diamond")

    # --- 5. все прочие публикуемые файлы ---
    scanned = 0
    for path in published_files():
        if path in traces or path in STRUCTURED_BIG:
            continue
        if path.stat().st_size > SMALL_FILE:
            errors.append(f"файл крупнее {SMALL_FILE // 2**20} MiB не покрыт проверкой по структуре: "
                          f"{path.relative_to(ROOT)}")
            continue
        blob = norm(path.read_text(encoding="utf-8", errors="ignore"))
        scanned += 1
        where = str(path.relative_to(ROOT))
        check_ids(where, blob)
        for fragment in fragments | prefixes:
            if fragment in blob:
                errors.append(f"{where}: текст GPQA — «{fragment[:50]}…»")

    print(f"снимок: трасс {len(traces)}, строк конфликтов {len(conflicts)}, строк индекса "
          f"{len(index)}, прочих публикуемых файлов просканировано {scanned}, "
          f"бенчмарки {summary.get('benchmarks')}")
    if errors:
        raise SystemExit("ПРОВЕРКА GPQA НЕ ПРОЙДЕНА:\n- " + "\n- ".join(errors[:50]))
    print("GPQA в снимке НЕ НАЙДЕНО: ни идентификаторов записей и вопросов, ни канареечных "
          "строк, ни текстов вопросов, ни текстов рассуждений"
          if identifiers else
          "структурно чисто: ни файла, ни строки с gpqa_diamond, все трассы — срез native-PO")


if __name__ == "__main__":
    sys.exit(main())
