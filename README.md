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

## 7.1. Точный контракт POST /process

Единственный обязательный endpoint — `POST /process`. Контракт фиксирован и
не меняется: на вход только `payload` и `payload_id`, на выход только `result`.

```json
// Request
{ "payload": "<string>", "payload_id": "<string>" }

// Response (200)
{ "result": "<string>" }
```

Поведение определяется состоянием `payload_id` (state machine):

| Состояние | Вход | Результат |
| --- | --- | --- |
| Новый `payload_id` | исходный текст | MASK, возвращает маску |
| MASKED | тот же исходный текст | retry MASK, та же маска (идемпотентно) |
| MASKED | маска | DEMASK, возвращает оригинал |
| DEMASKED | маска | retry DEMASK, тот же оригинал (идемпотентно) |
| MASKED/DEMASKED | несовпадающий текст | 422 `InvalidPayloadError` |

Каждый успешный roundtrip гарантирует: маска не содержит известные исходные
ПД; DEMASK полностью равен оригиналу; retry возвращает идентичный результат.

## 8. Запуск тестов

```bash
pytest
```

Concurrency/slow/performance тесты исключены из обычного прогона:

```bash
pytest -m concurrency   # concurrency-тесты
pytest -m slow          # медленные pytest-сценарии
```

## 9. Запуск performance tests

Интерактивный режим:

```bash
locust -f tests/performance/locustfile.py --host http://localhost:8000
```

Headless-прогон с управлением целевым HTTP RPS. Runner проверяет readiness
(`GET /health/ready`, который реально проверяет зависимости и может вернуть
503), сохраняет CSV/JSON/Markdown и завершает работу с ошибкой, если
фактический RPS, p95/p99 или error rate не проходят заданные пороги:

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --targets 100,330,500,1000 --scenario mixed
```

Доступные сценарии: `mask_only`, `roundtrip`, `mask_retry`, `demask_retry`,
`duplicate_payload`, `large_payload`, `mixed`. Результаты сохраняются в
`artifacts/performance/`. Числа 100/330/500/1000 являются целевым RPS;
отчёт отдельно показывает фактически достигнутое значение.

**Фактическая методика runner'а**: каждый target выполняется как **отдельная
ступень** длительностью `--duration` (по умолчанию 60 с) с линейным
`spawn`-ом пользователей только внутри этой ступени. Это **не** единый
плавный профиль со средним 330 RPS и пиками до 1000 RPS — это четыре
независимых прогона. Сценарий `mixed` взвешен так, что суммарное число MASK
равно числу DEMASK **в математическом ожидании**, а не строго в каждом прогоне
(прерванные roundtrip и 429 могут разбалансировать фактические счётчики).
Внешний LLM в hot path не используется.

Отчёт сохраняет: target RPS, actual RPS, средний latency, p50/p95/p99,
количество запросов, количество MASK и DEMASK, HTTP 2xx, HTTP 429, HTTP 422,
прочие HTTP-статусы, функциональные ошибки, transport errors, а также CPU/RAM
load generator (если установлен `psutil`; при отсутствии — `n/a`, не FAIL).
429 учитывается отдельно от ошибок корректности и допустим только при наличии
`Retry-After`. Счётчики HTTP-статусов сверяются с Locust `Request Count` строго:
при расхождении отчёт помечается как FAIL, а не выдаётся за валидный PASS.

Простой синхронный бенчмарк (без Locust):

```bash
python scripts/benchmark.py --url http://localhost:8000 --users 200 --duration 30
```

### 9.0.1. Непрерывный профиль организаторов (`--profile organizer`)

Отдельный opt-in режим, воспроизводящий требуемый исходным заданием профиль
нагрузки как **один непрерывный** Locust-запуск (без остановки/перезапуска
процесса между фазами). Профиль управляется `LoadTestShape` + динамическим
`wait_time`, который пересчитывает целевой rate по времени:

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --profile organizer --scenario roundtrip
```

Основной SLA-сценарий — `roundtrip` (строгий MASK → DEMASK с парными
счётчиками). `mixed` остаётся дополнительным stress-профилем; retry, conflict
и large payload запускаются отдельными сценариями.

Точный 480-секундный профиль:

| Фаза | Время | Target RPS |
| --- | --- | --- |
| ramp-up | 0–180s | 0 → 330 |
| steady | 180–300s | 330 |
| peak | 300–330s | 1000 |
| ramp-down | 330–360s | 1000 → 330 |
| steady | 360–480s | 330 |

