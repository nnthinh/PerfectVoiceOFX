#!/usr/bin/env bash
# Kill the sidecar mid-life (SIGKILL). Process must die; loopback must drop.
# Does not touch Resolve — crash isolation is the sidecar's job.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
engine="$root/engine"
python="${PYTHON:-python3}"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/pv-chaos.XXXXXX")"
token_file="$tmp/run.token"
# 256-bit hex
token="c8a05c1111e11111e11111e11111e11111e11111e11111e11111e11111e11111"
printf '%s\n' "$token" > "$token_file"
chmod 600 "$token_file"

cleanup() {
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT

export PYTHONPATH="$engine${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export PERFECTVOICE_STUB_HOLD=30

"$python" -u -m perfectvoice_engine.serve serve \
  --bind 127.0.0.1 \
  --port 0 \
  --idle-seconds 0 \
  --token-file "$token_file" \
  >"$tmp/stdout" 2>"$tmp/stderr" &
pid=$!

ready=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "engine exited before READY:" >&2
    cat "$tmp/stderr" >&2 || true
    exit 1
  fi
  if ready="$(grep -m1 '^READY ' "$tmp/stdout" 2>/dev/null || true)" && [ -n "$ready" ]; then
    break
  fi
  sleep 0.1
done

if [ -z "$ready" ]; then
  echo "no READY line:" >&2
  cat "$tmp/stderr" >&2 || true
  exit 1
fi

url="${ready#READY }"
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $token" \
  "$url/v1/health")"
if [ "$code" != "200" ]; then
  echo "health before kill: HTTP $code" >&2
  exit 1
fi

kill -9 "$pid"
set +e
wait "$pid"
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "expected non-zero after SIGKILL, got 0" >&2
  exit 1
fi

if kill -0 "$pid" 2>/dev/null; then
  echo "process still alive after SIGKILL" >&2
  exit 1
fi

set +e
curl -sS --max-time 2 \
  -H "Authorization: Bearer $token" \
  "$url/v1/health" >/dev/null 2>"$tmp/curl.err"
curl_status=$?
set -e
if [ "$curl_status" -eq 0 ]; then
  echo "loopback still served after kill" >&2
  exit 1
fi

echo "chaos_kill_engine: SIGKILL dropped sidecar (exit $status, curl $curl_status)"
