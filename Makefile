.PHONY: install test eval serve
install: ; pip install -e ".[dev]"
test: ; python -m pytest -q
eval: ; python run_eval.py
serve: ; uvicorn agentproof.service:app --port 8000