Интеграл target / 480 = **330.9375 RPS** (средний target). Максимум
одновременных `FastHttpUser` — жёстко **не более 200**: CLI отклоняет
`--max-users > 200` в organizer-режиме с явной ошибкой, а в locustfile есть
defense-in-depth cap `min(..., 200)` даже если `ORGANIZER_MAX_USERS` задан
больше через env. `--profile organizer` **несовместим** с `--targets` (CLI
отклоняет оба сразу). Реальные actual RPS, latency, статусы и ошибки берутся
из отчёта, а не из формулы; в `summary.json` добавляются profile metadata
(фазы, `target_average_rps`, `actual_average_rps`, `target_vs_actual_delta`).
Строгая сверка счётчиков сохраняется.

> **Полные 480-секундные прогоны выполнены** (см. раздел 9.7 и
> `docs/qa-report.md`): несколько полных `--profile organizer` прогонов против
> локального приложения (memory single-worker и Redis multiworker). Все они
> **FAIL** по RPS-гейту (фактический средний ~284–308 RPS против target 330.94)
> и не достигают пика 1000 RPS (наблюдалось ~543–852 RPS). Профиль реализован
> и покрыт unit-тестами чистой функции `target_rps_at(t)`, cap пользователей и
> CLI-валидации. Короткий исполняемый smoke
> (`ORGANIZER_DURATION=5`, `ORGANIZER_MAX_USERS=5`, `ORGANIZER_RPS_PER_USER=1`)
> подтверждает работу shape + динамического `wait_time` + записи custom metrics
> при остановке, но **не** является результатом capacity/SLA.

## 9.1. Метрики

Технический endpoint `GET /metrics` отдаёт метрики в формате Prometheus
(`text/plain; version=0.0.4`). Он не меняет контракт `POST /process`.

```bash
curl http://localhost:8000/metrics
```

Метрики используют стандартные монотонные Prometheus Counter/Histogram,
раздельные buckets для latency в секундах и размера payload в байтах. В labels
не используются `payload`, `payload_id` или `request_id`.

## 9.2. CI

GitHub Actions workflow в `.github/workflows/ci.yml` на каждый push/PR
запускает: `pip install -e ".[dev]"`, `ruff check .`,
`mypy app scripts benchmarks`, `pytest`, `pytest -m concurrency`,
`python -m benchmarks.score`, `docker compose config`, `git diff --check` и
валидацию submission ZIP (build/validate/smoke).

После объединения изменений детектора из `main` локальный
`python -m benchmarks.score` проходит: общий F1 0.995, recall
`PASSPORT_ISSUER` 1.0. Это не подтверждает результат удалённого CI.
`docker compose config`
выполняется с безопасным тестовым `MASKING_KEY` (только для рендера compose,
не используется в рантайме). `git diff --check` сравнивает изменения с базой:
для PR — с базовой веткой, для push — с предыдущим коммитом (для новой ветки —
с пустым деревом), а не весь history.

## 9.3. Submission ZIP

```bash
python scripts/package_submission.py submission.zip
python scripts/package_submission.py --validate submission.zip
python scripts/package_submission.py --smoke submission.zip
```

Сборщик создаёт детерминированный архив и исключает `.env*` (кроме
`.env.example`), VCS, виртуальные окружения, caches, performance artifacts и
предыдущие ZIP-файлы. Smoke-валидация распаковывает архив во временную
директорию, устанавливает проект, импортирует `app.main`, выполняет MASK и
DEMASK, проверяет точное восстановление и запускает быстрый pytest subset.

## 9.4. Конфигурация

Все настройки задаются переменными окружения (см. `.env.example`).

### MASKING_KEY

Fernet-ключ для шифрования состояния восстановления (включая исходный текст)
в хранилище. Обязателен при `RESTORATION_STORE_BACKEND=redis` — сервис
завершается с ошибкой, если ключ не задан. Должен быть одинаковым во всех
воркерах.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Redis password

**Текущий `docker-compose.yml` включает аутентификацию Redis**: `redis-server`
запускается с `--requirepass "$REDIS_PASSWORD"`, а `REDIS_URL` содержит пароль
(`redis://:${REDIS_PASSWORD}@redis:6379/0`). Пароль обязателен и никогда не
логируется. Для внешнего защищённого Redis укажите URL с учётными данными:

```bash
REDIS_URL=redis://:password@redis-host:6379/0
```

## 9.5. Health / readiness

- `GET /health` — **liveness**: всегда возвращает `{"status": "ok"}`, не
  проверяет зависимости.
- `GET /health/ready` — **readiness**: проверяет доступность зависимостей
  (policy provider, restoration store) и может вернуть `503` со
  `{"status": "not_ready"}`, если они недоступны.
