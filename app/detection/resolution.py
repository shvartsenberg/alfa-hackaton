"""Resolution of overlapping PII entities into non-overlapping spans."""

from __future__ import annotations

from app.core.enums import PIIType
from app.core.models import PIIEntity

_PRIORITIES: dict[PIIType, int] = {
    PIIType.BANK_CARD: 90,
    PIIType.INN: 90,
    PIIType.PASSPORT_NUMBER: 90,
    PIIType.PASSPORT_DIVISION_CODE: 90,
    PIIType.DRIVER_LICENSE: 90,
    PIIType.PHONE: 80,
    PIIType.EMAIL: 80,
    PIIType.ADDRESS: 70,
    PIIType.PERSON_NAME: 60,
    PIIType.CARDHOLDER_NAME: 60,
    PIIType.BIRTH_PLACE: 60,
    PIIType.CITIZENSHIP: 60,
    PIIType.COUNTRY: 60,
    PIIType.CITY: 60,
    PIIType.STREET: 60,
    PIIType.POSTAL_CODE: 60,
    PIIType.PASSPORT_ISSUER: 60,
    PIIType.PASSPORT_ISSUE_DATE: 60,
    PIIType.CVV: 30,
    PIIType.PIN: 30,
    PIIType.HOUSE: 30,
    PIIType.APARTMENT: 30,
}

_DEFAULT_PRIORITY = 50


def _priority(pii_type: PIIType) -> int:
    return _PRIORITIES.get(pii_type, _DEFAULT_PRIORITY)


def _overlaps(a: PIIEntity, b: PIIEntity) -> bool:
    return a.start < b.end and b.start < a.end


def _better(a: PIIEntity, b: PIIEntity) -> PIIEntity:
    pa, pb = _priority(a.type), _priority(b.type)
    if pa != pb:
        return a if pa > pb else b
    if a.confidence != b.confidence:
        return a if a.confidence > b.confidence else b
    la, lb = a.end - a.start, b.end - b.start
    return a if la >= lb else b


def resolve_spans(entities: list[PIIEntity]) -> list[PIIEntity]:
    """Return non-overlapping entities sorted by start ascending.

    Deduplicates by (type, start, end) keeping the highest confidence, then
    resolves overlaps by type priority, confidence, and span length.
    """
    best: dict[tuple[PIIType, int, int], PIIEntity] = {}
    for entity in entities:
        key = (entity.type, entity.start, entity.end)
        current = best.get(key)
        if current is None or entity.confidence > current.confidence:
            best[key] = entity

    unique = sorted(best.values(), key=lambda e: (e.start, e.end))
    resolved: list[PIIEntity] = []
    for entity in unique:
        if resolved and _overlaps(resolved[-1], entity):
            resolved[-1] = _better(resolved[-1], entity)
        else:
            resolved.append(entity)
    return resolved


if __name__ == "__main__":
    from app.core.models import DetectionContext

    ctx = DetectionContext()
    nested = [
        PIIEntity(PIIType.PHONE, "89991234567", 0, 11, detector="regex"),
        PIIEntity(PIIType.PIN, "1234", 3, 7, detector="regex"),
    ]
    assert len(resolve_spans(nested)) == 1

    partial = [
        PIIEntity(PIIType.EMAIL, "a@b.ru", 0, 6, detector="regex"),
        PIIEntity(PIIType.PHONE, "89991234567", 4, 15, detector="regex"),
    ]
    assert len(resolve_spans(partial)) == 1

    unsorted = [
        PIIEntity(PIIType.INN, "7707083893", 10, 20, detector="regex"),
        PIIEntity(PIIType.EMAIL, "a@b.ru", 0, 6, detector="regex"),
    ]
    result = resolve_spans(unsorted)
    assert [e.start for e in result] == sorted(e.start for e in result)