"""Score the detector against a benchmark dataset.

Run from the repository root:

    python benchmarks/score.py

Reports span-level precision, recall and F1 per PII type and overall, plus
false positives on negative cases.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import cast

import yaml

# Make the project root importable when run as `python benchmarks/score.py`.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.models import DetectionContext  # noqa: E402
from app.detection.context.resolver import ContextResolver  # noqa: E402
from app.detection.engine import DetectionEngine  # noqa: E402
from app.detection.regex.detector import RegexDetector  # noqa: E402

_DATASET_PATH = Path(__file__).resolve().parent / "dataset.yaml"
_F1_THRESHOLD = 0.7
# Per-type recall floor. A type that appears in the dataset but is never
# detected (recall == 0.0) must fail the benchmark even if the overall F1 is
# green, so a mandatory PII type cannot silently regress to zero recall.
_MIN_RECALL_PER_TYPE = 0.1


def _load_dataset() -> list[dict[str, object]]:
    with _DATASET_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return cast(list[dict[str, object]], data)


def _run_engine() -> DetectionEngine:
    return DetectionEngine([RegexDetector()], ContextResolver())


def _span_key(pii_type: str, start: int, end: int) -> tuple[str, int, int]:
    return (pii_type, start, end)


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
        found_spans = {
            _span_key(e.type.value, e.start, e.end): e.value for e in found
        }
        expected_spans = {
            _span_key(str(e["type"]), int(e["start"]), int(e["end"])): str(e["text"])
            for e in expected
        }

        for key, value in found_spans.items():
            pii_type = key[0]
            if key in expected_spans:
                tp[pii_type] += 1
            else:
                fp[pii_type] += 1
                if not expected_spans:
                    false_positives.append((case_id, pii_type, value))

        for key in expected_spans:
            pii_type = key[0]
            if key not in found_spans:
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

    violations: list[str] = []
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
        if t + n > 0 and recall < _MIN_RECALL_PER_TYPE:
            violations.append(
                f"{pii_type} recall {recall:.3f} < {_MIN_RECALL_PER_TYPE:.1f}"
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

    if violations:
        print("\nPer-type recall violations:")
        for violation in violations:
            print(f"  {violation}")

    return 1 if (f1 < _F1_THRESHOLD or violations) else 0


if __name__ == "__main__":
    sys.exit(_main())