#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT="${1:-$ROOT/events/lightwell-critical-advisory.json}"
AO_HOST="${AO_HOST:-$(kubectl get route automation-orchestrator -n automation-orchestrator -o jsonpath='{.spec.host}' 2>/dev/null || true)}"
AO_PASSWORD="${AO_PASSWORD:-$(kubectl get secret automation-orchestrator-initial-admin-password -n automation-orchestrator -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)}"
WEBHOOK_URL="${AO_WEBHOOK_URL:-https://${AO_HOST}/api/v1/webhooks/lightwell-advisory}"

TOKEN="$(curl -sk -X POST "https://${AO_HOST}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg user admin --arg pass "${AO_PASSWORD}" '{username:$user,password:$pass}')" \
  | jq -r '.access_token')"

echo "POST $(basename "${EVENT}") -> ${WEBHOOK_URL}"
curl -sk -X POST "${WEBHOOK_URL}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @"${EVENT}" \
  -w "\nHTTP %{http_code}\n"
