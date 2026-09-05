.PHONY: help backend frontend dev install install-backend install-frontend

help:
	@echo "Targets:"
	@echo "  make backend          Start FastAPI backend on http://localhost:8765"
	@echo "  make frontend         Start Vite frontend on http://localhost:5173"
	@echo "  make dev              Start backend and frontend in parallel (Ctrl-C kills both)"
	@echo "  make install          Install backend (poetry) and frontend (npm) dependencies"
	@echo "  make install-backend  Install only backend dependencies"
	@echo "  make install-frontend Install only frontend dependencies"

backend:
	poetry run uvicorn app.backend.main:app --reload --port 8765

frontend:
	cd app/frontend && npm run dev

dev:
	@echo "Starting backend (:8765) and frontend (:5173) — Ctrl-C kills both"
	@trap 'kill 0' INT TERM EXIT; \
	  poetry run uvicorn app.backend.main:app --reload --port 8765 & \
	  (cd app/frontend && npm run dev) & \
	  wait

install: install-backend install-frontend

install-backend:
	poetry install

install-frontend:
	cd app/frontend && npm install
