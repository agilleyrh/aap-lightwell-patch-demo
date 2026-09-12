# Lightwell Patch Pipeline demos

Two working Ansible Automation Platform demos that simulate how [Red Hat / IBM Lightwell](https://www.redhat.com/en/about/press-releases/ibm-and-red-hat-expand-lightwell-new-offerings-build-the-trust-infrastructure-ai-era-open-source) patches would be applied with Ansible:

1. **EDA → AAP workflow** — a Lightwell Clearinghouse webhook hits Event-Driven Ansible, which launches a Controller workflow.
2. **Webhook / EDA → Automation Orchestrator** — the same alert is analyzed by AO agent nodes, then AO launches that same AAP workflow.

Lightwell Network publishes **signed, surgically backported application-layer dependencies** (PyPI, Maven, npm) so you can fix a CVE without taking a breaking major upgrade. This repository simulates that catalog and the delivery pipeline around it.

These playbooks are **demo simulations**. They do not talk to the commercial Lightwell Network. They rebuild and promote a local artifact of `payments-api` / `checkout-service` on the AAP control node so the flow runs on an `aap-demo` cluster without extra VMs.

## Pipeline

```text
Lightwell advisory
        │
        ├─ Demo 1: EDA event stream ──────────────────────────────┐
        │                                                         │
        └─ Demo 2: AO webhook → analyze agent → switch ───────────┤
                                                                  ▼
                    AAP workflow: Lightwell | Application Patch Pipeline
                    ┌──────────────┐   success    ┌─────────────┐
                    │ Project sync │─────────────►│ Analyze SBOM│
                    │ (Lightwell   │              └──────┬──────┘
                    │  git repo)   │                     │
                    └──────────────┘         success     │ failure (no signed patch)
                                           ▼             ▼
                                   Sync signed     Notify investigate
                                   Lightwell pkg
                                           ▼
                                      Rebuild app
                                           ▼
                                        Test app
                                           ▼
                                   Promote staging → prod
                                           ▼
                                      Notify patched
```

## Demo 1 — EDA in AAP

Rulebook: [`extensions/eda/rulebooks/lightwell-aap-pipeline.yml`](extensions/eda/rulebooks/lightwell-aap-pipeline.yml)

```text
Lightwell webhook  →  EDA event stream  →  run_workflow_template
                                          Lightwell | Application Patch Pipeline
```

If `lightwell_patch_available` is true, EDA launches the AAP workflow. If not, it logs and stops.

## Demo 2 — Automation Orchestrator

Shared graph: [`ao/lightwell-intelligent-patch.json`](ao/lightwell-intelligent-patch.json)

`setup/configure_demo.py` publishes **three** AO workflows from that graph. Each one already has the trigger defaults for its path, and the decision switch is pinned so a live run does not depend on editing the form or on the LLM picking the route.

```text
Start the matching workflow (no extra vars)
        ▼
Analyze Lightwell Alert  (agent; still shows the JSON decision)
        ▼
    Switch (pinned to this demo)
  Auto-apply (non-prod)  → Launch AAP workflow → Notify patched
  Approval required      → Human approval → Launch AAP workflow
  Investigate            → Investigate agent → Notify investigate
```

| AO workflow | Story | Manual trigger defaults |
|---|---|---|
| **Lightwell Auto-apply (non-prod)** | Staging `payments-api`, signed `urllib3` backport | `LW-2026-00412`, `environment=staging`, patch available |
| **Lightwell Approval (prod)** | Same backport, `payments-api` in **production** | `LW-2026-00412`, `environment=production`, then **Approve Production Lightwell Patch** |
| **Lightwell Investigate** | Production `checkout-service`, no Network backport yet | `LW-2026-00418`, `lightwell_patch_available=false` |

AO does the **decisioning**. AAP still does the **work** (repo sync, rebuild, test, promote) on the auto-apply and (after approval) prod paths.

Agent nodes need an **LLM Provider** credential **and** an enabled model (`llm_model_id`) in AO. `setup/configure_demo.py` auto-binds the `RH MaaS` credential when it exists (override with `AO_AGENT_CREDENTIAL_ID` / `AO_AGENT_LLM_MODEL_ID`). Without that binding, Analyze Lightwell Alert fails with `LLMConfigurationError` during streaming.

Do not attach the cluster **aap-demo MCP Server** to those agent nodes on this MicroShift cluster: its route is `*.apps.127.0.0.1.nip.io`, which AO pods resolve to themselves, and Analyze then fails with `ToolDiscoveryError`.

An older combined workflow named `Lightwell Intelligent Patch Pipeline` may still exist from earlier setups. The three path-specific workflows replace it for demos; you can disable or delete the old one in the AO UI.

## Sample alerts

| File | Advisory | App | Result |
|---|---|---|---|
| [`events/lightwell-critical-advisory.json`](events/lightwell-critical-advisory.json) | LW-2026-00412 / CVE-2026-55102 | staging `payments-api` (`urllib3==2.2.3`) | Auto-apply → rebuild, test, promote |
| [`events/lightwell-prod-approval.json`](events/lightwell-prod-approval.json) | LW-2026-00412 / CVE-2026-55102 | production `payments-api` | Approval gate, then the same AAP pipeline |
| [`events/lightwell-investigate.json`](events/lightwell-investigate.json) | LW-2026-00418 / CVE-2026-55118 | production `checkout-service` (commons-text) | No Network patch yet → investigate |

## Configure this AAP / AO instance

From a laptop that can reach the `aap-demo` cluster:

```bash
python3 setup/configure_demo.py
```

The script:

- Creates the `Lightwell Patch Demo` AAP project (this Git repo)
- Creates the job templates and the `Lightwell | Application Patch Pipeline` workflow
- Imports three AO workflows: **Lightwell Auto-apply (non-prod)**, **Lightwell Approval (prod)**, **Lightwell Investigate**
- Creates an EDA project, token event stream, and (optionally) a disabled rulebook activation

Enable the EDA activation when you want Demo 1 live (it starts another pod):

```bash
LIGHTWELL_ENABLE_EDA=1 python3 setup/configure_demo.py
```

On this `aap-demo` cluster the setup has already been applied:

| Resource | Name / URL |
|---|---|
| AAP project | `Lightwell Patch Demo` |
| AAP workflow | `Lightwell \| Application Patch Pipeline` |
| EDA event stream | `Lightwell AAP Demo` |
| EDA activation | `Lightwell AAP Pipeline` (created **disabled** so it does not consume extra pods) |
| AO workflows | `Lightwell Auto-apply (non-prod)`, `Lightwell Approval (prod)`, `Lightwell Investigate` |

Verified on Controller: apply path workflow job **145** (all nodes successful) and investigate path job **159** (analyze failed as designed → notify-investigate).

## Run the demos

**AAP workflow only** (always works, good smoke test):

```bash
./scripts/fire-aap-workflow.sh events/lightwell-critical-advisory.json
./scripts/fire-aap-workflow.sh events/lightwell-investigate.json
```

**Demo 1 — EDA** (activation must be enabled):

```bash
./scripts/fire-eda-event.sh events/lightwell-critical-advisory.json
```

**Demo 2 — AO** (start the matching workflow in the AO UI, or webhook):

```bash
./scripts/fire-ao-webhook.sh events/lightwell-critical-advisory.json
./scripts/fire-ao-webhook.sh events/lightwell-prod-approval.json
./scripts/fire-ao-webhook.sh events/lightwell-investigate.json
```

From the AO UI, open the workflow and click run. Leave the form defaults — Auto-apply, Approval, and Investigate are three separate workflows so you do not swap variables mid-demo. On **Lightwell Approval (prod)**, approve (or reject) **Approve Production Lightwell Patch**.

Re-import after pulling these changes:

```bash
python3 setup/configure_demo.py --skip-controller --skip-eda
```

## Playbooks

| Job template | Playbook |
|---|---|
| Lightwell \| Analyze Advisory | `playbooks/analyze_advisory.yml` |
| Lightwell \| Sync Catalog | `playbooks/sync_lightwell_repo.yml` |
| Lightwell \| Rebuild Application | `playbooks/rebuild_app.yml` |
| Lightwell \| Test Application | `playbooks/test_app.yml` |
| Lightwell \| Promote Application | `playbooks/promote_app.yml` |
| Lightwell \| Notify Complete | `playbooks/notify_complete.yml` |
| Lightwell \| Post AO Webhook | `playbooks/post_ao_webhook.yml` |
| Lightwell \| Launch Patch Workflow | `playbooks/launch_aap_workflow.yml` |

The first workflow node is a **project update**, which is the repo sync that brings the signed Lightwell package metadata onto the execution node.

## Local playbook smoke test

```bash
ansible-playbook -i localhost, -c local playbooks/analyze_advisory.yml \
  -e advisory_id=LW-2026-00412 -e affected_app=payments-api
```

## What is simulated

| Production Lightwell | This demo |
|---|---|
| Lightwell Network authenticated registry | `catalog/` in this git repo |
| Digitally signed backport wheel/jar | `METADATA.json` + `signature.txt` |
| App rebuild in CI / OpenShift | Artifact + rewritten lockfile under `/tmp/lightwell-demo` |
| Promotion through real clusters | `build → staging → production` directories |
| Clearinghouse embargo feed | JSON files in `events/` posted to EDA or AO |
