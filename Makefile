.PHONY: help install dev api console test lint fixtures screenshots build clean

help:
	@echo "install      npm + pip dependencies"
	@echo "api          run the API on :8000"
	@echo "console      run the console on :5173"
	@echo "test         both test suites"
	@echo "fixtures     regenerate the TS->Python agreement fixtures"
	@echo "screenshots  recapture the README images (needs both servers running)"

install:
	npm install
	pip install -e "services/api[dev]"

api:
	uvicorn rainmaker.app:app --app-dir services/api/src --port 8000 --reload

console:
	npm run dev -w @rainmaker/console

test:
	npx vitest run --root packages/crdt
	pytest services/api -q

lint:
	npx tsc --noEmit -p packages/crdt/tsconfig.json
	npx tsc --noEmit -p apps/console/tsconfig.json
	ruff check services/api

fixtures:
	npx tsx packages/crdt/scripts/fixtures.ts

build:
	npm run build -w @rainmaker/console

screenshots:
	node scripts/screenshots.mjs

clean:
	rm -rf data apps/console/dist node_modules/.vite .pytest_cache .ruff_cache
