"""Валидаторы для детекции данных."""

from __future__ import annotations

from datetime import date, datetime

__all__ = [
    "digits",
    "luhn",
    "inn_checksum",
    "parse_date",
    "is_birth_date",
]


def digits(s: str) -> str:
    """Вернуть только цифры из строки."""
    return "".join(ch for ch in s if ch.isdigit())


def luhn(s: str) -> bool:
    """Проверить номер по алгоритму Луна (длина 13..19)."""
    num = digits(s)
    if not 13 <= len(num) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def inn_checksum(s: str) -> bool:
    """Проверить контрольную сумму ИНН РФ (10 или 12 цифр)."""
    num = digits(s)
    if len(num) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        check = sum(int(num[i]) * weights[i] for i in range(9)) % 11 % 10
        return check == int(num[9])
    if len(num) == 12:
        weights_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        weights_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        check_1 = sum(int(num[i]) * weights_1[i] for i in range(10)) % 11 % 10
        check_2 = sum(int(num[i]) * weights_2[i] for i in range(11)) % 11 % 10
        return check_1 == int(num[10]) and check_2 == int(num[11])
    return False


def parse_date(s: str) -> date | None:
    """Разобрать дату из строки в нескольких форматах."""
    text = s.strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return date(parsed.year, parsed.month, parsed.day)
    parts = text.split()
    if len(parts) == 3:
        months = {
            "января": 1,
            "февраля": 2,
            "марта": 3,
            "апреля": 4,
            "мая": 5,
            "июня": 6,
            "июля": 7,
            "августа": 8,
            "сентября": 9,
            "октября": 10,
            "ноября": 11,
            "декабря": 12,
        }
        day, month_name, year = parts
        month = months.get(month_name.lower())
        if month is None:
            return None
        try:
            return date(int(year), month, int(day))
        except ValueError:
            return None
    return None


def is_birth_date(s: str) -> bool:
    """Проверить, что строка — дата рождения (прошлое, возраст <= 120)."""
    parsed = parse_date(s)
    if parsed is None:
        return False
    today = date.today()
    if parsed >= today:
        return False
    age = today.year - parsed.year - (
        (today.month, today.day) < (parsed.month, parsed.day)
    )
    return age <= 120


if __name__ == "__main__":
    assert luhn("4111 1111 1111 1111")
    assert not luhn("4111 1111 1111 1112")
    assert inn_checksum("7707083893")
    assert inn_checksum("500100732259")
    assert parse_date("31.02.1990") is None
    assert not is_birth_date("12.03.2099")