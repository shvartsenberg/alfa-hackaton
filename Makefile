.PHONY: install lint typecheck test test-concurrency run docker-build docker-up perf perf-report smoke package package-validate

install:
	pip install -e ".[dev]"

lint:
	ruff check app tests scripts

typecheck:
	mypy app

test:
	pytest -m "not concurrency and not slow and not performance"

test-concurrency:
	pytest -m concurrency

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t pii-security-proxy .

docker-up:
	docker compose up --build

perf:
	locust -f tests/performance/locustfile.py --host http://localhost:8000

perf-report:
	python scripts/run_performance.py --host http://localhost:8000 --scenario mixed

smoke:
	python scripts/smoke_test.py

package:
	python scripts/package_submission.py

package-validate:
	python scripts/package_submission.py --validate submission.zip
