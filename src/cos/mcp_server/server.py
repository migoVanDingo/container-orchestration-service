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

    @mcp.tool()
    def network_create(name: str) -> str:
        """Create (or find) a user-defined network so multiple containers can talk.

        Put cooperating containers on the same network (pass `network=<name>` to
        container_ensure/run). They then reach each other by container name — a
        persistent container named X is reachable at hostname 'cos-X'.
        """
        try:
            be.ensure_network(name)
            return f"network {name} ready (reach members at hostname cos-<name>)"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def network_remove(name: str) -> str:
        """Remove a user-defined network (detach containers first)."""
        try:
            be.remove_network(name)
            return f"removed network {name}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def network_list() -> str:
        """List managed user-defined networks and their attached containers."""
        nets = be.list_networks()
        if not nets:
            return "(no managed networks)"
        return "\n".join(
            f"{n['name']:<20} [{', '.join(n['containers']) or 'empty'}]" for n in nets)

    @mcp.tool()
    def image_build(
        tag: str,
        context: str | None = None,
        dockerfile: str | None = None,
        dockerfile_inline: str | None = None,
        base: str | None = None,
        provision: list[str] | None = None,
    ) -> str:
        """Build a named, reusable image once — then run it many times.

        Prefer this over `docker build` in a shell: the image is labeled and
        managed, so `gc` can reclaim it later. Give exactly one source:
        `context` (a build dir, optional `dockerfile`), `dockerfile_inline`
        (a full Dockerfile as a string), or `base`+`provision` (FROM base; RUN
        steps). Then start containers with container_run/ensure `image=<tag>`
        instead of rebuilding per container.
        """
        try:
            info = be.build_image(
                tag, context=context, dockerfile=dockerfile,
                dockerfile_inline=dockerfile_inline, base=base,
                provision=tuple(provision or ()))
            return f"built {info['tag']} ({info['id']}, {info['size_mb']}MB)"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def image_remove(tag: str, force: bool = False) -> str:
        """Remove a managed image by tag. Prefer this over `docker rmi`."""
        try:
            be.remove_image(tag, force=force)
            return f"removed image {tag}"
        except CosError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool()
    def image_list() -> str:
        """List managed images (built via image_build or base+provision)."""
        imgs = be.list_images()
        if not imgs:
            return "(no managed images)"
        return "\n".join(f"{', '.join(i['tags']):<40} {i['size_mb']}MB" for i in imgs)

    @mcp.tool()
    def gc() -> str:
        """Reclaim managed cruft: stopped containers, empty networks, and images
        not backing any container. Safe — never touches running containers or
        unmanaged resources. Run after tearing a workload down."""
        r = be.gc()
        parts = []
        for kind in ("containers", "networks", "images"):
            if r[kind]:
                parts.append(f"{kind}: {', '.join(r[kind])}")
        return "reclaimed → " + ("; ".join(parts) if parts else "nothing to reclaim")

    return mcp