- `GET /metrics` отдаёт метрики Prometheus (см. 9.1).

## 9.6. Предварительный диагностический прогон (НЕ валидный PASS)

> **Предупреждение.** Ниже — только предварительный диагностический прогон.
> Он **не** является валидным PASS и **не** даёт оценку capacity/SLA по двум
> причинам: (1) каждая ступень длилась всего **15 секунд** (недостаточно для
> устойчивых percentiles), и (2) счётчики **рассогласованы** — в `rps-500`
> Locust CSV `Request Count=7055`, а custom `http_2xx=7243`; аналогично на
> других ступенях. Поэтому никакие стабильные ~500 RPS, достигнутые SLA или
> точные производительные выводы здесь **не заявляются**.

Измерено на реальном прогоне (1 worker, in-memory store, сценарий `mixed`).
Отчёт сохранён в `artifacts/performance/20260923T000000Z/`:

| Target RPS | Actual RPS | avg | p50 | p95 | p99 | MASK | DEMASK | 2xx | 429 | Результат |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| 100 | 101.1 | 20.3 ms | 18 ms | 39 ms | 51 ms | 725 | 711 | 1436 | 0 | диагностический |
| 330 | 331.1 | 50.7 ms | 48 ms | 83 ms | 95 ms | 2399 | 2377 | 4776 | 0 | диагностический |
| 500 | 501.2 | 52.1 ms | 52 ms | 88 ms | 110 ms | 3619 | 3624 | 7243 | 0 | диагностический |
| 1000 | 498.5 | 197.9 ms | 200 ms | 230 ms | 250 ms | 3662 | 3601 | 7263 | 0 | диагностический |

Эти числа — **сырые диагностические наблюдения**, не подтверждённые
согласованными счётчиками и достаточной длительностью. Сохранённые локальные
артефакты игнорируются git (`.gitignore`) и **не попадут в PR/ZIP** — не
рассчитывайте на доступный внешнему читателю отчёт.

Для полноценного повтора (согласованные счётчики, устойчивые percentiles)
запустите с достаточной длительностью и проверьте сверку счётчиков:

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --targets 100,330,500,1000 --scenario mixed --duration 120
```

## 9.7. Ранее заявленный Redis-прогон (основной production-профиль)

> **Важно про статус этого прогона.** Это **ранее заявленный/описанный**
> Redis-прогон с измеренным RPS/latency. Его артефакты сохранены по **старой
> схеме** и **не содержат** metadata новой схемы (`environment`,
> `server_configuration`, `docker_metrics`, `stage_timings`), поэтому он **не
> является доказательством** backend/workers/конфигурации сервера. Новая схема
> отчётности пока **не проверена полным 480-секундным прогоном** (Docker здесь
> недоступен). Числа ниже — это заявленные ранее измерения, а не подтверждённые
> новой схемой.

Основной production-профиль — **Redis** (несколько workers). Memory-бэкенд
остаётся для диагностики и **не** является основным benchmark.

Ранее заявленный 480-секундный прогон (4 workers, Redis, `roundtrip`):

| Метрика | Значение |
| --- | --- |
| target average RPS | 330.94 |
| actual average RPS | 307.62 |
| average latency | 41.5 ms |
| p50 / p95 / p99 | 17 / 130 / 160 ms |
| MASK / DEMASK / incomplete | 73867 / 73851 / 16 |
| 2xx / 429 / 422 / errors | 147718 / 0 / 0 / 0 |
| configured max users | 200 |
| observed max users | ~100 (см. ниже) |
| observed peak RPS | ~852 |

**Важно про concurrency.** `configured_max_users` — это настроенный предел,
а не фактически достигнутая concurrency. `observed_max_users` вычисляется из
истории Locust (`stats_stats_history.csv`) и отражает реально активных
пользователей. **Активные Locust-пользователи — это НЕ одновременные HTTP
соединения**: каждый `FastHttpUser` поддерживает пул соединений и может
выполнять запросы последовательно, поэтому число пользователей не равно числу
параллельных соединений к серверу. Для гарантии 200 активных пользователей
используйте `--required-concurrent-users 200`; без него генератор может создать
меньше пользователей, чем предел.

**Важно про peak RPS.** `observed_peak_rps` — фактический пик из истории.
Пик 1000 RPS **не подтверждён** фактическими измерениями (наблюдалось ~852
RPS). Не заявляйте 1000 RPS без реального прогона.

**Метрики ресурсов.** Отчёт сохраняет средний и **пиковый** CPU/RAM load
generator, а также пиковый CPU/RAM контейнеров app/Redis, сэмплируемые **в ходе**
прогона через `docker stats`. При отсутствии Docker эти значения честно
показываются как `unavailable`, а не выдумываются. Конфигурация сервера
(backend, число workers, команда запуска, логирование) в `summary.json` всегда
помечается как `operator_declared_unverified`: даже если оператор передал её
через `--server-config`, runner её не проверяет, поэтому источник никогда не
становится «verified». Runner не выдаёт собственное окружение за конфигурацию
удалённого сервера.

Артефакты: `artifacts/performance/final-organizer-redis/` (`summary.json`,
`summary.md`, `final_stats.csv`, `custom_metrics.json`,
`stats_stats_history.csv`). Performance-артефакты не попадают в ZIP.

**Не проверено** в этом прогоне: большие payload. Redis store и multiworker
проверены этим прогоном (см. таблицу выше). Точные команды/окружение для
повтора:

```bash
# Redis store (требует MASKING_KEY и запущенный Redis)
RESTORATION_STORE_BACKEND=redis REDIS_URL=redis://localhost:6379/0 \
  MASKING_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  uvicorn app.main:app --host 0.0.0.0 --port 8000

