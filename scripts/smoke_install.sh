#!/usr/bin/env bash
# Install Scrivio from a clean checkout by following the README, then check
# that what comes up is the current interface and that it works.
#
#     scripts/smoke_install.sh [directory-to-install-into]
#
# Uses demo mode, so it needs no provider and spends nothing. Run by CI on
# every change and by hand before a release. It clones the repository as
# committed: uncommitted changes are NOT tested.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-$(mktemp -d)/scrivio}"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8877}"

step() { printf '\n== %s\n' "$*"; }

step "A clean checkout, in ${TARGET}"
git clone --quiet --no-hardlinks "${REPO}" "${TARGET}"
cd "${TARGET}"
test ! -e web/dist        || { echo "web/dist is in the checkout; it must be built, not committed"; exit 1; }
test ! -e web/node_modules || { echo "node_modules is in the checkout"; exit 1; }
test ! -e .env            || { echo ".env is in the checkout"; exit 1; }

step "README step 1: Python"
"${PYTHON}" --version
"${PYTHON}" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt -c constraints.txt

step "README step 2: the interface"
node --version
(cd web && npm ci --no-audit --no-fund --silent && npm run build --silent)
test -f web/dist/index.html || { echo "the build produced no index.html"; exit 1; }

step "README step 3: the setup check"
# No provider is configured here, and the check must say so.
if SCRIVIO_ENV_FILE=/nonexistent LLM_CLI=qwen .venv/bin/python -m api.doctor; then
  echo "the setup check reported ready with no provider configured"; exit 1
fi
SCRIVIO_DEMO=1 SCRIVIO_ENV_FILE=/nonexistent .venv/bin/python -m api.doctor

step "The fact evaluation, in the environment just installed"
.venv/bin/python -m evals.resume_guard_eval | tail -1

step "Start it, in demo mode, told where its data is by its settings file alone"
# The data folder is named in the settings file and nowhere else, which
# is how Settings in the interface leaves it. The server and the backup
# tool once disagreed about exactly this arrangement.
printf "ARTICLE_OUTPUT_DIR='%s'\n" "${TARGET}/.smoke-output" > "${TARGET}/.smoke-settings.env"
unset ARTICLE_OUTPUT_DIR
export SCRIVIO_DEMO=1 SCRIVIO_ENV_FILE="${TARGET}/.smoke-settings.env"
export SCRIVIO_STATE_DIR="${TARGET}/.smoke-state"
PORT="${PORT}" .venv/bin/python -m api > "${TARGET}/.smoke-server.log" 2>&1 &
SERVER=$!
trap 'kill ${SERVER} 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do
  curl --silent --fail "http://127.0.0.1:${PORT}/health" > /dev/null && break
  kill -0 ${SERVER} 2>/dev/null || { cat "${TARGET}/.smoke-server.log"; exit 1; }
  sleep 0.2
done

step "What is being served"
BASE="http://127.0.0.1:${PORT}"
curl --silent --fail "${BASE}/health" | grep -q '"ok":true'
PAGE="$(curl --silent --fail "${BASE}/")"
echo "${PAGE}" | grep -q '<div id="root">' \
  || { echo "the page at / is not the current interface"; exit 1; }
echo "${PAGE}" | grep -q 'AI Article Studio' \
  && { echo "the page at / is the older classic interface"; exit 1; }
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "${BASE}/resumes")" = "401" \
  || { echo "an unpaired client was not refused"; exit 1; }
test "$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Host: evil.example' "${BASE}/health")" = "400" \
  || { echo "a request for another host name was not refused"; exit 1; }
grep -q "pairing code" "${TARGET}/.smoke-server.log" \
  || { echo "no pairing code was shown"; exit 1; }

step "A workflow, end to end, as a paired browser would"
.venv/bin/python - "${BASE}" <<'PY'
import json, os, sys, time, urllib.request
sys.path.insert(0, os.getcwd())
from api import boundary

base = sys.argv[1]
cookie = f"{boundary.COOKIE_NAME}={boundary.mint_session()}"

def call(method, path, body=None):
    request = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Cookie": cookie})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.headers, response.read()

def wait(path, until, what):
    for _ in range(150):
        doc = json.loads(call("GET", path)[2])
        if until(doc):
            return doc
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {what}")

mode = json.loads(call("GET", "/mode")[2])
assert mode["demo"] and mode["interface"] == "modern", mode

resume = "Jordan Rivera\nBackend Engineer\njordan@example.com\n\nExperience\nSoftware Engineer, Acme Corp\n- Built Kafka pipelines processing 2M events/day\n" * 3
created = json.loads(call("POST", "/resumes", {
    "resume_text": resume, "jd_text": "Senior engineer. Requirements: Python, Kafka."})[2])
rid = created["resume_id"]
wait(f"/resumes/{rid}", lambda d: d["status"] == "ready", "the analysis")
call("POST", f"/resumes/{rid}/tailor")
doc = wait(f"/resumes/{rid}", lambda d: d["tailor_status"] == "idle" and d["tailored"], "the tailoring")
assert doc["tailored"]["warnings"][0].startswith("DEMO"), "demo output must be labelled"

try:
    call("GET", f"/resumes/{rid}/download?fmt=md&version=tailored")
    raise SystemExit("an unfinished resume was exported")
except urllib.error.HTTPError as refused:
    assert refused.code == 409, refused.code

call("POST", f"/resumes/{rid}/fill-metrics", {"values": ["35"]})
status, headers, body = call("GET", f"/resumes/{rid}/download?fmt=md&version=tailored")
assert headers["X-Scrivio-Export"] == "final" and b"[METRIC]" not in body

session = json.loads(call("POST", "/interviews", {"topic": "Kafka", "num_questions": 3})[2])
answer = json.loads(call("POST", f"/interviews/{session['session_id']}/answers", {
    "question_id": session["questions"][0]["id"],
    "answer": "A consumer group rebalances when membership changes, pausing consumption meanwhile."})[2])
assert answer["evaluation"]["score"] > 0
print("resume imported, tailored, completed, and exported; an interview answered and graded")
PY

step "Back it up with the tool, lose it, and put it back"
kill ${SERVER} 2>/dev/null || true
wait ${SERVER} 2>/dev/null || true
.venv/bin/python -m api.data where | tee "${TARGET}/.smoke-where.txt"
grep -q "\.smoke-output/demo-mode" "${TARGET}/.smoke-where.txt" \
  || { echo "the backup tool is not looking where the server kept its records"; exit 1; }
.venv/bin/python -m api.data backup "${TARGET}/.smoke-backup.zip"
RECORDS="$(ls "${TARGET}/.smoke-output/demo-mode/resumes"/*.json | wc -l | tr -d ' ')"
test "${RECORDS}" -ge 1 || { echo "the server left no resume to back up"; exit 1; }
unzip -l "${TARGET}/.smoke-backup.zip" | grep -q "resumes/.*\.json" \
  || { echo "the backup does not hold the resume the server saved"; exit 1; }
find "${TARGET}/.smoke-output/demo-mode/resumes" -name '*.json' -delete
.venv/bin/python -m api.data restore "${TARGET}/.smoke-backup.zip"
test "$(ls "${TARGET}/.smoke-output/demo-mode/resumes"/*.json | wc -l | tr -d ' ')" = "${RECORDS}" \
  || { echo "the restore did not put the resume back"; exit 1; }

step "Passed"
