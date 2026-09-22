"""Regex-based detector for PII entities.

A recall-first hybrid detector for Russian text. Each rule pairs a compiled
pattern with an optional marker list and an optional validator. A marker is a
substring that must appear in a window around the match; a validator is a
boolean function applied to the match.

Design notes:
- Recall-first: when in doubt between over-masking and leaking PII, we mask.
- Types that are unambiguous (EMAIL, PHONE, BANK_CARD, INN, PASSPORT_NUMBER,
  PASSPORT_DIVISION_CODE, CVV, PIN) are detected directly.
- Types that need context (PERSON_NAME, BIRTH_PLACE, CITIZENSHIP, ADDRESS,
  PASSPORT_ISSUER, PASSPORT_ISSUE_DATE, DRIVER_LICENSE, COUNTRY, POSTAL_CODE,
  CITY, STREET, HOUSE, APARTMENT, CARDHOLDER_NAME) are matched with
  marker-anchored patterns so the value is captured right after a known
  marker, avoiding obvious false positives (e.g. "Александр Сергеевич Пушкин"
  in a historical context, a bank branch address, a contract date, an order
  number).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.core.enums import PIIType
from app.core.models import DetectionContext, PIIEntity
from app.detection.base import PIIDetector
from app.detection.validators import inn_checksum, is_birth_date, luhn

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-]?(?:\(\d{3}\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_BANK_CARD_RE = re.compile(r"(?<!\d)(?:\d[\s\-]?){12,18}\d(?!\d)")
_INN_RE = re.compile(r"(?<!\d)\d{10}(?!\d)|(?<!\d)\d{12}(?!\d)")
_PASSPORT_NUMBER_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
_PASSPORT_DIVISION_CODE_RE = re.compile(r"(?<!\d)\d{3}-\d{3}(?!\d)")
_BIRTH_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})(?!\d)"
)
_CVV_RE = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
_PIN_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")

# Marker-anchored patterns: capture the value right after a known marker.
_PERSON_NAME_RE = re.compile(
    r"(?i:клиент|гражданин|гражданка|заявитель|пациент|сотрудник|менеджер|"
    r"директор|держатель|владелец|представитель|покупатель|абонент|"
    r"пользователь|получатель|отправитель)\s+"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})"
)
_BIRTH_PLACE_RE = re.compile(
    r"(?i:место рождения|родился в|родилась в|уроженец|уроженка|родом из)\s*:?\s*[—–-]?\s*"
    r"(?:г\.\s*|городе\s+|города\s+|город\s+|село\s+|села\s+|с\.\s*|"
    r"деревня\s+|деревни\s+|д\.\s*)?"
    r"([А-ЯЁ][а-яё]+(?:-[а-яё]+-[А-ЯЁ][а-яё]+|-[А-ЯЁ][а-яё]+)*"
    r"(?:\s+[А-ЯЁ][а-яё]+(?:-[а-яё]+-[А-ЯЁ][а-яё]+|-[А-ЯЁ][а-яё]+)*){0,1}"
    r"(?:\s+[А-ЯЁ][а-яё]+(?:ской|ского|ский|ого|ая|ое)\s+(?:области|область|обл\.|района|район|края|край))?)"
)
_CITIZENSHIP_RE = re.compile(
    r"(?i:гражданство|гражданин|гражданка)\s*:?\s*"
    r"([А-ЯЁ][А-ЯЁа-яё]*(?:\s+[А-ЯЁ][А-ЯЁа-яё]*){0,2})"
)
_PASSPORT_ISSUER_RE = re.compile(
    r"(?i:кем выдан|выдан\b|выдано\b|орган, выдавший)\s*:?\s*"
    r"([А-ЯЁа-яё0-9№-]+\.?(?:\s+[А-ЯЁа-яё0-9№-]+\.?)*)"
)
_PASSPORT_ISSUE_DATE_RE = re.compile(
    r"(?i:дата выдачи|выдан|выдано)(?:[^0-9]{0,80}?)"
    r"(\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2})"
)
_DRIVER_LICENSE_RE = re.compile(
    r"(?i:водительское удостоверение|водительские права|права|в\.у\.|ву)\s*:?\s*"
    r"(\d{2}\s?[А-ЯЁ]{2}\s?\d{6}|\d{2}\s?\d{2}\s?\d{6}|\d{10})"
)
_ADDRESS_RE = re.compile(
    r"(?i:адрес регистрации|адрес проживания|адрес|место жительства|"
    r"место регистрации|проживает|живет|живёт|"
    r"зарегистрирован|зарегистрирована|прописан|прописана)"
    r"(?:\s+по\s+адресу)?\s*:?\s*"
    r"((?:[А-ЯЁа-яё0-9-]+\.?\s*[,.]?\s*){2,10})"
)
_COUNTRY_RE = re.compile(
    r"(?i:страна)\s*:?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)"
)
_POSTAL_CODE_RE = re.compile(
    r"(?i:индекс|почтовый индекс)\s*:?\s*(\d{6})"
)
_CITY_RE = re.compile(
    r"(?i:город|г\.)\s*:?\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)"
)
_STREET_RE = re.compile(
    r"(?i:улица|ул\.|проспект|пр-т|переулок|пер\.)\s*:?\s*"
    r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)"
)
_HOUSE_RE = re.compile(r"(?i:дом|д\.)\s*:?\s*(\d{1,4}(?:[А-ЯЁа-яё])?)")
_APARTMENT_RE = re.compile(r"(?i:квартира|кв\.)\s*:?\s*(\d{1,4})")
_CARDHOLDER_NAME_RE = re.compile(
    r"(?i:держатель карты|cardholder|имя на карте|владелец карты)\s*:?\s*"
    r"([A-ZА-ЯЁ][A-Za-zА-ЯЁа-яё]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-ЯЁа-яё]+)?)"
)

_VALIDATORS: dict[PIIType, Callable[[str], bool]] = {
    PIIType.BANK_CARD: luhn,
    PIIType.INN: inn_checksum,
    PIIType.BIRTH_DATE: is_birth_date,
}

# Rules that capture a group (the actual PII value) vs. the whole match.
# Marker-anchored patterns use inline (?i:...) so the marker is
# case-insensitive while the captured value stays case-sensitive.
_RULES: dict[PIIType, re.Pattern[str]] = {
    PIIType.EMAIL: _EMAIL_RE,
    PIIType.PHONE: _PHONE_RE,
    PIIType.BANK_CARD: _BANK_CARD_RE,
    PIIType.INN: _INN_RE,
    PIIType.PASSPORT_NUMBER: _PASSPORT_NUMBER_RE,
    PIIType.PASSPORT_DIVISION_CODE: _PASSPORT_DIVISION_CODE_RE,
    PIIType.BIRTH_DATE: _BIRTH_DATE_RE,
    PIIType.CVV: _CVV_RE,
    PIIType.PIN: _PIN_RE,
    PIIType.PERSON_NAME: _PERSON_NAME_RE,
    PIIType.BIRTH_PLACE: _BIRTH_PLACE_RE,
    PIIType.CITIZENSHIP: _CITIZENSHIP_RE,
    PIIType.PASSPORT_ISSUER: _PASSPORT_ISSUER_RE,
    PIIType.PASSPORT_ISSUE_DATE: _PASSPORT_ISSUE_DATE_RE,
    PIIType.DRIVER_LICENSE: _DRIVER_LICENSE_RE,
    PIIType.ADDRESS: _ADDRESS_RE,
    PIIType.COUNTRY: _COUNTRY_RE,
    PIIType.POSTAL_CODE: _POSTAL_CODE_RE,
    PIIType.CITY: _CITY_RE,
    PIIType.STREET: _STREET_RE,
    PIIType.HOUSE: _HOUSE_RE,
    PIIType.APARTMENT: _APARTMENT_RE,
    PIIType.CARDHOLDER_NAME: _CARDHOLDER_NAME_RE,
}

# Types whose rule captures the value in group(1) rather than the whole match.
_GROUPED_TYPES = frozenset(
    {
        PIIType.PERSON_NAME,
        PIIType.BIRTH_PLACE,
        PIIType.CITIZENSHIP,
        PIIType.PASSPORT_ISSUER,
        PIIType.PASSPORT_ISSUE_DATE,
        PIIType.DRIVER_LICENSE,
        PIIType.ADDRESS,
        PIIType.COUNTRY,
        PIIType.POSTAL_CODE,
        PIIType.CITY,
        PIIType.STREET,
        PIIType.HOUSE,
        PIIType.APARTMENT,
        PIIType.CARDHOLDER_NAME,
    }
)

# Non-grouped types that still require a marker in a window around the match
# to avoid matching digits inside other numbers (e.g. CVV/PIN inside a phone)
# or dates/numbers in non-PII contexts (e.g. a contract date, an org INN).
_MARKER_WINDOW = 40
_MARKER_TYPES: dict[PIIType, tuple[str, ...]] = {
    PIIType.CVV: ("cvv", "cvc", "код проверки"),
    PIIType.PIN: ("pin", "пин"),
    PIIType.BIRTH_DATE: (
        "г.р.",
        "дата рожд",
        "дата рождения",
        "род.",
        "родился",
        "родилась",
    ),
    PIIType.PASSPORT_NUMBER: ("паспорт", "серия", "выдан"),
}

# Country/citizenship names that must not be treated as PERSON_NAME.
_COUNTRY_NAMES = frozenset(
    {
        "российская федерация",
        "российской федерации",
        "рф",
        "россия",
        "россии",
        "республика беларусь",
        "республики беларусь",
        "беларусь",
        "беларуси",
        "казахстан",
        "казахстана",
        "республика казахстан",
        "республики казахстан",
        "украина",
        "украины",
        "узбекистан",
        "узбекистана",
        "армения",
        "армении",
        "грузия",
        "грузии",
        "таджикистан",
        "таджикистана",
        "киргизия",
        "киргизии",
        "молдова",
        "молдовы",
        "туркменистан",
        "туркменистана",
        "азербайджан",
        "азербайджана",
        "литва",
        "литвы",
        "латвия",
        "латвии",
        "эстония",
        "эстонии",
    }
)

# Address component types that should be suppressed in a bank-branch context.
_ADDRESS_COMPONENT_TYPES = frozenset(
    {
        PIIType.ADDRESS,
        PIIType.CITY,
        PIIType.STREET,
        PIIType.HOUSE,
        PIIType.APARTMENT,
        PIIType.POSTAL_CODE,
        PIIType.PHONE,
    }
)


class RegexDetector(PIIDetector):
    """Detects PII candidates using compiled regular expressions."""

    name = "regex"

    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        entities: list[PIIEntity] = []
        for pii_type, pattern in _RULES.items():
            for match in pattern.finditer(text):
                if pii_type in _GROUPED_TYPES:
                    value = match.group(1)
                    start = match.start(1)
                    end = match.end(1)
                    # Strip trailing punctuation so the span covers only the value.
                    while end > start and value[-1] in ".,;!? ":
                        end -= 1
                        value = value[:-1]
                    if pii_type == PIIType.PASSPORT_ISSUER:
                        value, start, end = self._clean_issuer(
                            text, value, start, end
                        )
                        if not value:
                            continue
                else:
                    value = match.group(0)
                    start = match.start()
                    end = match.end()
                validator = _VALIDATORS.get(pii_type)
                if validator is not None and not validator(value):
                    continue
                if pii_type == PIIType.PERSON_NAME and value.lower() in _COUNTRY_NAMES:
                    continue
                if pii_type == PIIType.CITIZENSHIP and value.lower() not in _COUNTRY_NAMES:
                    continue
                if pii_type == PIIType.ADDRESS and not self._is_full_address(value):
                    continue
                if pii_type in _ADDRESS_COMPONENT_TYPES and self._is_bank_context(
                    text, start
                ):
                    continue
                markers = _MARKER_TYPES.get(pii_type)
                if markers is not None and not self._has_marker(
                    text, start, end, markers
                ):
                    continue
                entities.append(
                    PIIEntity(
                        type=pii_type,
                        value=value,
                        start=start,
                        end=end,
                        confidence=1.0,
                        detector=self.name,
                    )
                )
        return entities

    @staticmethod
    def _has_marker(
        text: str, start: int, end: int, markers: tuple[str, ...]
    ) -> bool:
        window = text[max(0, start - _MARKER_WINDOW) : end + _MARKER_WINDOW].lower()
        return any(marker in window for marker in markers)

    @staticmethod
    def _is_bank_context(text: str, start: int) -> bool:
        """Return True if the text near ``start`` mentions a bank branch.

        A public bank-branch phone/address is not personal data, so it is
        suppressed. But a clearly personal phone (e.g. "его личный телефон")
        must not be suppressed even if a bank is mentioned nearby.
        """
        window = text[max(0, start - 80) : start + 20].lower()
        if "банк" not in window and "отделение" not in window:
            return False
        # Personal indicators mean the value belongs to the client, not the bank.
        personal = ("личный", "личного", "личному", "мой", "моя", "моего",
                    "его", "её", "ее", "клиента", "клиент", "заявителя",
                    "пациента", "сотрудника")
        return not any(indicator in window for indicator in personal)

    @staticmethod
    def _is_full_address(value: str) -> bool:
        """Return True if ``value`` looks like a full address, not a fragment.

        A full address contains at least one address component (postal code,
        street, house, apartment) or is at least three tokens long.
        """
        import re as _re

        if _re.search(r"\d{6}|ул\.|улица|д\.|дом|кв\.|квартира|г\.|город", value):
            return True
        return len(value.split()) >= 3

    @staticmethod
    def _clean_issuer(
        text: str, value: str, start: int, end: int
    ) -> tuple[str, int, int]:
        """Trim a PASSPORT_ISSUER value to the issuing authority.

        The greedy pattern may capture a trailing date or a pure date
        fragment; truncate at a date and drop purely numeric values.
        """
        import re as _re

        # Look for a date in the original text right after the captured span.
        tail = text[end : end + 12]
        date_match = _re.match(r"\s*\d{2}[./]\d{2}[./]\d{4}|\s*\d{4}-\d{2}-\d{2}", tail)
        if date_match:
            value = value.rstrip()
            end = start + len(value)
        # Also truncate at a date fragment inside the value (e.g. "12" in
        # "ОВД района Арбат 12").
        frag = _re.search(r"\s+\d{1,2}$", value)
        if frag:
            value = value[: frag.start()].rstrip()
            end = start + len(value)
        if not value or value.isdigit():
            return "", start, start
        # Drop values that are just a year phrase (e.g. "в 2015 году"), which
        # are not an issuing authority.
        if _re.fullmatch(r"в\s+\d{4}\s+году", value):
            return "", start, start
        return value, start, end