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
from cos.core.spec import EnvSpec, WorkloadSpec, is_forbidden_host_path, is_user_network

_STDIN_PATH = "/tmp/cos_stdin"

# Non-breaking safety defaults applied to every container. These kill the
# fork-bomb / OOM / setuid-escalation vectors without dropping capabilities
# (which breaks stock images — cap-drop + read-only rootfs stay a future
# opt-in `hardened` profile). A spec's own limits override the resource caps.
_DEFAULT_PIDS_LIMIT = 512
_DEFAULT_MEM_LIMIT = "2g"      # generous; raise per-workload via spec.limits.memory
_DEFAULT_CPUS = 2.0           # raise per-workload via spec.limits.cpus


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
        if is_forbidden_host_path(context):
            raise SpecError(f"build context {context!r} is a host-sensitive path "
                            f"(would tar the host); use a specific project dir")
        tag = "cos-build:" + _hash(context + (dockerfile or ""))
        try:
            self.client.images.build(path=context, dockerfile=dockerfile, tag=tag, rm=True,
                                     labels={L.MANAGED: "true"})
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
            self.client.images.build(fileobj=io.BytesIO(dockerfile.encode()), tag=tag, rm=True,
                                     labels={L.MANAGED: "true"})
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"base+provision build failed: {exc}") from exc
        return tag

    # ── one-shot jobs ────────────────────────────────────────────────────────

    def run_job(self, spec: WorkloadSpec) -> JobResult:
        spec.validate()
        image = self.resolve_image(spec.env)
        if is_user_network(spec.network):
            self.ensure_network(spec.network)
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
                self._verify_running(existing, spec.name)
            return Handle(id=existing.id, name=spec.name, status="running")
        image = self.resolve_image(spec.env)
        if is_user_network(spec.network):
            self.ensure_network(spec.network)
        lbls = L.build(lifecycle="persistent", name=spec.name, owner=spec.owner,
                       purpose=spec.purpose, ttl_seconds=spec.ttl_seconds)
        kwargs = self._create_kwargs(spec, image, lbls)
        container = self.client.containers.run(**{**kwargs, "detach": True})
        self._verify_running(container, spec.name)
        return Handle(id=container.id, name=spec.name, status="running")

    def _verify_running(self, container: Any, name: str) -> None:
        """Confirm the container is actually up — catch immediate/short crashes
        and surface the exit code + logs instead of falsely reporting 'running'."""
        for delay in (0.0, 1.0):
            if delay:
                time.sleep(delay)
            try:
                container.reload()
            except Exception:  # noqa: BLE001
                break
            if container.status == "running":
                if delay:  # survived the second check
                    return
                continue  # running on first look; re-check after a beat
            # not running → it crashed. Capture why, clean up, raise.
            code = (container.attrs.get("State") or {}).get("ExitCode")
            logs = _combined_logs(container)
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
            raise BackendError(
                f"container {name!r} exited immediately (code {code}). "
                f"logs:\n{logs[:1500]}"
            )

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

    # ── networks (inter-container communication) ──────────────────────────────

    def ensure_network(self, name: str) -> str:
        """Find-or-create a user-defined bridge network. Containers sharing it
        resolve each other by container name (Docker's embedded DNS)."""
        existing = self.client.networks.list(names=[name])
        if existing:
            return existing[0].id
        try:
            net = self.client.networks.create(
                name, driver="bridge", check_duplicate=True,
                labels={L.MANAGED: "true"})
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not create network {name!r}: {exc}") from exc
        return net.id

    def remove_network(self, name: str) -> None:
        nets = self.client.networks.list(names=[name])
        if not nets:
            raise NotFoundError(f"no network named {name!r}")
        try:
            nets[0].remove()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not remove network {name!r}: {exc}") from exc

    def list_networks(self) -> list[dict]:
        out = []
        for n in self.client.networks.list(filters={"label": f"{L.MANAGED}=true"}):
            n.reload()
            members = [c.get("Name", "?") for c in (n.attrs.get("Containers") or {}).values()]
            out.append({"name": n.name, "id": n.id[:12], "containers": members})
        return out

    # ── images (build-once, run-many) ─────────────────────────────────────────

    def build_image(
        self, tag: str, *, context: str | None = None, dockerfile: str | None = None,
        dockerfile_inline: str | None = None, base: str | None = None,
        provision: tuple[str, ...] = (), owner: str = "",
    ) -> dict:
        """Build a named, reusable image labeled cos.managed.

        The build-once-run-many primitive: build a tagged image, then reference
        it from many container_run/ensure calls (`image=<tag>`). Exactly one
        source: `context` (a build dir), `dockerfile_inline` (a Dockerfile
        string), or `base`+`provision` (FROM base; RUN steps). Labeling makes
        the image visible to list_images and reclaimable by gc.
        """
        sources = [context is not None, dockerfile_inline is not None, base is not None]
        if sum(sources) != 1:
            raise SpecError(
                "build_image needs exactly one of: context, dockerfile_inline, base(+provision)")
        if context is not None and is_forbidden_host_path(context):
            raise SpecError(f"build context {context!r} is a host-sensitive path "
                            f"(would tar the host); use a specific project dir")
        labels = {L.MANAGED: "true", L.NAME: tag, L.OWNER: owner,
                  L.CREATED: str(int(time.time()))}
        try:
            if context is not None:
                img, _ = self.client.images.build(
                    path=context, dockerfile=dockerfile, tag=tag, rm=True, labels=labels)
            else:
                if base is not None:
                    df = "\n".join([f"FROM {base}"] + [f"RUN {s}" for s in provision]) + "\n"
                else:
                    df = dockerfile_inline
                img, _ = self.client.images.build(
                    fileobj=io.BytesIO(df.encode()), tag=tag, rm=True, labels=labels)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"build_image {tag!r} failed: {exc}") from exc
        return {"tag": tag, "id": img.id[:19], "size_mb": round(img.attrs.get("Size", 0) / 1e6, 1)}

    def remove_image(self, tag: str, force: bool = False) -> None:
        try:
            self.client.images.get(tag)
        except Exception:  # noqa: BLE001
            raise NotFoundError(f"no image named {tag!r}")
        try:
            self.client.images.remove(tag, force=force)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"could not remove image {tag!r}: {exc}") from exc

    def list_images(self) -> list[dict]:
        out = []
        for img in self.client.images.list(filters={"label": f"{L.MANAGED}=true"}):
            out.append({"tags": img.tags or ["<none>"], "id": img.id[:19],
                        "size_mb": round(img.attrs.get("Size", 0) / 1e6, 1)})
        return out

    # ── garbage collection ────────────────────────────────────────────────────

    def prune_containers(self) -> list[str]:
        """Remove STOPPED managed containers (any lifecycle). Leaves running ones."""
        removed = []
        for c in self.client.containers.list(all=True, filters=L.managed_filter()):
            if c.status in ("running", "restarting", "paused"):
                continue
            try:
                c.remove(force=True)
                removed.append(c.name)
            except Exception:  # noqa: BLE001
                pass
        return removed

    def prune_networks(self) -> list[str]:
        """Remove managed networks with no attached containers."""
        removed = []
        for n in self.client.networks.list(filters={"label": f"{L.MANAGED}=true"}):
            n.reload()
            if n.attrs.get("Containers"):
                continue
            try:
                n.remove()
                removed.append(n.name)
            except Exception:  # noqa: BLE001
                pass
        return removed

    def prune_images(self, only_unused: bool = True) -> list[str]:
        """Remove the managed image CACHE (cos-gen/cos-build) not backing any
        container.

        Named `image_build` images (they carry a cos.name label) are the
        build-once-run-many artifacts and are NEVER pruned here — reclaim those
        deliberately via `remove_image`. gc only reclaims the disposable cache.
        """
        in_use = set()
        if only_unused:
            for c in self.client.containers.list(all=True):
                iid = (c.attrs.get("Image") or "")
                if iid:
                    in_use.add(iid)
        candidates: dict[str, Any] = {}
        for img in self.client.images.list(filters={"label": f"{L.MANAGED}=true"}):
            if (img.labels or {}).get(L.NAME):
                continue  # intentional build-once image — not cache; skip
            candidates[img.id] = img
        # backward-compat: our synthetic cache tags, even if built unlabeled.
        for img in self.client.images.list():
            if any(t.startswith(("cos-gen:", "cos-build:")) for t in (img.tags or [])):
                candidates[img.id] = img
        removed = []
        for iid, img in candidates.items():
            if only_unused and iid in in_use:
                continue
            label = (img.tags or [iid[:19]])[0]
            try:
                self.client.images.remove(img.id, force=True)
                removed.append(label)
            except Exception:  # noqa: BLE001 — shared layer / concurrent use
                pass
        return removed

    def gc(self) -> dict:
        """Reclaim managed cruft: stopped containers, empty networks, unused
        images. Order matters — containers free image + network references."""
        containers = self.prune_containers()
        networks = self.prune_networks()
        images = self.prune_images(only_unused=True)
        return {"containers": containers, "networks": networks, "images": images}

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
            # Always-on, non-breaking hardening.
            "pids_limit": _DEFAULT_PIDS_LIMIT,          # kills fork bombs
            "security_opt": ["no-new-privileges"],      # kills setuid escalation
        }
        if spec.ports:
            # Publish to loopback only (sandbox-first): host 127.0.0.1:<host> -> container.
            kwargs["ports"] = {
                f"{p.container}/{p.protocol}": ("127.0.0.1", p.host) for p in spec.ports
            }
        # Resource caps: spec value wins; otherwise a default (never unlimited).
        kwargs["mem_limit"] = spec.limits.memory or _DEFAULT_MEM_LIMIT
        cpus = spec.limits.cpus or _DEFAULT_CPUS
        kwargs["nano_cpus"] = int(cpus * 1_000_000_000)
        if spec.lifecycle == "persistent" and spec.name:
            kwargs["name"] = f"cos-{spec.name}"
        return {k: v for k, v in kwargs.items() if v is not None}

    def _find(self, name: str) -> Any:
        # Scope to managed: exec/logs/stop/rm must never act on a look-alike
        # container that merely carries a matching cos.name label.
        conts = self.client.containers.list(
            all=True, filters={"label": [f"{L.MANAGED}=true", f"{L.NAME}={name}"]})
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


def _combined_logs(container: Any) -> str:
    try:
        raw = container.logs(stdout=True, stderr=True)
        return raw.decode("utf-8", "replace") if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def _logs(container: Any, *, stdout: bool, stderr: bool) -> str:
    try:
        raw = container.logs(stdout=stdout, stderr=stderr)
        return raw.decode("utf-8", "replace") if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "readtimeout" in name
