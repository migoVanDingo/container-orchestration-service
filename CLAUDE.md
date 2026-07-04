# container-orchestration-service (`cos`) — developer guide

A standalone, harness-agnostic Docker control plane with an MCP server front end.
Built against `agent-runtime/v2/_design/0024-container-orchestration-and-job-dispatch.md`.
NOT part of the arc repo — arc consumes it as an external MCP server.

## Code map

```
src/cos/
  core/
    spec.py      EnvSpec (image | build | base+provision), WorkloadSpec, Mount,
                 Limits — the data model + validation + from_dict (the wire contract)
    labels.py    cos.* label constants — the ONLY state (docker ps is the DB)
    backend.py   DockerBackend over docker-py: resolve_image, run_job, ensure_env,
                 exec, logs, stop, rm, list, reap
    errors.py    CosError hierarchy
  mcp_server/
    server.py    FastMCP server: container_run/ensure/exec/logs/stop/rm/list/reap
  cli/
    main.py      `cos ping|run|ps|logs|exec|stop|rm|reap|serve`
tests/
  test_spec.py            unit (no docker)
  test_backend_live.py    live vs the daemon (skips if unreachable)
  test_mcp_server_live.py end-to-end: `cos serve` driven by a real MCP client
```

## The model

- **EnvSpec** — how to get the image: `image` (pull), `build` (a context), or
  `base + provision` (base + RUN steps → synthesized Dockerfile, cached by hash).
  Exactly one is set.
- **WorkloadSpec** — env + command + stdin + mounts + env_vars + limits +
  `network` (**default `none`**) + `lifecycle` (`ephemeral` | `persistent`).
- **run_job** → one-shot, returns `{exit_code, stdout, stderr}`, auto-removes.
- **ensure_env** → persistent find-or-create by `cos.name` label (idempotent).
- **ensure_network / remove_network / list_networks** — user-defined bridge
  networks (labeled `cos.managed=true`) for inter-container DNS. `run_job` /
  `ensure_env` auto-create the network when `spec.network` isn't `none`/`bridge`.

## Non-obvious decisions

- **State is container labels, not a DB.** `docker ps --filter
  label=cos.managed=true` reconstructs everything after a restart. See `labels.py`.
- **The MCP server is the primary front end** (streamable-HTTP). The core library
  is transport-agnostic — a native REST API + client come when an engine
  dispatcher needs a non-MCP path. See `_deviations/0001`.
- **stdin = `put_archive` + `sh -c '<cmd> < /tmp/cos_stdin'`**, NOT socket attach
  (which hung on macOS). Needs a shell + a command. See `_deviations/0002`... (D2).
- **`network=none` + limits are the sandbox defaults.** Cap-drop / read-only
  rootfs are deferred (break stock images) — future `hardened` opt-in.
- **A user network name is passed straight to docker-py as `network_mode`.** Any
  value that isn't `none`/`bridge` (and passes the name regex) is a user network;
  `host` and `container:*` are rejected as sandbox escapes. Inter-container name
  resolution only works on user networks, not the default `bridge`.
- **base+provision images are cached** by content hash (`cos-gen:<hash>`); rebuild
  only on change.
- **`mcp` is an optional extra** — the core + CLI work without it; only `cos serve`
  needs it.

## Build + test

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # spec unit always; live tests skip w/o Docker
.venv/bin/ruff check src tests
```

Live tests need the Docker daemon (they pull `alpine:3.19`). They're the real
validation — the backend and MCP server are exercised against a real daemon.

## How arc consumes it

```bash
cos serve --port 8770
arc mcp add container --transport http --url http://127.0.0.1:8770/mcp
```
Then the agent gets `container_*` tools (gated + observable via arc's MCP client).

## Conventions

- Use Edit/Write, not bash heredocs. WHY-only comments. No emojis in code/commits.
- Tools/CLI raise `CosError` subclasses; the MCP server maps them to tool errors.
- New backend op → method on `DockerBackend` + a `container_*` MCP tool + a `cos`
  subcommand + a live test.
