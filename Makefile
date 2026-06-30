.PHONY: install test lint pyz publish-wiki check-docs clean

install:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check src tests

pyz:
	scripts/build_pyz.sh

publish-wiki:
	scripts/publish_wiki.sh

check-docs:
	uv run python scripts/check_docs_sync.py

clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
