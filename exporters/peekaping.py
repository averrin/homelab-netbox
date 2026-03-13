"""Peekaping exporter — upserts HTTP monitors for all NetBox hosts with URLs.

auth_type custom field behaviour:
  "basic"      → HTTP basic auth; LOGIN/PASSWORD fetched from Infisical at /vms/{name}/
  "cloudflare" → monitor created paused (active=false)
  (empty)      → no auth, active

monitors custom field:
  "both" / empty → internal + external (default)
  "internal"     → internal only
  "external"     → external only
"""

from __future__ import annotations

import json
import sys

import pynetbox
import requests

from config import InfisicalConfig, SourceConfig
from logging_utils import c as _c

_NOTIFIER_ID = "6bde4b28-afab-4200-9692-47cf9089adfc"  # Carrot Bot
_DEFAULT_INTERVAL = 60
_DEFAULT_TIMEOUT = 16
_DEFAULT_MAX_RETRIES = 3

def _print(verb: str, name: str, details: dict | None = None, error: str | None = None) -> None:
    verb_color = {"create": _c("green"), "update": _c("yellow"), "delete": _c("yellow"), "skip": _c("dim")}.get(verb, "")
    prefix = "ERROR" if error else "APPLIED"
    tag = f"[{prefix}]" if verb != "skip" else "[SKIP]"
    print(f"{tag} {verb_color}{verb.upper()}{_c('reset')} monitor {_c('cyan')}\"{name}\"{_c('reset')}")
    if error:
        print(f"  {_c('bold')}ERROR{_c('reset')}: {error}")
    elif details:
        for k, v in details.items():
            if isinstance(v, dict) and "old" in v and "new" in v:
                print(f"  {k}: {v['old']} -> {v['new']}")
            else:
                print(f"  {k}: {v}")


def export(netbox_url: str, netbox_token: str, cfg: SourceConfig, infisical_cfg: InfisicalConfig | None = None) -> None:
    headers = {"X-API-Key": cfg.token, "Content-Type": "application/json"}
    base = cfg.url.rstrip("/") + "/api/v1"

    existing = _load_monitors(base, headers)  # name → {id, config, active}

    nb = pynetbox.api(netbox_url, token=netbox_token)
    nb.http_session.verify = False

    infisical = _init_infisical(infisical_cfg)

    stats = {"create": 0, "update": 0, "skip": 0, "delete": 0}
    desired: set[str] = set()

    for obj in list(nb.virtualization.virtual_machines.filter(status="active")) + list(nb.dcim.devices.filter(status="active")):
        cfs = dict(obj.custom_fields)
        skip_ssl = bool(cfs.get("skip_ssl_verify"))
        auth_type = (cfs.get("auth_type") or "").lower().strip()
        active = auth_type != "cloudflare"
        monitors_scope = (cfs.get("monitors") or "both").lower().strip()
        if monitors_scope == "none":
            continue

        basic_user = basic_pass = ""
        if auth_type == "basic" and infisical and infisical_cfg:
            basic_user = _infisical_secret(infisical, infisical_cfg, obj.name, "LOGIN")
            basic_pass = _infisical_secret(infisical, infisical_cfg, obj.name, "PASSWORD")
            if not basic_user:
                auth_type = "none"

        for label, url in (("external", cfs.get("external_url")), ("internal", cfs.get("internal_url"))):
            if not url:
                continue
            if monitors_scope == "internal" and label == "external":
                continue
            if monitors_scope == "external" and label == "internal":
                continue
            if label == "external" and "averr.in" not in url:
                continue

            name = f"{obj.name} [{label}]"
            if name in desired:
                continue  # duplicate NetBox object name — skip
            desired.add(name)
            config_str = _http_config(url, skip_ssl=skip_ssl, auth_type=auth_type,
                                      basic_user=basic_user, basic_pass=basic_pass)

            if name in existing:
                ex = existing[name]
                old_cfg = json.loads(ex["config"]) if ex["config"] else {}
                new_cfg = json.loads(config_str)
                diff = {}
                if old_cfg.get("url") != new_cfg.get("url"):
                    diff["url"] = {"old": old_cfg.get("url"), "new": new_cfg.get("url")}
                if old_cfg.get("authMethod") != new_cfg.get("authMethod"):
                    diff["auth"] = {"old": old_cfg.get("authMethod"), "new": new_cfg.get("authMethod")}
                if old_cfg.get("ignore_tls_errors") != new_cfg.get("ignore_tls_errors"):
                    diff["ignore_tls_errors"] = {"old": old_cfg.get("ignore_tls_errors"), "new": new_cfg.get("ignore_tls_errors")}
                if ex["active"] != active:
                    diff["active"] = {"old": ex["active"], "new": active}

                if not diff:
                    _print("skip", name)
                    stats["skip"] += 1
                    continue

                err = _upsert(base, headers, name, config_str, active, monitor_id=ex["id"])
                _print("update", name, details=diff, error=err)
                stats["update"] += 1
            else:
                details = {"url": url, "auth": auth_type or "none", "active": active}
                err = _upsert(base, headers, name, config_str, active)
                _print("create", name, details=details, error=err)
                stats["create"] += 1

    # Delete monitors no longer in desired state
    for name, ex in existing.items():
        if name not in desired:
            err = _delete_monitor(base, headers, ex["id"])
            _print("delete", name, error=err)
            stats["delete"] += 1

    print(
        f"\n{_c('bold')}Peekaping{_c('reset')} sync complete: "
        f"{_c('green')}{stats['create']} create{_c('reset')}, "
        f"{_c('yellow')}{stats['update']} update{_c('reset')}, "
        f"{_c('dim')}{stats['skip']} skip{_c('reset')}, "
        f"{_c('yellow')}{stats['delete']} delete{_c('reset')}"
    )


