# PII Security Proxy

Модуль безопасности персональных данных между системой-потребителем и LLM.
Сервис идентифицирует ПД, маскирует их перед отправкой в LLM и восстанавливает
исходный текст после.

## 1. Что решает проект

Защищает персональные данные при передаче текста в LLM: маскирует ПД,
сохраняет состояние для восстановления и возвращает исходный текст после
обработки. Поддерживает разные правила для разных систем-потребителей.

## 2. Архитектурная схема

```mermaid
flowchart LR
    Consumer --> Proxy["PII Proxy"]
    Proxy --> Mask
    Mask --> LLM
    LLM --> Demask
    Demask --> Consumer
```

Подробнее: [docs/architecture.md](docs/architecture.md).

## 3. Quick Start

```bash
pip install -e ".[dev]"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 4. Запуск

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 5. Docker запуск

```bash
docker compose up --build
```

## 6. curl для MASK

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"payload": "Напишите мне на test@example.com или +7 999 123-45-67", "payload_id": "example-1"}'
```

## 7. curl для DEMASK

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"payload": "<masked text>", "payload_id": "example-1"}'
```

## 8. Запуск тестов

```bash
pytest
```

## 9. Запуск performance tests

```bash
locust -f tests/performance/locustfile.py --host http://localhost:8000 \
  --users 200 --spawn-rate 20 --run-time 5m
```

Профиль: 200 соединений, каждое ждёт ответ перед следующим запросом.
`--spawn-rate 20` даёт плавный разгон. p50/p95/p99 и error rate — в отчёте
Locust. Для 100/500/1000/2000 RPS меняйте `--users`/`--spawn-rate`.

## 9a. Мульти-воркер (общий store)

Для нескольких воркеров нужен общий Redis store:

```bash
RESTORATION_STORE_BACKEND=redis REDIS_URL=redis://localhost:6379/0 \
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

`docker compose up --build` уже поднимает Redis и включает redis-бэкенд.

## 9b. Бенчмарк latency

```bash
python scripts/benchmark.py --url http://localhost:8000 --users 200 --duration 30
```

Измеряет p50/p95/p99 и достигнутый RPS. Внимание: клиент бенчмарка работает
в одном процессе (GIL), поэтому при большом числе пользователей latency
завышается из-за клиентской стороны. Для честной оценки серверной latency
используйте последовательный прогон или распределённый клиент (Locust).

## 10. Как добавить новый PIIType

Добавьте значение в `PIIType` в `app/core/enums.py`. Затем укажите его в
`enabled_types` и `masking` в конфигурации consumer.

## 11. Как добавить новый Detector

Создайте класс, реализующий `PIIDetector` в `app/detection/base.py`, и
зарегистрируйте его в `DetectionEngine` в `app/api/dependencies.py`.

## 12. Как добавить MaskingStrategy

Создайте класс, реализующий `MaskingStrategy` в `app/masking/base.py`, и
зарегистрируйте его в `DefaultMaskingStrategyFactory`.

## 13. Как добавить consumer policy

Создайте YAML-файл в `configs/consumers/`. Имя файла становится
`consumer_id`.

## 14. Security considerations

См. [docs/security.md](docs/security.md). Чувствительные данные никогда не
логируются и не попадают в метрики.

## 15. Ограничения initial implementation

- Обнаруживаются только EMAIL и PHONE (regex).
- In-memory хранилище состояния (один процесс).
- Нет реального NER, LLM, аутентификации и rate limiting.

## Настройка consumer (кратко)

Создайте YAML-файл в `configs/consumers/` с именем, равным `consumer_id`.
Укажите `enabled`, список `enabled_types`, стратегии `masking` для каждого
типа и флаг `demasking.enabled`. Перезапустите сервис, чтобы политика
загрузилась. Примеры: `default.yaml` и `demo.yaml`.