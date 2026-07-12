# Разметки аннотаторов

Сюда кладутся выгрузки из вьюера: `annot_<ник>.json` (кнопка «экспорт разметки»).

Формат:

    {
      "annotator_id": "<ник>",
      "exported": "<iso-время>",
      "tool": "toloka",
      "annotations": {
        "<item_id>": {"verdict": "<тип|∅|unclear>", "notes": "..."},
        ...
      }
    }

После добавления файла — `git commit && git push`, затем `python3 ../build/score_agents.py`
пересчитает score по всем разметкам в этой папке.
