"""Container labels are the control plane's state.

Every container this service creates is tagged so `docker ps --filter
label=cos.managed=true` reconstructs "what's running" after any restart — no
sidecar database. All orchestration metadata (owner, lifecycle, ttl, purpose)
lives here.
"""
from __future__ import annotations

import time

MANAGED = "cos.managed"          # "true" on everything we create
OWNER = "cos.owner"              # who asked for it (session/agent/caller id)
LIFECYCLE = "cos.lifecycle"     # "ephemeral" | "persistent"
NAME = "cos.name"               # logical name (persistent find-or-create key)
PURPOSE = "cos.purpose"         # free-text intent
CREATED = "cos.created"         # unix seconds (string)
TTL = "cos.ttl"                 # seconds after CREATED to reap (string; "" = none)


def build(
    *,
    lifecycle: str,
    name: str | None = None,
    owner: str = "",
    purpose: str = "",
    ttl_seconds: int | None = None,
    now: float | None = None,
    extra: dict | None = None,
) -> dict:
    created = int(now if now is not None else time.time())
    labels = {
        MANAGED: "true",
        LIFECYCLE: lifecycle,
        OWNER: owner,
        PURPOSE: purpose,
        CREATED: str(created),
        TTL: str(ttl_seconds) if ttl_seconds else "",
    }
    if name:
        labels[NAME] = name
    if extra:
        labels.update({str(k): str(v) for k, v in extra.items()})
    return labels


def managed_filter(owner: str | None = None, lifecycle: str | None = None) -> dict:
    """docker-py `filters=` dict selecting our managed containers."""
    wanted = [f"{MANAGED}=true"]
    if owner:
        wanted.append(f"{OWNER}={owner}")
    if lifecycle:
        wanted.append(f"{LIFECYCLE}={lifecycle}")
    return {"label": wanted}


def is_expired(labels: dict, now: float | None = None) -> bool:
    ttl = labels.get(TTL) or ""
    if not ttl:
        return False
    try:
        created = int(labels.get(CREATED, "0"))
        return (now if now is not None else time.time()) - created > int(ttl)
    except (ValueError, TypeError):
        return False
