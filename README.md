# container-orchestration-service (`cos`)

A small, **harness-agnostic** Docker control plane. It runs *workloads* —
one-shot **jobs** and long-lived **services** — on the local Docker daemon, with
arc-agnostic ownership, lifecycle, and reaping layered on top. All state lives
in container labels (`cos.managed=true`), so there's no sidecar database.

Its primary front end is an **MCP server**: any MCP client (e.g. the arc agent
runtime) connects over streamable-HTTP and gets `container_*` tools. The same
core library is usable directly from Python and from the `cos` CLI.

Designed against `agent-runtime/v2/_design/0024-container-orchestration-and-job-dispatch.md`.

## Why

Some capabilities can't (or shouldn't) run in a host process — heavy engines
with hostile installs. The answer is "the environment is a dependency": ship a
recipe (an image), and dispatch a job into a container. This service is the
thing that runs those containers.

## ⚠️ Security / trust model — read before exposing this

cos drives the **Docker daemon, which is root-equivalent on a standard install**.
Treat it accordingly.

**Hardened (2026-07):**
- **Bind-mount sources are validated** — `/var/run/docker.sock`, `/`, `/proc`,
  `/sys`, `/dev`, `/etc`, `/var/run` (and symlink/`..` tricks) are rejected, so
  the mount-the-socket host-escape is closed.
- **Every container gets `pids_limit`, `no-new-privileges`, and default
  cpu/mem caps** (a spec's own limits override) — fork-bomb / OOM / setuid-
  escalation are shut off without breaking stock images.
- `network=none` default, loopback-only port publishing, `host`/`container:*`
  network modes rejected, correctly-quoted command/stdin (no shell injection).

**Still open — know these:**
- **The MCP server is unauthenticated.** Anything that can reach
  `127.0.0.1:8770` — any local process, and (absent an Origin/Host check) a
  malicious web page via DNS-rebinding — can drive Docker. **Never bind it to
  anything but loopback, and never expose the port.** It is a control plane for
  a **single trusted local user**, not a multi-tenant service.
- **Not yet a full sandbox for untrusted code.** `cap_drop=ALL` + read-only
  rootfs + a workspace mount *allowlist* remain a future opt-in `hardened`
  profile. The catastrophic vectors are closed; for genuinely untrusted binaries
  wait for that profile. Full analysis + mitigation log:
  `_code_review/02-security-audit.md` and `_mitigation/` in the agent-runtime repo.

## Concepts

- **EnvSpec** — how to obtain the image: `image` (pull), `build` (a context), or
  `base + provision` (a base image + setup steps, synthesized into a Dockerfile).
- **WorkloadSpec** — env + command + stdin + mounts + env vars + limits +
  network (**default `none`**) + lifecycle (`ephemeral` | `persistent`).
- **Jobs** run once, return `{exit_code, stdout, stderr}`, and auto-remove.
- **Services** are persistent, named, and reconnected by label (find-or-create).
- **Networks** — put cooperating containers on a user-defined network and they
  reach each other by name over Docker's embedded DNS. A persistent container
  named `X` is reachable at hostname `cos-X`. `none` (sandbox) and `bridge`
  (host-reachable, no inter-container DNS) remain the built-in modes; `host` and
  `container:*` are rejected.
- **Images** — `build_image` builds a named, `cos.managed`-labeled image ONCE
  (from a context dir, an inline Dockerfile, or base+provision); reference it
  from many containers via `image=<tag>` (build-once, run-many). `image_list` /
  `image_remove` manage them.
- **GC** — `gc` reclaims managed cruft: stopped containers, empty networks, and
  images not backing any container. Never touches running containers or
  unmanaged resources. All builds (including the base+provision cache) are
  labeled managed, so nothing accumulates unreclaimably.

## Multi-container example

```bash
cos network create appnet
cos run python:3.11-slim --network appnet --cmd "python -m http.server 8000"  # (as a service via MCP container_ensure)
# a second container on appnet reaches the first at http://cos-<name>:8000
cos network ls
```

Over MCP: `network_create` / `network_list` / `network_remove`, plus
`network=<name>` on `container_run` / `container_ensure`.

## Build once, run many + clean up

```bash
cos image build myapp:latest --context ./myapp   # build + label the image once
cos run myapp:latest --cmd "..."                 # reference it by tag, N times
cos image ls
cos gc                                           # reclaim stopped/empty/unused
```

Over MCP: `image_build` / `image_list` / `image_remove` and `gc`.

## Quick start

```bash
pip install -e ".[mcp]"          # core + MCP server
cos ping                         # check the daemon
cos run alpine:3.19 --cmd "echo hello"
cos serve --port 8770            # run the MCP server (streamable-HTTP)
```

Point an MCP client at it (arc example):

```bash
arc mcp add container --transport http --url http://127.0.0.1:8770/mcp
```

## Status

v1: core library + Docker backend + MCP server + CLI. Native REST API + a
programmatic Python client are deferred until an engine dispatcher needs a
non-MCP path (see `_deviations/`).

## License

MIT.