# Multiworker (требует Redis store для согласованного состояния).
# Все воркеры должны использовать ОДИН общий Fernet-ключ и один Redis URL,
# иначе состояние не будет согласовано между процессами. Ключ генерируется
# один раз и передаётся в переменную окружения; реальный секрет не встраивается
# в команду и не печатается.
export MASKING_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
RESTORATION_STORE_BACKEND=redis REDIS_URL=redis://localhost:6379/0 \
  MASKING_KEY="$MASKING_KEY" \
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# Large payload сценарий
LOAD_SCENARIO=large_payload python scripts/run_performance.py \
  --host http://localhost:8000 --targets 100 --scenario large_payload
```

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

- Детекция построена на regex + валидаторах + маркерах (recall-first). Точность
  по типам измеряется локальным бенчмарком (`python -m benchmarks.score`), а не
  заявляется без измерений.
- In-memory хранилище состояния (один процесс) — для нескольких воркеров
  используйте Redis (`RESTORATION_STORE_BACKEND=redis`).
- Нет реального NER и внешнего LLM в hot path (LLM-детектор опционален и
  выключен по умолчанию).
- Rate limiting реализован (глобальный, возвращает 429 с `Retry-After`).
- Аутентификация/авторизация не реализованы.
- Ранее обнаруженный нулевой recall `PASSPORT_ISSUER` исправлен в `main`:
  на текущем локальном датасете recall 1.0 (8/8). Пороговые проверки качества
  остаются обязательными; это не оценка на скрытых данных организаторов.

## Настройка consumer (кратко)

Создайте YAML-файл в `configs/consumers/` с именем, равным `consumer_id`.
Укажите `enabled`, список `enabled_types`, стратегии `masking` для каждого
типа и флаг `demasking.enabled`. Перезапустите сервис, чтобы политика
загрузилась. Примеры: `default.yaml` и `demo.yaml`.

## Выбор consumer

По умолчанию используется `default` consumer. Для выбора другого consumer
передайте необязательный заголовок `X-Consumer-ID`:

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -H "X-Consumer-ID: demo" \
  -d '{"payload": "test@example.com", "payload_id": "example-1"}'
```

Неизвестный или отключённый consumer отклоняется с HTTP 403. Заголовок не
меняет обязательный JSON-контракт `POST /process`.

## Поддерживаемые типы ПД

EMAIL, PHONE, BANK_CARD, INN, PASSPORT_NUMBER, PASSPORT_DIVISION_CODE,
BIRTH_DATE, CVV, PIN, PERSON_NAME, BIRTH_PLACE, CITIZENSHIP, PASSPORT_ISSUER,
PASSPORT_ISSUE_DATE, DRIVER_LICENSE, ADDRESS, COUNTRY, POSTAL_CODE, CITY,
STREET, HOUSE, APARTMENT, CARDHOLDER_NAME.

## Качество детекции

```bash
python -m benchmarks.score
```

Отчёт показывает span-level precision/recall/F1 по каждому типу и в целом, а
также false positives на негативных кейсах. Бенчмарк падает, если recall
обязательного типа с эталонными кейсами ниже 0.5, общий recall ниже 0.8
или общий F1 ниже 0.8. Нулевой recall обязательного типа не может скрыться
за общим результатом. Качество на скрытых данных этим не подтверждается.
