# Masking / Demasking / Security

Модуль маскирования и восстановления персональных данных (ПД). Отвечает за
формат маски по типу ПД, контекстные правила и безопасное хранение состояния
для восстановления.

## Как это устроено

- `app/masking/strategies/impl.py` — стратегии маскирования. Формат зависит от
  типа ПД (см. ниже).
- `app/masking/engine.py` — `MaskingEngine.mask(text, entities, strategy_by_type)`
  применяет стратегии к spans, отсеивает невалидные/пересекающиеся, применяет
  контекстное правило для PIN.
- `app/restoration/memory.py` — `InMemoryRestorationStore` хранит состояние
  (оригинал + маппинги) зашифрованным через Fernet, с TTL.
- `app/processing/process_service.py` — `ProcessService.process(...)` решает,
  маскировать или восстанавливать, по наличию состояния для `payload_id`.

## Интеграция для участника 1 (API)

`POST /process` уже вызывает `ProcessService.process(payload, payload_id, policy)`.
Логика mask/demask определяется автоматически:

```python
# app/api/routes/process.py (уже так)
outcome = process_service.process(request.payload, request.payload_id, policy)
return ProcessResponse(result=outcome.result)
```

- Если для `payload_id` ещё нет состояния — выполняется MASK.
- Если состояние есть и пришёл замаскированный текст — выполняется DEMASK
  (возвращается сохранённый `original_text`).
- Повторный MASK с тем же текстом — идемпотентен (возвращается тот же результат).
- Повторный DEMASK — идемпотентен.

Исключения (из `app/core/exceptions.py`), которые может бросить сервис:
- `InvalidPayloadError` (422) — пустой payload/payload_id или текст не совпадает
  с сохранённым состоянием.
- `RestorationStateNotFoundError` (404) — нет состояния для payload_id.
- `ProcessingError` (500) — внутренняя ошибка.

## Формат Span для участника 2 (детектор)

Детектор возвращает список `PIIEntity` (см. `app/core/models.py`):

```python
@dataclass(slots=True)
class PIIEntity:
    type: PIIType      # enum из app/core/enums.py
    value: str         # найденный фрагмент
    start: int         # индекс начала (включительно)
    end: int           # индекс конца (исключительно)
    confidence: float = 1.0
    detector: str = "unknown"
    metadata: dict = field(default_factory=dict)
```

`start`/`end` — как срез `text[start:end]`. `MaskingEngine` сам отсеивает
невалидные spans (start<0, end>len, start>=end) и при пересечении оставляет
более длинный.

## Формат маски по типу ПД

| Тип | Формат | Пример |
| --- | --- | --- |
| PERSON_NAME, CARDHOLDER_NAME | инициалы | `Иванов Иван Иванович` → `И. И. И.` |
| PASSPORT_NUMBER, BANK_CARD, INN, DRIVER_LICENSE, PHONE | первые 2 и последние 2 цифры, остальные `*`, разделители на месте | `4509 123456` → `45** ****56`; `4567 8901 2345 6756` → `45** **** **** **56` |
| PASSPORT_DIVISION_CODE | цифры → `*`, дефис на месте | `123-456` → `***-***` |
| EMAIL | первый символ логина + `***` + `@домен` | `test@example.com` → `t***@example.com` |
| BIRTH_DATE, CVV, PIN, ADDRESS, BIRTH_PLACE, CITIZENSHIP, PASSPORT_ISSUER, PASSPORT_ISSUE_DATE и др. | все буквы/цифры → `*`, разделители на месте | `12.03.1990` → `**.**.****` |

Режим (FULL_MASK / PARTIAL_MASK) выбирается политикой consumer
(`configs/consumers/*.yaml`), формат внутри режима — типом ПД. По умолчанию
применяется `PARTIAL_MASK` (формат из ТЗ).

## Контекстное правило

PIN маскируется только если в том же тексте есть BANK_CARD. Правило задано
таблицей `CONTEXT_REQUIRES` в `app/masking/engine.py`:

```python
CONTEXT_REQUIRES = {PIIType.PIN: frozenset({PIIType.BANK_CARD})}
```

Чтобы добавить правило — добавьте запись в эту таблицу.

## Переменные окружения

- `MASKING_KEY` — Fernet-ключ для шифрования состояния в store. Если не задан,
  генерируется случайный при старте (состояние не восстановится после
  перезапуска). Сгенерировать:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `RESTORATION_TTL_SECONDS` — TTL записи в store (по умолчанию 3600).

## Ограничение

`InMemoryRestorationStore` хранит состояние в памяти одного процесса и не
работает при нескольких воркерах uvicorn. Для горизонтального масштабирования
нужен Redis-бэкенд за интерфейсом `RestorationStore` (см. `app/restoration/base.py`).