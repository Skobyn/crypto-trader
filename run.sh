#!/bin/bash
set -e
cd "$(dirname "$0")"
pip install -r backend/requirements.txt -q
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
