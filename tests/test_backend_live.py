"""Live backend tests against the real Docker daemon. Skips if unreachable."""
from __future__ import annotations

import pytest

from cos.core.backend import DockerBackend
from cos.core.spec import EnvSpec, Limits, WorkloadSpec


@pytest.fixture(scope="module")
def backend():
    b = DockerBackend()
    try:
        b.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"docker daemon not reachable: {exc}")
    return b


def _spec(**kw):
    kw.setdefault("network", "none")
    return WorkloadSpec(**kw)


def test_run_job_image_echo(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("echo", "hello-cos"),
    ))
    assert res.exit_code == 0
    assert "hello-cos" in res.stdout


def test_run_job_env_vars(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("sh", "-c", "echo $FOO"),
        env_vars={"FOO": "barbaz"},
    ))
    assert "barbaz" in res.stdout


def test_run_job_stdin(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("cat",),
        stdin="piped-payload\n",
    ))
    assert "piped-payload" in res.stdout


def test_run_job_base_provision(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(base="alpine:3.19", provision=("echo built-in-image > /marker",)),
        command=("cat", "/marker"),
    ))
    assert "built-in-image" in res.stdout


def test_run_job_nonzero_exit(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("sh", "-c", "exit 7"),
    ))
    assert res.exit_code == 7


def test_run_job_limits_accepted(backend):
    res = backend.run_job(_spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("echo", "ok"),
        limits=Limits(cpus=0.5, memory="128m"),
    ))
    assert res.exit_code == 0


def test_persistent_lifecycle(backend):
    name = "cos-test-persist"
    # clean any leftover
    try:
        backend.rm(name)
    except Exception:  # noqa: BLE001
        pass
    spec = _spec(
        env=EnvSpec(image="alpine:3.19"),
        command=("sleep", "120"),
        lifecycle="persistent",
        name=name,
    )
    handle = backend.ensure_env(spec)
    assert handle.status == "running"
    # idempotent find-or-create
    handle2 = backend.ensure_env(spec)
    assert handle2.id == handle.id
    ex = backend.exec(name, ["echo", "exec-works"])
    assert ex["exit_code"] == 0 and "exec-works" in ex["output"]
    names = [c.name for c in backend.list()]
    assert name in names
    backend.stop(name)
    backend.rm(name)
    assert name not in [c.name for c in backend.list()]
