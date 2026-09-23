"""Unit tests for the regex detector."""

from __future__ import annotations

import pytest

from app.core.enums import PIIType
from app.core.models import DetectionContext
from app.detection.regex.detector import RegexDetector


def test_detects_email() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Contact test@example.com please", DetectionContext()
    )
    assert len(entities) == 1
    assert entities[0].type == PIIType.EMAIL
    assert entities[0].value == "test@example.com"
    assert entities[0].start == 8
    assert entities[0].end == 24


def test_detects_phone() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Call +7 999 123-45-67 now", DetectionContext()
    )
    assert len(entities) == 1
    assert entities[0].type == PIIType.PHONE
    assert entities[0].value == "+7 999 123-45-67"


def test_detects_multiple_entities() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Email a@b.com and phone +7 999 123-45-67", DetectionContext()
    )
    assert len(entities) == 2
    assert {e.type for e in entities} == {PIIType.EMAIL, PIIType.PHONE}


def test_no_false_positive_on_plain_text() -> None:
    detector = RegexDetector()
    entities = detector.detect("Hello world, nothing here", DetectionContext())
    assert entities == []


@pytest.mark.parametrize(
    ("text", "pii_type", "value"),
    [
        (
            "Клиент Иванов Иван Иванович обратился в банк.",
            PIIType.PERSON_NAME,
            "Иванов Иван Иванович",
        ),
        ("Гражданин Петров Пётр подписал договор.", PIIType.PERSON_NAME, "Петров Пётр"),
        ("Место рождения: город Москва.", PIIType.BIRTH_PLACE, "Москва"),
        (
            "Гражданство Российская Федерация подтверждено.",
            PIIType.CITIZENSHIP,
            "Российская Федерация",
        ),
        (
            "Паспорт выдан Отделом УФМС России по г. Москве.",
            PIIType.PASSPORT_ISSUER,
            "Отделом УФМС России по г. Москве",
        ),
        (
            "Дата выдачи 15.03.2015 указана в паспорте.",
            PIIType.PASSPORT_ISSUE_DATE,
            "15.03.2015",
        ),
        (
            "Водительское удостоверение 77 12 345678 выдано в 2018 году.",
            PIIType.DRIVER_LICENSE,
            "77 12 345678",
        ),
        (
            "Адрес: г. Москва, ул. Тверская, д. 12, кв. 34.",
            PIIType.ADDRESS,
            "г. Москва, ул. Тверская, д. 12, кв. 34",
        ),
        ("Страна: Россия, регион проживания указан ниже.", PIIType.COUNTRY, "Россия"),
        ("Почтовый индекс 101000 для корреспонденции.", PIIType.POSTAL_CODE, "101000"),
        ("Город Казань, улица Баумана.", PIIType.CITY, "Казань"),
        ("Улица Ленина, дом 10.", PIIType.STREET, "Ленина"),
        ("Дом 12, квартира 34.", PIIType.HOUSE, "12"),
        ("Квартира 34, подъезд 2.", PIIType.APARTMENT, "34"),
        (
            "Держатель карты IVANOV IVAN указан на лицевой стороне.",
            PIIType.CARDHOLDER_NAME,
            "IVANOV IVAN",
        ),
    ],
)
def test_detects_context_types(text: str, pii_type: PIIType, value: str) -> None:
    detector = RegexDetector()
    entities = detector.detect(text, DetectionContext())
    assert any(e.type == pii_type and e.value == value for e in entities)


@pytest.mark.parametrize(
    "text",
    [
        "Александр Сергеевич Пушкин родился в 1799 году в Москве.",
        "Отделение банка №8595 работает с 9 до 18 часов.",
        "Договор от 12.03.2024 подписан обеими сторонами.",
        "ИНН организации 1234567890 указан в реквизитах.",
        "Иван и Мария гуляли по парку вчера вечером.",
        "Москва — столица России, крупный мегаполис.",
        "Улица была пуста в этот поздний час.",
        "Дом стоял на краю деревни.",
        "Квартира была просторной и светлой.",
        "Число 5678 встречается в тексте.",
    ],
)
def test_no_false_positive_on_negative_cases(text: str) -> None:
    detector = RegexDetector()
    entities = detector.detect(text, DetectionContext())
    assert entities == []