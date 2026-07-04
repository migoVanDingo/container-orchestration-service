"""The workload data model — the single source of truth for what to run.

Tools, the CLI, and future clients all build a `WorkloadSpec`; the backend
consumes it. Two engine paths fall out of `EnvSpec`:
  - image      -> pull + run
  - build      -> build a context (Dockerfile) + run
  - base+prov  -> synthesize a Dockerfile (FROM base; RUN <provision>) + run
Exactly one of {image, build, base} is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cos.core.errors import SpecError

NETWORKS = ("none", "bridge")
LIFECYCLES = ("ephemeral", "persistent")


@dataclass(frozen=True)
class Mount:
    source: str                 # host path (bind) or volume name
    target: str                 # path inside the container
    read_only: bool = False
    type: str = "bind"          # "bind" | "volume"

    def validate(self) -> None:
        if self.type not in ("bind", "volume"):
            raise SpecError(f"mount.type must be bind|volume, got {self.type!r}")
        if not self.source or not self.target:
            raise SpecError("mount requires source and target")


@dataclass(frozen=True)
class Limits:
    cpus: float | None = None       # fractional cores, e.g. 1.5
    memory: str | None = None       # docker mem string, e.g. "512m", "2g"


@dataclass(frozen=True)
class BuildSpec:
    context: str                    # path to build context
    dockerfile: str | None = None   # relative to context; None = ./Dockerfile


@dataclass(frozen=True)
class EnvSpec:
    image: str | None = None
    build: BuildSpec | None = None
    base: str | None = None
    provision: tuple[str, ...] = ()

    def validate(self) -> None:
        set_count = sum(x is not None for x in (self.image, self.build, self.base))
        if set_count != 1:
            raise SpecError(
                "EnvSpec needs exactly one of {image, build, base}; "
                f"got {set_count}"
            )
        if self.base is None and self.provision:
            raise SpecError("`provision` only applies with `base`")


@dataclass(frozen=True)
class WorkloadSpec:
    env: EnvSpec
    command: tuple[str, ...] | None = None
    stdin: str | None = None
    mounts: tuple[Mount, ...] = ()
    env_vars: dict = field(default_factory=dict)
    network: str = "none"                 # sandbox-first default
    limits: Limits = field(default_factory=Limits)
    lifecycle: str = "ephemeral"
    name: str | None = None               # required for persistent
    owner: str = ""
    purpose: str = ""
    ttl_seconds: int | None = None
    timeout_seconds: int | None = 300

    def validate(self) -> None:
        self.env.validate()
        for m in self.mounts:
            m.validate()
        if self.network not in NETWORKS:
            raise SpecError(f"network must be one of {NETWORKS}, got {self.network!r}")
        if self.lifecycle not in LIFECYCLES:
            raise SpecError(f"lifecycle must be one of {LIFECYCLES}, got {self.lifecycle!r}")
        if self.lifecycle == "persistent" and not self.name:
            raise SpecError("persistent workloads require a `name`")

    @staticmethod
    def from_dict(d: dict) -> WorkloadSpec:
        env = _env_from_dict(d)
        cmd = d.get("command")
        if isinstance(cmd, str):
            import shlex
            cmd = shlex.split(cmd)
        mounts = tuple(
            Mount(
                source=str(m["source"]),
                target=str(m["target"]),
                read_only=bool(m.get("read_only", False)),
                type=str(m.get("type", "bind")),
            )
            for m in (d.get("mounts") or [])
        )
        lim = d.get("limits") or {}
        spec = WorkloadSpec(
            env=env,
            command=tuple(cmd) if cmd else None,
            stdin=d.get("stdin"),
            mounts=mounts,
            env_vars={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            network=str(d.get("network", "none")),
            limits=Limits(cpus=lim.get("cpus"), memory=lim.get("memory")),
            lifecycle=str(d.get("lifecycle", "ephemeral")),
            name=d.get("name"),
            owner=str(d.get("owner", "")),
            purpose=str(d.get("purpose", "")),
            ttl_seconds=d.get("ttl_seconds"),
            timeout_seconds=d.get("timeout_seconds", 300),
        )
        spec.validate()
        return spec


def _env_from_dict(d: dict) -> EnvSpec:
    build = d.get("build")
    bs = None
    if build:
        bs = BuildSpec(context=str(build["context"]), dockerfile=build.get("dockerfile"))
    env = EnvSpec(
        image=d.get("image"),
        build=bs,
        base=d.get("base"),
        provision=tuple(d.get("provision") or ()),
    )
    env.validate()
    return env
