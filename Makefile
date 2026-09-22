.PHONY: install lint typecheck test run docker-build docker-up perf smoke

install:
	pip install -e ".[dev]"

lint:
	ruff check app tests scripts

typecheck:
	mypy app

test:
	pytest

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t pii-security-proxy .

docker-up:
	docker compose up --build

perf:
	locust -f tests/performance/locustfile.py --host http://localhost:8000

smoke:
	python scripts/smoke_test.py