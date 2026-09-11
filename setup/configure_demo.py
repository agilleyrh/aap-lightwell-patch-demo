#!/usr/bin/env python3
"""Provision the Lightwell patch demos on AAP Controller, EDA, and Automation Orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
AO_WORKFLOW_FILE = REPO_ROOT / "ao" / "lightwell-intelligent-patch.json"

PROJECT_NAME = "Lightwell Patch Demo"
PROJECT_URL_DEFAULT = "https://github.com/agilleyrh/aap-lightwell-patch-demo.git"
WORKFLOW_NAME = "Lightwell | Application Patch Pipeline"
INVENTORY_NAME = "Demo Inventory"
ORGANIZATION_NAME = "Default"
EDA_DE_IMAGE = os.environ.get(
    "LIGHTWELL_EDA_DE_IMAGE",
    "registry.redhat.io/ansible-automation-platform-27/de-minimal-rhel9:latest",
)

JOB_TEMPLATES = [
    ("Lightwell | Analyze Advisory", "playbooks/analyze_advisory.yml", False),
    ("Lightwell | Sync Catalog", "playbooks/sync_lightwell_repo.yml", False),
    ("Lightwell | Rebuild Application", "playbooks/rebuild_app.yml", False),
    ("Lightwell | Test Application", "playbooks/test_app.yml", False),
    ("Lightwell | Promote Application", "playbooks/promote_app.yml", False),
    ("Lightwell | Notify Complete", "playbooks/notify_complete.yml", False),
    ("Lightwell | Post AO Webhook", "playbooks/post_ao_webhook.yml", False),
    ("Lightwell | Launch Patch Workflow", "playbooks/launch_aap_workflow.yml", True),
]


class API:
    def __init__(self, base: str, token: str | None = None, username: str | None = None, password: str | None = None) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.context = ssl._create_unverified_context()

    def request(self, path: str, method: str = "GET", payload: Any = None, extra_headers: dict[str, str] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = path if path.startswith("http") else f"{self.base}{path}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        if self.username and not self.token:
            import base64

            raw = f"{self.username}:{self.password or ''}".encode()
            request.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=90) as response:
                raw_body = response.read()
                return json.loads(raw_body) if raw_body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc

    def find(self, endpoint: str, name: str, extra: str = "") -> dict[str, Any] | None:
        query = f"name={urllib.parse.quote(name)}"
        if extra:
            query += f"&{extra}"
        result = self.request(f"/{endpoint}/?{query}")
        return next(iter(result.get("results", [])), None)

    def upsert(self, endpoint: str, name: str, payload: dict[str, Any], extra: str = "") -> dict[str, Any]:
        current = self.find(endpoint, name, extra)
        if current:
            updated = self.request(f"/{endpoint}/{current['id']}/", "PATCH", payload)
            print(f"  updated {endpoint}: {name}")
            return updated if updated else {**current, **payload, "id": current["id"]}
        created = self.request(f"/{endpoint}/", "POST", payload)
        print(f"  created {endpoint}: {name}")
        return created


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def discover_aap() -> tuple[str, str]:
    host = os.environ.get("AAP_HOST") or run(
        ["kubectl", "get", "route", "aap", "-n", "aap-operator", "-o", "jsonpath={.spec.host}"]
    )
    password = os.environ.get("AAP_PASSWORD") or run(
        [
            "kubectl",
            "get",
            "secret",
            "aap-admin-password",
            "-n",
            "aap-operator",
            "-o",
            "jsonpath={.data.password}",
        ]
    )
    if password and not os.environ.get("AAP_PASSWORD"):
        import base64

        password = base64.b64decode(password).decode()
    if not host or not password:
        raise RuntimeError("Set AAP_HOST and AAP_PASSWORD or run this on an aap-demo cluster")
    return host, password


def discover_ao() -> tuple[str, str]:
    host = os.environ.get("AO_HOST") or run(
        [
            "kubectl",
            "get",
            "route",
            "automation-orchestrator",
            "-n",
            "automation-orchestrator",
            "-o",
            "jsonpath={.spec.host}",
        ]
    )
    password = os.environ.get("AO_PASSWORD") or run(
        [
            "kubectl",
            "get",
            "secret",
            "automation-orchestrator-initial-admin-password",
            "-n",
            "automation-orchestrator",
            "-o",
            "jsonpath={.data.password}",
        ]
    )
    if password and not os.environ.get("AO_PASSWORD"):
        import base64

        password = base64.b64decode(password).decode()
    return host, password


def aap_login(host: str, username: str, password: str) -> API:
    basic = API(f"https://{host}", username=username, password=password)
    created = basic.request("/api/gateway/v1/tokens/", "POST", {"description": "lightwell-demo-setup"})
    token = created.get("token")
    if not token:
        raise RuntimeError(f"AAP did not return a gateway token: {created}")
    return API(f"https://{host}", token=token)


def wait_project(api: API, project_id: int, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        project = api.request(f"/api/controller/v2/projects/{project_id}/")
        status = project.get("status")
        if status == "successful":
            return
        if status in {"failed", "error", "canceled"}:
            raise RuntimeError(f"AAP project sync ended {status}")
        time.sleep(5)
    raise RuntimeError("Timed out waiting for AAP project sync")


def associate_credential(api: API, template_id: int, credential_id: int) -> None:
    existing = api.request(f"/api/controller/v2/job_templates/{template_id}/credentials/")
    ids = {item["id"] for item in existing.get("results", [])}
    if credential_id in ids:
        return
    api.request(
        f"/api/controller/v2/job_templates/{template_id}/credentials/",
        "POST",
        {"id": credential_id},
    )


def delete_workflow_nodes(api: API, workflow_id: int) -> None:
    nodes = api.request(f"/api/controller/v2/workflow_job_templates/{workflow_id}/workflow_nodes/?page_size=200")
    for node in nodes.get("results", []):
        api.request(f"/api/controller/v2/workflow_job_template_nodes/{node['id']}/", "DELETE")


def add_node(api: API, workflow_id: int, identifier: str, unified_id: int, extra: dict[str, Any] | None = None) -> int:
    payload: dict[str, Any] = {
        "workflow_job_template": workflow_id,
        "unified_job_template": unified_id,
        "identifier": identifier,
        "all_parents_must_converge": True,
    }
    if extra:
        payload["extra_data"] = extra
    created = api.request("/api/controller/v2/workflow_job_template_nodes/", "POST", payload)
    return created["id"]


def link(api: API, source_id: int, dest_id: int, rel: str = "success_nodes") -> None:
    api.request(
        f"/api/controller/v2/workflow_job_template_nodes/{source_id}/{rel}/",
        "POST",
        {"id": dest_id},
    )


def provision_controller(api: API, project_url: str, project_branch: str) -> dict[str, Any]:
    print("Configuring AAP Controller")
    org = api.find("api/controller/v2/organizations", ORGANIZATION_NAME)
    if not org:
        raise RuntimeError("Default organization was not found")
    inventory = api.find("api/controller/v2/inventories", INVENTORY_NAME)
    if not inventory:
        raise RuntimeError("Demo Inventory was not found")

    project = api.upsert(
        "api/controller/v2/projects",
        PROJECT_NAME,
        {
            "name": PROJECT_NAME,
            "description": "Simulated Lightwell Network catalog and patch pipeline playbooks",
            "organization": org["id"],
            "scm_type": "git",
            "scm_url": project_url,
            "scm_branch": project_branch,
            "scm_update_on_launch": True,
            "scm_delete_on_update": False,
            "allow_override": False,
        },
    )
    wait_project(api, project["id"])
    print(f"  project synced: {PROJECT_NAME}")

    aap_cred = api.find("api/controller/v2/credentials", "AAP Credential")
    templates: dict[str, dict[str, Any]] = {}
    for name, playbook, needs_aap_cred in JOB_TEMPLATES:
        payload = {
            "name": name,
            "description": f"Lightwell demo playbook {playbook}",
            "job_type": "run",
            "organization": org["id"],
            "project": project["id"],
            "playbook": playbook,
            "inventory": inventory["id"],
            "ask_variables_on_launch": True,
        }
        template = api.upsert("api/controller/v2/job_templates", name, payload)
        if needs_aap_cred and aap_cred:
            associate_credential(api, template["id"], aap_cred["id"])
            print(f"  attached AAP Credential to {name}")
        templates[name] = template

    extra_vars = {
        "advisory_id": "LW-2026-00412",
        "cve_id": "CVE-2026-55102",
        "severity": "Critical",
        "package_name": "urllib3",
        "patched_version": "2.2.3+lightwell1",
        "affected_app": "payments-api",
        "target_app": "payments-api",
        "lightwell_patch_available": True,
    }
    workflow = api.upsert(
        "api/controller/v2/workflow_job_templates",
        WORKFLOW_NAME,
        {
            "name": WORKFLOW_NAME,
            "description": "Sync Lightwell repo, rebuild, test, and promote a patched application",
            "organization": org["id"],
            "inventory": inventory["id"],
            "ask_variables_on_launch": True,
            "allow_simultaneous": True,
            "extra_vars": json.dumps(extra_vars),
        },
    )
    delete_workflow_nodes(api, workflow["id"])

    jt = {name: templates[name]["id"] for name, _, _ in JOB_TEMPLATES}
    n_sync = add_node(api, workflow["id"], "sync-lightwell-project", project["id"])
    n_analyze = add_node(api, workflow["id"], "analyze-advisory", jt["Lightwell | Analyze Advisory"])
    n_catalog = add_node(api, workflow["id"], "sync-lightwell-package", jt["Lightwell | Sync Catalog"])
    n_rebuild = add_node(api, workflow["id"], "rebuild-application", jt["Lightwell | Rebuild Application"])
    n_test = add_node(api, workflow["id"], "test-application", jt["Lightwell | Test Application"])
    n_promote = add_node(api, workflow["id"], "promote-application", jt["Lightwell | Promote Application"])
    n_ok = add_node(
        api,
        workflow["id"],
        "notify-patched",
        jt["Lightwell | Notify Complete"],
        {"notify_status": "patched"},
    )
    n_investigate = add_node(
        api,
        workflow["id"],
        "notify-investigate",
        jt["Lightwell | Notify Complete"],
        {"notify_status": "investigate"},
    )

    link(api, n_sync, n_analyze)
    link(api, n_analyze, n_catalog)
    link(api, n_analyze, n_investigate, "failure_nodes")
    link(api, n_catalog, n_rebuild)
    link(api, n_rebuild, n_test)
    link(api, n_test, n_promote)
    link(api, n_promote, n_ok)
    print(f"  workflow nodes linked: {WORKFLOW_NAME}")
    return {
        "organization_id": org["id"],
        "inventory_id": inventory["id"],
        "project_id": project["id"],
        "workflow_id": workflow["id"],
        "templates": {name: templates[name]["id"] for name, _, _ in JOB_TEMPLATES},
    }


def eda_upsert(api: API, endpoint: str, name: str, payload: dict[str, Any], name_field: str = "name") -> dict[str, Any]:
    listing = api.request(f"/api/eda/v1/{endpoint}/?page_size=200")
    current = next((item for item in listing.get("results", []) if item.get(name_field) == name), None)
    if current:
        updated = api.request(f"/api/eda/v1/{endpoint}/{current['id']}/", "PATCH", payload)
        print(f"  updated eda {endpoint}: {name}")
        return updated if updated else current
    created = api.request(f"/api/eda/v1/{endpoint}/", "POST", payload)
    print(f"  created eda {endpoint}: {name}")
    return created


def provision_eda(api: API, controller: dict[str, Any], project_url: str, project_branch: str, aap_token: str, aap_host: str) -> dict[str, Any]:
    print("Configuring Event-Driven Ansible")
    orgs = api.request("/api/eda/v1/organizations/?name=Default")
    org = next(iter(orgs.get("results", [])), None)
    if not org:
        raise RuntimeError("EDA Default organization was not found")

    result: dict[str, Any] = {"organization_id": org["id"]}
    try:
        de = eda_upsert(
            api,
            "decision-environments",
            "Lightwell Decision Environment",
            {
                "name": "Lightwell Decision Environment",
                "description": "Minimal AAP 2.7 decision environment for Lightwell rulebooks",
                "image_url": EDA_DE_IMAGE,
                "organization_id": org["id"],
                "pull_policy": "missing",
            },
        )
        result["decision_environment_id"] = de["id"]
    except RuntimeError as exc:
        print(f"  warning: decision environment skipped: {exc}")

    project = eda_upsert(
        api,
        "projects",
        PROJECT_NAME,
        {
            "name": PROJECT_NAME,
            "description": "Lightwell EDA rulebooks",
            "organization_id": org["id"],
            "url": project_url,
            "scm_branch": project_branch,
        },
    )
    result["eda_project_id"] = project["id"]
    deadline = time.time() + 180
    while time.time() < deadline:
        project = api.request(f"/api/eda/v1/projects/{project['id']}/")
        if project.get("import_state") in {"completed", "failed"}:
            break
        time.sleep(5)
    if project.get("import_state") != "completed":
        print(f"  warning: EDA project import state is {project.get('import_state')}")

    rulebooks = api.request(f"/api/eda/v1/rulebooks/?project_id={project['id']}&page_size=50")
    by_name = {item["name"]: item for item in rulebooks.get("results", [])}
    result["rulebooks"] = {name: item["id"] for name, item in by_name.items()}

    stream_cred = eda_upsert(
        api,
        "eda-credentials",
        "Lightwell Event Stream Token",
        {
            "name": "Lightwell Event Stream Token",
            "organization_id": org["id"],
            "credential_type_id": 8,
            "inputs": {
                "auth_type": "token",
                "token": os.environ.get("LIGHTWELL_EVENT_STREAM_TOKEN", "lightwell-demo-stream-token"),
                "http_header_key": "Authorization",
            },
        },
    )
    aap_cred = eda_upsert(
        api,
        "eda-credentials",
        "Lightwell AAP Controller",
        {
            "name": "Lightwell AAP Controller",
            "organization_id": org["id"],
            "credential_type_id": 4,
            "inputs": {
                "host": f"https://{aap_host}/api/controller/",
                "oauth_token": aap_token,
                "verify_ssl": False,
            },
        },
    )

    stream = eda_upsert(
        api,
        "event-streams",
        "Lightwell AAP Demo",
        {
            "name": "Lightwell AAP Demo",
            "organization_id": org["id"],
            "event_stream_type": "token",
            "eda_credential_id": stream_cred["id"],
        },
    )
    result["event_stream"] = stream
    result["event_stream_token"] = os.environ.get("LIGHTWELL_EVENT_STREAM_TOKEN", "lightwell-demo-stream-token")

    try:
        tokens = api.request("/api/eda/v1/users/me/awx-tokens/")
        token_row = next((item for item in tokens.get("results", []) if item.get("name") == "lightwell-demo"), None)
        if not token_row:
            token_row = api.request(
                "/api/eda/v1/users/me/awx-tokens/",
                "POST",
                {"name": "lightwell-demo", "token": aap_token},
            )
            print("  created EDA controller token lightwell-demo")
        result["awx_token_id"] = token_row["id"]
    except RuntimeError as exc:
        print(f"  warning: EDA controller token skipped: {exc}")

    aap_rulebook = next((item for name, item in by_name.items() if "aap-pipeline" in name), None)
    if aap_rulebook and result.get("decision_environment_id") and result.get("awx_token_id"):
        sources = api.request(f"/api/eda/v1/rulebooks/{aap_rulebook['id']}/sources/")
        source = next((item for item in sources.get("results", []) if item.get("name") == "lightwell_advisories"), {})
        mappings = json.dumps(
            [
                {
                    "source_name": "lightwell_advisories",
                    "event_stream_id": stream["id"],
                    "event_stream_name": stream["name"],
                    "rulebook_hash": source.get("rulebook_hash", ""),
                }
            ]
        )
        try:
            activation = eda_upsert(
                api,
                "activations",
                "Lightwell AAP Pipeline",
                {
                    "name": "Lightwell AAP Pipeline",
                    "description": "Demo 1: EDA event stream launches the AAP Lightwell workflow",
                    "organization_id": org["id"],
                    "decision_environment_id": result["decision_environment_id"],
                    "project_id": project["id"],
                    "rulebook_id": aap_rulebook["id"],
                    "awx_token_id": result["awx_token_id"],
                    "eda_credentials": [aap_cred["id"]],
                    "is_enabled": os.environ.get("LIGHTWELL_ENABLE_EDA", "0") == "1",
                    "restart_policy": "on-failure",
                    "log_level": "info",
                    "source_mappings": mappings,
                },
            )
            result["activation_id"] = activation["id"]
            result["activation_enabled"] = activation.get("is_enabled")
        except RuntimeError as exc:
            print(f"  warning: EDA activation skipped: {exc}")
    else:
        print("  warning: EDA activation not created (rulebook, DE, or controller token missing)")
    return result


def ao_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get("resources", payload.get("results", []))
        return value if isinstance(value, list) else []
    return []


def provision_ao(host: str, password: str, aap_info: dict[str, Any]) -> dict[str, Any]:
    print("Configuring Automation Orchestrator")
    login = API(f"https://{host}").request("/api/v1/auth/login", "POST", {"username": "admin", "password": password})
    token = login.get("access_token")
    if not token:
        raise RuntimeError("AO login failed")
    ao = API(f"https://{host}", token=token)
    projects = ao_items(ao.request("/api/v1/projects?limit=50"))
    project = next((item for item in projects if item.get("is_default")), projects[0] if projects else None)
    if not project:
        raise RuntimeError("AO has no project")

    credentials = ao_items(ao.request("/api/v1/credentials?limit=50"))
    integrations = ao_items(ao.request("/api/v1/integrations?limit=50"))
    aap_cred = next((item for item in credentials if item.get("name") == "aap-demo AAP Token"), None)
    aap_int = next((item for item in integrations if item.get("name") == "aap-demo AAP"), None)
    if not aap_cred or not aap_int:
        raise RuntimeError("aap-demo AAP integration/credential is missing from AO")

    document = json.loads(AO_WORKFLOW_FILE.read_text())
    for node in document.get("nodes", []):
        if node.get("type") == "aap_job_template":
            node.setdefault("parameters", {})
            node["parameters"]["credential_id"] = aap_cred["id"]
            node["parameters"]["integration_id"] = aap_int["id"]
        if node.get("type") == "agentic":
            node.setdefault("parameters", {})
            node["parameters"]["tool_selection_strategy"] = "ALL"
            node["parameters"].pop("credential_id", None)

    existing = ao_items(ao.request("/api/v1/workflows?limit=100"))
    current = next((item for item in existing if item.get("name") == document["name"]), None)
    payload = {
        "name": document["name"],
        "description": document.get("description"),
        "labels": {"source": "agilleyrh/aap-lightwell-patch-demo", "demo": "lightwell"},
        "workflow_definition": document,
        "project_id": project["id"],
    }
    if current:
        ao.request(
            f"/api/v1/workflows/{current['id']}",
            "PATCH",
            {
                "description": payload["description"],
                "labels": payload["labels"],
                "workflow_definition": document,
                "change_description": "Updated Lightwell intelligent patch pipeline",
            },
        )
        workflow_id = current["id"]
        print(f"  updated AO workflow: {document['name']}")
    else:
        created = ao.request("/api/v1/workflows", "POST", payload)
        workflow_id = created["id"]
        print(f"  created AO workflow: {document['name']}")

    versions = ao_items(ao.request(f"/api/v1/workflows/{workflow_id}/versions?limit=5"))
    version = versions[0] if versions else None
    published = False
    if version:
        version_number = version.get("version", 1)
        for path in (
            f"/api/v1/workflows/{workflow_id}/versions/{version_number}/publish",
            f"/api/v1/workflows/{workflow_id}/versions/{version['id']}/publish",
        ):
            try:
                ao.request(path, "POST", {"change_description": "Publish Lightwell demo"})
                published = True
                print("  published AO workflow")
                break
            except RuntimeError:
                continue
    webhook_url = f"https://{host}/api/v1/webhooks/lightwell-advisory"
    return {
        "workflow_id": workflow_id,
        "project_id": project["id"],
        "published": published,
        "webhook_url": webhook_url,
        "ui": f"https://{host}/",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-url", default=os.environ.get("LIGHTWELL_PROJECT_URL", PROJECT_URL_DEFAULT))
    parser.add_argument("--project-branch", default=os.environ.get("LIGHTWELL_PROJECT_BRANCH", "main"))
    parser.add_argument("--skip-eda", action="store_true")
    parser.add_argument("--skip-ao", action="store_true")
    args = parser.parse_args()

    aap_host, aap_password = discover_aap()
    api = aap_login(aap_host, "admin", aap_password)
    controller = provision_controller(api, args.project_url, args.project_branch)

    eda: dict[str, Any] = {}
    if not args.skip_eda:
        try:
            eda = provision_eda(api, controller, args.project_url, args.project_branch, api.token or "", aap_host)
        except RuntimeError as exc:
            print(f"EDA configuration failed: {exc}")

    ao: dict[str, Any] = {}
    if not args.skip_ao:
        ao_host, ao_password = discover_ao()
        if ao_host and ao_password:
            try:
                ao = provision_ao(ao_host, ao_password, controller)
            except RuntimeError as exc:
                print(f"AO configuration failed: {exc}")
        else:
            print("AO host/password not found; skipping AO import")

    summary = {
        "aap": f"https://{aap_host}/",
        "workflow": WORKFLOW_NAME,
        "workflow_id": controller["workflow_id"],
        "project": PROJECT_NAME,
        "eda_event_stream": (eda.get("event_stream") or {}).get("url") or (eda.get("event_stream") or {}).get("uuid"),
        "eda_event_stream_id": (eda.get("event_stream") or {}).get("id"),
        "eda_activation_enabled": eda.get("activation_enabled"),
        "ao_workflow": "Lightwell Intelligent Patch Pipeline",
        "ao_webhook_url": ao.get("webhook_url"),
        "ao_ui": ao.get("ui"),
        "sample_event": "events/lightwell-critical-advisory.json",
    }
    print("\nLightwell demo is configured:\n")
    print(json.dumps(summary, indent=2))
    out = Path("/tmp/lightwell-demo-endpoints.json")
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
