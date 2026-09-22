"""Score the regex detector against a benchmark dataset.

Run: python benchmarks/score.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import cast

import yaml

from app.core.models import DetectionContext
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector

_DATASET_PATH = Path(__file__).resolve().parent / "dataset.yaml"
_F1_THRESHOLD = 0.7


def _load_dataset() -> list[dict[str, object]]:
    with _DATASET_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return cast(list[dict[str, object]], data)


def _run_engine() -> DetectionEngine:
    return DetectionEngine([RegexDetector()], ContextResolver())


def _score() -> tuple[
    tuple[dict[str, int], dict[str, int], dict[str, int]],
    list[tuple[str, str, str]],
]:
    cases = _load_dataset()
    engine = _run_engine()
    context = DetectionContext()

    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    false_positives: list[tuple[str, str, str]] = []

    for case in cases:
        case_id = str(case["id"])
        text = str(case["text"])
        expected = case["expected"]
        assert isinstance(expected, list)

        found = engine.detect(text, context).entities
        found_set = {(e.type.value, e.value) for e in found}
        expected_set = {(e["type"], e["text"]) for e in expected}

        for pii_type, value in found_set:
            if (pii_type, value) in expected_set:
                tp[pii_type] += 1
            else:
                fp[pii_type] += 1
                if not expected_set:
                    false_positives.append((case_id, pii_type, value))

        for pii_type, value in expected_set:
            if (pii_type, value) not in found_set:
                fn[pii_type] += 1

    return (tp, fp, fn), false_positives


def _fmt_metric(value: float) -> str:
    return f"{value:.3f}"


def _main() -> int:
    (tp, fp, fn), false_positives = _score()

    types = sorted(set(tp) | set(fp) | set(fn))
    print(f"{'тип':<22}{'TP':>4}{'FP':>4}{'FN':>4}{'precision':>11}{'recall':>9}{'F1':>8}")

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())

    for pii_type in types:
        t = tp[pii_type]
        f = fp[pii_type]
        n = fn[pii_type]
        precision = t / (t + f) if t + f else 0.0
        recall = t / (t + n) if t + n else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        print(
            f"{pii_type:<22}{t:>4}{f:>4}{n:>4}"
            f"{_fmt_metric(precision):>11}{_fmt_metric(recall):>9}{_fmt_metric(f1):>8}"
        )

    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    print(
        f"{'TOTAL':<22}{total_tp:>4}{total_fp:>4}{total_fn:>4}"
        f"{_fmt_metric(precision):>11}{_fmt_metric(recall):>9}{_fmt_metric(f1):>8}"
    )

    if false_positives:
        print("\nFalse positives на негативных кейсах:")
        for case_id, pii_type, value in false_positives:
            print(f"  {case_id}: {pii_type} = {value}")

    return 1 if f1 < _F1_THRESHOLD else 0


if __name__ == "__main__":
    sys.exit(_main())