# ── Peekaping API ─────────────────────────────────────────────────────────────

def _load_monitors(base: str, headers: dict) -> dict[str, dict]:
    result = {}
    page = 0
    limit = 50
    while True:
        resp = requests.get(f"{base}/monitors", headers=headers, params={"page": page, "limit": limit}, verify=False)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        for m in data:
            result[m["name"]] = {
                "id": m["id"],
                "config": m.get("config") or "",
                "active": m.get("active", True),
            }
        if len(data) < limit:
            break
        page += 1
    return result


def _delete_monitor(base: str, headers: dict, monitor_id: str) -> str | None:
    resp = requests.delete(f"{base}/monitors/{monitor_id}", headers=headers, verify=False)
    if not resp.ok:
        return f"{resp.status_code} {resp.text[:120]}"
    return None


def _upsert(base: str, headers: dict, name: str, config_str: str, active: bool, monitor_id: str | None = None) -> str | None:
    payload = {
        "name": name,
        "type": "http",
        "active": active,
        "interval": _DEFAULT_INTERVAL,
        "timeout": _DEFAULT_TIMEOUT,
        "max_retries": _DEFAULT_MAX_RETRIES,
        "retry_interval": _DEFAULT_INTERVAL,
        "resend_interval": 3,
        "notification_ids": [_NOTIFIER_ID],
        "config": config_str,
    }
    if monitor_id:
        resp = requests.put(f"{base}/monitors/{monitor_id}", headers=headers, json=payload, verify=False)
    else:
        resp = requests.post(f"{base}/monitors", headers=headers, json=payload, verify=False)
    if not resp.ok:
        return f"{resp.status_code} {resp.text[:120]}"
    return None


def _http_config(url: str, skip_ssl: bool = False, auth_type: str = "",
                 basic_user: str = "", basic_pass: str = "") -> str:
    config: dict = {
        "url": url,
        "method": "GET",
        "accepted_statuscodes": ["2XX"],
        "max_redirects": 10,
        "ignore_tls_errors": skip_ssl,
        "authMethod": "basic" if auth_type == "basic" else "none",
        "check_cert_expiry": False,
        "encoding": "json",
    }
    if auth_type == "basic":
        config["basic_auth_user"] = basic_user
        config["basic_auth_pass"] = basic_pass
    return json.dumps(config, sort_keys=True)


# ── Infisical helpers ─────────────────────────────────────────────────────────

def _init_infisical(cfg: InfisicalConfig | None):
    if not cfg or not cfg.is_configured:
        return None
    try:
        from infisical_sdk import InfisicalSDKClient
        client = InfisicalSDKClient(host=cfg.url.rstrip("/"))
        client.auth.universal_auth.login(client_id=cfg.client_id, client_secret=cfg.client_secret)
        return client
    except Exception as e:
        print(f"  WARNING: Infisical init failed: {e}")
        return None


def _infisical_secret(client, cfg: InfisicalConfig, vm_name: str, key: str) -> str:
    try:
        secret = client.secrets.get_secret_by_name(
            secret_name=key,
            project_id=cfg.project_id,
            environment_slug=cfg.environment,
            secret_path=f"/vms/{vm_name}",
        )
        return secret.secretValue or ""
    except Exception:
        return ""
