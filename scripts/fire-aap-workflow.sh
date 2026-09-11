#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT="${1:-$ROOT/events/lightwell-critical-advisory.json}"
AAP_HOST="${AAP_HOST:-$(kubectl get route aap -n aap-operator -o jsonpath='{.spec.host}' 2>/dev/null || true)}"
AAP_PASSWORD="${AAP_PASSWORD:-$(kubectl get secret aap-admin-password -n aap-operator -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)}"
TOKEN="${AAP_TOKEN:-}"

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(curl -sk -u "admin:${AAP_PASSWORD}" -X POST "https://${AAP_HOST}/api/gateway/v1/tokens/" \
    -H "Content-Type: application/json" -d '{"description":"lightwell-fire-workflow"}' | jq -r '.token')"
fi

WF_ID="$(curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${AAP_HOST}/api/controller/v2/workflow_job_templates/?name=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("Lightwell | Application Patch Pipeline"))')" \
  | jq -r '.results[0].id')"

echo "Launching AAP workflow ${WF_ID} with $(basename "${EVENT}")"
LAUNCH="$(curl -sk -X POST "https://${AAP_HOST}/api/controller/v2/workflow_job_templates/${WF_ID}/launch/" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d "{\"extra_vars\": $(jq -c '. + {target_app: .affected_app}' "${EVENT}")}")"
JOB="$(echo "${LAUNCH}" | jq -r '.workflow_job // .id')"
echo "Workflow job ${JOB}"
echo "UI: https://${AAP_HOST}/execution/jobs/workflow/${JOB}/details"
