"""DockerBackend — the control plane over docker-py.

Adds arc-agnostic ownership, lifecycle, labels-as-state, and reaping on top of
the Docker daemon. No sidecar DB: `docker ps --filter label=cos.managed=true`
is the source of truth.

Sandbox-first defaults live in the spec (network=none). Image is resolved three
ways (image | build | base+provision) before the container is created.
"""
from __future__ import annotations

import hashlib
import io
import shlex
import tarfile
import time
from dataclasses import dataclass
from typing import Any

from cos.core import labels as L
from cos.core.errors import BackendError, NotFoundError, SpecError, TimeoutError_
from cos.core.spec import EnvSpec, WorkloadSpec

_STDIN_PATH = "/tmp/cos_stdin"


@dataclass
class JobResult:
    exit_code: int
    stdout: str
    stderr: str
    container_id: str
    duration_s: float
    timed_out: bool = False


@dataclass
class Handle:
    id: str
    name: str
    status: str


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str
    lifecycle: str
    owner: str
    created: str


class DockerBackend:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import docker
            try:
                self._client = docker.from_env()
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"cannot reach the Docker daemon: {exc}") from exc
        return self._client

    def ping(self) -> dict:
        try:
            return self.client.version()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"docker ping failed: {exc}") from exc

    # ── image resolution ─────────────────────────────────────────────────────

    def resolve_image(self, env: EnvSpec) -> str:
        env.validate()
        if env.image is not None:
            return self._ensure_pulled(env.image)
        if env.build is not None:
            return self._build_context(env.build.context, env.build.dockerfile)
        return self._build_base_provision(env.base, env.provision)

    def _ensure_pulled(self, image: str) -> str:
        try:
            self.client.images.get(image)
        except Exception:  # noqa: BLE001 — not present locally; pull
            try:
                self.client.images.pull(image)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"could not pull image {image!r}: {exc}") from exc
        return image

    def _build_context(self, context: str, dockerfile: str | None) -> str:
        tag = "cos-build:" + _hash(context + (dockerfile or ""))
        try:
            self.client.images.build(path=context, dockerfile=dockerfile, tag=tag, rm=True)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"build failed for context {context!r}: {exc}") from exc
        return tag

    def _build_base_provision(self, base: str, provision: tuple[str, ...]) -> str:
        lines = [f"FROM {base}"] + [f"RUN {step}" for step in provision]
        dockerfile = "\n".join(lines) + "\n"
        tag = "cos-gen:" + _hash(dockerfile)
        try:
            self.client.images.get(tag)
            return tag  # cached
        except Exception:  # noqa: BLE001 — build it
            pass
        try:
            self.client.images.build(fileobj=io.BytesIO(dockerfile.encode()), tag=tag, rm=True)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"base+provision build failed: {exc}") from exc
        return tag

    # ── one-shot jobs ────────────────────────────────────────────────────────

    def run_job(self, spec: WorkloadSpec) -> JobResult:
        spec.validate()
        image = self.resolve_image(spec.env)
        lbls = L.build(lifecycle="ephemeral", owner=spec.owner, purpose=spec.purpose,
                       ttl_seconds=spec.ttl_seconds, name=spec.name)
        kwargs = self._create_kwargs(spec, image, lbls)
        container = self.client.containers.create(**kwargs)
        start = time.time()
        timed_out = False
        try:
            if spec.stdin is not None:
                _put_file(container, _STDIN_PATH, spec.stdin)
            container.start()
            try:
                result = container.wait(timeout=spec.timeout_seconds)
                exit_code = int(result.get("StatusCode", -1))
            except Exception as exc:  # noqa: BLE001 — wait timeout / daemon error
                timed_out = _is_timeout(exc)
                try:
                    container.kill()
                except Exception:  # noqa: BLE001
                    pass
                if not timed_out:
                    raise BackendError(f"waiting on job failed: {exc}") from exc
                exit_code = -1
            stdout = _logs(container, stdout=True, stderr=False)
            stderr = _logs(container, stdout=False, stderr=True)
        finally:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        if timed_out:
            raise TimeoutError_(
                f"job exceeded timeout of {spec.timeout_seconds}s (stdout so far: "
                f"{stdout[:200]!r})"
            )
        return JobResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                         container_id=container.id, duration_s=round(time.time() - start, 2))

    # ── persistent workloads ─────────────────────────────────────────────────

    def ensure_env(self, spec: WorkloadSpec) -> Handle:
        if spec.lifecycle != "persistent" or not spec.name:
            raise BackendError("ensure_env requires a persistent spec with a name")
        existing = self._find(spec.name)
        if existing is not None:
            if existing.status != "running":
                existing.start()
            return Handle(id=existing.id, name=spec.name, status="running")
        image = self.resolve_image(spec.env)
        lbls = L.build(lifecycle="persistent", name=spec.name, owner=spec.owner,
                       purpose=spec.purpose, ttl_seconds=spec.ttl_seconds)
        kwargs = self._create_kwargs(spec, image, lbls)
        container = self.client.containers.run(**{**kwargs, "detach": True})
        return Handle(id=container.id, name=spec.name, status="running")

    def exec(self, name: str, command: list[str] | str) -> dict:
        container = self._require(name)
        res = container.exec_run(command)
        out = res.output.decode("utf-8", "replace") if res.output else ""
        return {"exit_code": int(res.exit_code), "output": out}

    def logs(self, name: str, tail: int | None = None) -> str:
        container = self._require(name)
        raw = container.logs(tail=tail or "all")
        return raw.decode("utf-8", "replace") if raw else ""

    def stop(self, name: str) -> None:
        self._require(name).stop()

    def rm(self, name: str) -> None:
        self._require(name).remove(force=True)

    def list(self, owner: str | None = None, lifecycle: str | None = None) -> list[ContainerInfo]:
        conts = self.client.containers.list(
            all=True, filters=L.managed_filter(owner=owner, lifecycle=lifecycle))
        return [self._info(c) for c in conts]

    def reap(self, owner: str | None = None, only_expired: bool = True) -> list[str]:
        """Remove ephemeral managed containers (expired ones by default)."""
        reaped: list[str] = []
        for c in self.client.containers.list(
                all=True, filters=L.managed_filter(owner=owner, lifecycle="ephemeral")):
            lbls = c.labels or {}
            if only_expired and not L.is_expired(lbls):
                continue
            try:
                c.remove(force=True)
                reaped.append(c.name)
            except Exception:  # noqa: BLE001
                pass
        return reaped

    # ── internals ────────────────────────────────────────────────────────────

    def _create_kwargs(self, spec: WorkloadSpec, image: str, lbls: dict) -> dict:
        import docker

        mounts = [
            docker.types.Mount(target=m.target, source=m.source, type=m.type,
                               read_only=m.read_only)
            for m in spec.mounts
        ]
        command = list(spec.command) if spec.command else None
        if spec.stdin is not None:
            # Feed stdin via a file copied into the container (put_archive) + a
            # shell redirect — robust across platforms (no socket attach, no host
            # file sharing). Needs /bin/sh and a command to redirect into.
            if not command:
                raise SpecError("stdin requires a command to redirect into")
            command = ["/bin/sh", "-c", f"{shlex.join(command)} < {_STDIN_PATH}"]
        kwargs: dict = {
            "image": image,
            "command": command,
            "environment": spec.env_vars or None,
            "mounts": mounts or None,
            "network_mode": spec.network,
            "labels": lbls,
            "detach": True,
        }
        if spec.limits.memory:
            kwargs["mem_limit"] = spec.limits.memory
        if spec.limits.cpus:
            kwargs["nano_cpus"] = int(spec.limits.cpus * 1_000_000_000)
        if spec.lifecycle == "persistent" and spec.name:
            kwargs["name"] = f"cos-{spec.name}"
        return {k: v for k, v in kwargs.items() if v is not None}

    def _find(self, name: str) -> Any:
        conts = self.client.containers.list(all=True, filters={"label": f"{L.NAME}={name}"})
        return conts[0] if conts else None

    def _require(self, name: str) -> Any:
        c = self._find(name)
        if c is None:
            raise NotFoundError(f"no managed container named {name!r}")
        return c

    def _info(self, c: Any) -> ContainerInfo:
        lbls = c.labels or {}
        image = c.image.tags[0] if getattr(c, "image", None) and c.image.tags else "?"
        return ContainerInfo(
            id=c.id[:12], name=lbls.get(L.NAME, c.name), image=image, status=c.status,
            lifecycle=lbls.get(L.LIFECYCLE, "?"), owner=lbls.get(L.OWNER, ""),
            created=lbls.get(L.CREATED, ""),
        )


def _put_file(container: Any, path: str, content: str) -> None:
    """Copy `content` to `path` inside a created (not-yet-started) container."""
    import posixpath

    data = content.encode()
    directory, filename = posixpath.split(path)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    container.put_archive(directory or "/", stream.read())


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def _logs(container: Any, *, stdout: bool, stderr: bool) -> str:
    try:
        raw = container.logs(stdout=stdout, stderr=stderr)
        return raw.decode("utf-8", "replace") if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "readtimeout" in name
