"""The MCP server front end — `container_*` tools over the DockerBackend.

Any MCP client (arc, etc.) connects over streamable-HTTP and drives containers.
Thin: each tool builds a WorkloadSpec (validated) and calls the backend; errors
propagate as MCP tool errors.
"""
from __future__ import annotations

from typing import Any

from cos.core.backend import DockerBackend, JobResult
from cos.core.errors import CosError
from cos.core.spec import WorkloadSpec


def _fmt_job(res: JobResult) -> str:
    parts = [f"exit={res.exit_code} ({res.duration_s}s)"]
    if res.stdout.strip():
        parts.append("stdout:\n" + res.stdout.rstrip())
    if res.stderr.strip():
        parts.append("stderr:\n" + res.stderr.rstrip())
    return "\n".join(parts)


def _spec_dict(
    *, image, base, provision, build_context, command, stdin, env, mounts, ports,
    network, cpus, memory, timeout_seconds, lifecycle="ephemeral", name=None,
    owner="", ttl_seconds=None,
) -> dict:
    d: dict[str, Any] = {
        "command": command, "stdin": stdin, "env": env or {}, "mounts": mounts or [],
        "ports": ports or [], "network": network, "lifecycle": lifecycle, "owner": owner,
        "timeout_seconds": timeout_seconds,
    }
    if image:
        d["image"] = image
    if base:
        d["base"] = base
        if provision:
            d["provision"] = provision
    if build_context:
        d["build"] = {"context": build_context}
    if cpus is not None or memory is not None:
        d["limits"] = {"cpus": cpus, "memory": memory}
    if name:
        d["name"] = name
    if ttl_seconds is not None:
        d["ttl_seconds"] = ttl_seconds
    return d


def build_server(host: str = "127.0.0.1", port: int = 8770, backend: DockerBackend | None = None):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("container-orchestration", host=host, port=port)
    be = backend or DockerBackend()

    @mcp.tool()
    def container_run(
        image: str | None = None,
        command: str | None = None,
        base: str | None = None,
        provision: list[str] | None = None,
        build_context: str | None = None,
        stdin: str | None = None,
        env: dict | None = None,
        mounts: list[dict] | None = None,
        ports: list[str] | None = None,
        network: str = "none",
        cpus: float | None = None,
        memory: str | None = None,
        timeout_seconds: int = 300,
    ) -> str:
        """Run a one-shot container job and return its exit code + output.

        Provide exactly one image source: `image` (pull), `base`+`provision`
        (a base image plus setup commands), or `build_context` (a build dir).
        `command` is a shell string. `mounts` is a list of
        {source, target, read_only?, type?}. `ports` publishes to the host as
        "host:container" strings (requires network='bridge'). `network` defaults
        to 'none' (sandboxed). The job auto-removes when it finishes.
        """
        try:
            spec = WorkloadSpec.from_dict(_spec_dict(
                image=image, base=base, provision=provision, build_context=build_context,
                command=command, stdin=stdin, env=env, mounts=mounts, ports=ports,
                network=network, cpus=cpus, memory=memory, timeout_seconds=timeout_seconds))
            return _fmt_job(be.run_job(spec))
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_ensure(
        name: str,
        image: str | None = None,
        command: str | None = None,
        base: str | None = None,
        provision: list[str] | None = None,
        env: dict | None = None,
        mounts: list[dict] | None = None,
        ports: list[str] | None = None,
        network: str = "bridge",
        cpus: float | None = None,
        memory: str | None = None,
    ) -> str:
        """Find-or-create a persistent, named container (a long-lived service).
        Idempotent: returns the existing one if present, else starts + verifies it.

        To reach a server from the host, publish `ports` as "host:container"
        strings (e.g. ["50505:8000"]) — this needs network='bridge' (the default
        here). Raises with the container's logs if it crashes on start.
        """
        try:
            spec = WorkloadSpec.from_dict(_spec_dict(
                image=image, base=base, provision=provision, build_context=None,
                command=command, stdin=None, env=env, mounts=mounts, ports=ports,
                network=network, cpus=cpus, memory=memory, timeout_seconds=None,
                lifecycle="persistent", name=name))
            h = be.ensure_env(spec)
            pub = f" ports {[f'{p.host}->{p.container}' for p in spec.ports]}" if spec.ports else ""
            return f"{h.name}: {h.status} ({h.id[:12]}){pub}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_exec(name: str, command: str) -> str:
        """Run a command inside a running persistent container."""
        try:
            import shlex
            r = be.exec(name, shlex.split(command))
            return f"exit={r['exit_code']}\n{r['output'].rstrip()}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_logs(name: str, tail: int | None = None) -> str:
        """Return logs from a managed container."""
        try:
            return be.logs(name, tail=tail) or "(no output)"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_stop(name: str) -> str:
        """Stop a persistent container."""
        try:
            be.stop(name)
            return f"stopped {name}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_rm(name: str) -> str:
        """Remove a managed container."""
        try:
            be.rm(name)
            return f"removed {name}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def container_list() -> str:
        """List all containers this service manages."""
        rows = be.list()
        if not rows:
            return "(no managed containers)"
        return "\n".join(
            f"{c.name:<24} {c.lifecycle:<10} {c.status:<10} {c.image}" for c in rows)

    @mcp.tool()
    def container_reap(only_expired: bool = True) -> str:
        """Remove ephemeral managed containers (expired ones by default)."""
        reaped = be.reap(only_expired=only_expired)
        return f"reaped {len(reaped)}: {', '.join(reaped)}" if reaped else "nothing to reap"

    return mcp
