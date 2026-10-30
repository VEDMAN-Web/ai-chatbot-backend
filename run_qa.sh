#!/usr/bin/env bash
# ==============================================================================
# SparkBeta Tech AI Chatbot - Master QA Automated Test Runner (Bash)
# Target: Staging Environment (Tajlandia.com, VarsoviaEstate.com, Thai.estate)
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================================================="
echo "SPARKBETA TECH AI CHATBOT - AUTOMATED QA TEST RUNNER (BASH)"
echo "=============================================================================="

# 1. Environment Guard
export ENVIRONMENT="staging"
echo "[1/3] Environment check: ENVIRONMENT=${ENVIRONMENT}"

# 2. Check Backend Server Health
echo "[2/3] Checking backend health on http://127.0.0.1:8000..."
if curl -s -f http://127.0.0.1:8000/health > /dev/null; then
    echo "      Backend health verified on http://127.0.0.1:8000"
else
    echo "      WARNING: Backend is not responding on http://127.0.0.1:8000"
    echo "      Ensure uvicorn is running: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
fi

# 3. Execute Parallel Test Runner
echo "[3/3] Launching parallel test execution..."
python scripts/run_qa_parallel.py "$@"

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo ">>> ALL AUTOMATED QA TEST SUITES PASSED (EXIT CODE 0) <<<"
else
    echo ""
    echo ">>> AUTOMATED QA TEST SUITE FAILED (EXIT CODE ${EXIT_CODE}) <<<"
fi

exit $EXIT_CODE

