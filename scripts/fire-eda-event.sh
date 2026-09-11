#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT="${1:-$ROOT/events/lightwell-critical-advisory.json}"
AAP_HOST="${AAP_HOST:-$(kubectl get route aap -n aap-operator -o jsonpath='{.spec.host}' 2>/dev/null || true)}"
AAP_PASSWORD="${AAP_PASSWORD:-$(kubectl get secret aap-admin-password -n aap-operator -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)}"
TOKEN="${AAP_TOKEN:-}"
STREAM_TOKEN="${LIGHTWELL_EVENT_STREAM_TOKEN:-lightwell-demo-stream-token}"

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(curl -sk -u "admin:${AAP_PASSWORD}" -X POST "https://${AAP_HOST}/api/gateway/v1/tokens/" \
    -H "Content-Type: application/json" -d '{"description":"lightwell-fire-eda"}' | jq -r '.token')"
fi

STREAM_JSON="$(curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${AAP_HOST}/api/eda/v1/event-streams/?name=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("Lightwell AAP Demo"))')")"
STREAM_URL="$(echo "${STREAM_JSON}" | jq -r '.results[0].url // empty')"
STREAM_UUID="$(echo "${STREAM_JSON}" | jq -r '.results[0].uuid // empty')"

if [[ -z "${STREAM_URL}" || "${STREAM_URL}" == "null" ]]; then
  STREAM_URL="https://${AAP_HOST}/eda-event-streams/api/eda/v1/event-streams/${STREAM_UUID}/"
fi

echo "POST $(basename "${EVENT}") -> ${STREAM_URL}"
curl -sk -X POST "${STREAM_URL}" \
  -H "Authorization: Bearer ${STREAM_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @"${EVENT}" \
  -w "\nHTTP %{http_code}\n"
