"""LLM-based detector for PII entities."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Protocol

from app.core.enums import PIIType
from app.core.models import DetectionContext, PIIEntity
from app.detection.base import PIIDetector

_SYSTEM_PROMPT = """
Найди в тексте персональные данные и верни JSON.

Типы: PERSON_NAME, BIRTH_DATE, BIRTH_PLACE, CITIZENSHIP, PASSPORT_NUMBER,
PASSPORT_ISSUER, PASSPORT_DIVISION_CODE, PASSPORT_ISSUE_DATE, DRIVER_LICENSE,
ADDRESS, EMAIL, PHONE, INN, BANK_CARD, CVV, PIN, CARDHOLDER_NAME.

Правила:
- "text" — точная подстрока из текста, символ в символ.
- Адрес возвращай одним фрагментом целиком.
- Одно значение — одна запись, даже если встречается несколько раз.
- Находи всё, что похоже на ПД. Сомневаешься — возвращай.
- Только JSON, без markdown и пояснений.

Формат: {"entities":[{"type":"EMAIL","text":"a@b.ru"}]}
"""

_FEW_SHOT_USER = (
    "Клиент Иванов Иван Иванович, 12.03.1985 г.р., паспорт 4510 123456, "
    "г. Москва, ул. Ленина, д. 5, кв. 12, тел. +7 999 123-45-67, ivanov@mail.ru"
)

_FEW_SHOT_ASSISTANT = (
    '{"entities":['
    '{"type":"PERSON_NAME","text":"Иванов Иван Иванович"},'
    '{"type":"BIRTH_DATE","text":"12.03.1985"},'
    '{"type":"PASSPORT_NUMBER","text":"4510 123456"},'
    '{"type":"ADDRESS","text":"г. Москва, ул. Ленина, д. 5, кв. 12"},'
    '{"type":"PHONE","text":"+7 999 123-45-67"},'
    '{"type":"EMAIL","text":"ivanov@mail.ru"}'
    "]}"
)

_MESSAGES: list[dict[str, str]] = [
    {"role": "user", "content": _FEW_SHOT_USER},
    {"role": "assistant", "content": _FEW_SHOT_ASSISTANT},
]


class LLMClient(Protocol):
    """Minimal client interface for the LLM detector."""

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the model completion for the given messages."""
        ...


@lru_cache(maxsize=1024)
def _complete_cached(text: str) -> str:
    return _CLIENT.complete(
        _SYSTEM_PROMPT,
        [*_MESSAGES, {"role": "user", "content": text}],
    )


_CLIENT: LLMClient


class LLMDetector(PIIDetector):
    """Detects PII entities using an LLM client."""

    name = "llm"

    def __init__(self, client: LLMClient, enabled: bool = True) -> None:
        global _CLIENT
        _CLIENT = client
        self._enabled = enabled

    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        if not self._enabled:
            return []
        try:
            return self._parse_response(text, _complete_cached(text))
        except Exception:
            return []

    def _parse_response(self, text: str, response: str) -> list[PIIEntity]:
        payload = json.loads(response)
        entities: list[PIIEntity] = []
        for item in payload["entities"]:
            try:
                pii_type = PIIType(item["type"])
            except ValueError:
                continue
            needle = item["text"]
            start = text.find(needle)
            if start == -1:
                continue
            entities.append(
                PIIEntity(
                    type=pii_type,
                    value=needle,
                    start=start,
                    end=start + len(needle),
                    confidence=0.8,
                    detector=self.name,
                )
            )
        return entities