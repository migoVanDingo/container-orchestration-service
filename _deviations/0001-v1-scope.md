# Deviations — v1 vs design 0024

Where this implementation departs from
`agent-runtime/v2/_design/0024-container-orchestration-and-job-dispatch.md`.
For review.

## D1 — MCP-server-first; native REST API + Python client deferred

**Plan:** the service exposes a native HTTP/gRPC API + a Python client for
programmatic engine dispatchers, with MCP as the model-facing seam on top.

**Built:** v1 ships the **core library + Docker backend + MCP server + CLI**.
The MCP server (streamable-HTTP) is the front end; the core library is importable
directly. The separate **native REST API and Python client are deferred** until
an engine dispatcher (e.g. the angr plugin) needs a non-MCP path.

**Why:** the only consumer right now is arc, via the MCP client we just built
(0025) — so the MCP server is the end-to-end-testable surface today. The core
(`cos.core`) is transport-agnostic, so a FastAPI REST layer + client slot in
later without touching it. (Agreed with the user: "go, A, public".)

## D2 — stdin injection via `put_archive` + shell redirect (not socket attach)

**Plan (implied):** feed a job's payload via stdin.

**Built:** the first cut used docker-py `attach_socket` to write stdin — it hung
(`cat` never received EOF on macOS Docker Desktop). Replaced with a robust path:
copy the payload into the created container with `put_archive` to
`/tmp/cos_stdin`, and wrap the command as `sh -c '<command> < /tmp/cos_stdin'`.
No socket attach, no host file-sharing.

**Consequence:** `stdin` needs `/bin/sh` in the image and a `command` to redirect
into (raises `SpecError` otherwise). Fine for the job-dispatch use (engine images
have a shell). Env vars and mounts are the other injection channels.

## D3 — Security defaults: network + limits now; caps/read-only-rootfs deferred

**Plan:** sandbox-first — `network=none` default, resource limits, drop caps,
read-only rootfs where feasible.

**Built:** `network=none` default and cpu/mem limits are in. Dropping all
capabilities and read-only rootfs are **not** forced in v1 — they break many
stock images, so they'll be an opt-in `hardened` mode later. Non-privileged is
still the default (we never set `privileged`).

## D5 — Port publishing + verify-on-start (added after live testing)

0024 listed mounts but not published ports, and `ensure_env` originally reported
"running" optimistically. Live use (run a webserver, curl it) exposed both gaps:

- **Port publishing** — `WorkloadSpec.ports` (`PortMap`) maps container ports to
  **127.0.0.1** host ports (loopback-only, sandbox-first); requires
  `network='bridge'`. Surfaced in `container_run`/`container_ensure` (`ports`
  as `"host:container"` strings) and `cos run --publish`.
- **Verify-on-start** — `ensure_env` now reloads and confirms the container
  stayed up; on an immediate crash it raises with the **exit code + logs** rather
  than falsely reporting "running". (Bonus: the agent now sees a bad command's
  error instead of a phantom running server.)

## D4 — Labels-as-state (as designed), no reaper daemon yet

State lives entirely in container labels (`cos.managed`, `cos.lifecycle`,
`cos.owner`, `cos.ttl`, `cos.created`) per the plan. Reaping is **on-demand**
(`cos reap` / `container_reap`), not a background sweeper — a periodic reaper can
be added, but on-demand covers v1 (and TTL is recorded for it).
