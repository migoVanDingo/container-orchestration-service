"""Live backend tests against the real Docker daemon. Skips if unreachable."""
from __future__ import annotations

import time

import pytest

from cos.core.backend import DockerBackend
from cos.core.spec import EnvSpec, Limits, PortMap, WorkloadSpec


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


def test_persistent_published_port_is_reachable(backend):
    """The scenario the user hit: a persistent server, published, curl-able."""
    import urllib.request

    name = "cos-test-web"
    try:
        backend.rm(name)
    except Exception:  # noqa: BLE001
        pass
    spec = WorkloadSpec(
        env=EnvSpec(image="python:3.11-slim"),
        command=("python", "-m", "http.server", "8000"),
        lifecycle="persistent",
        name=name,
        network="bridge",
        ports=(PortMap(host=50506, container=8000),),
    )
    backend.ensure_env(spec)
    try:
        time.sleep(1.0)  # let the server bind
        with urllib.request.urlopen("http://127.0.0.1:50506/", timeout=5) as r:
            assert r.status == 200
    finally:
        backend.rm(name)


def test_ensure_surfaces_immediate_crash(backend):
    """A container that exits on start must raise with its logs, not report running."""
    from cos.core.errors import BackendError

    name = "cos-test-crash"
    try:
        backend.rm(name)
    except Exception:  # noqa: BLE001
        pass
    spec = WorkloadSpec(
        env=EnvSpec(image="alpine:3.19"),
        command=("sh", "-c", "echo boom-msg >&2; exit 3"),
        lifecycle="persistent",
        name=name,
    )
    with pytest.raises(BackendError) as ei:
        backend.ensure_env(spec)
    assert "boom-msg" in str(ei.value) or "code 3" in str(ei.value)


def test_shared_network_inter_container_dns(backend):
    """Two containers on a user-defined network reach each other by name."""
    net = "cos-test-net"
    try:
        backend.rm("web")
    except Exception:  # noqa: BLE001
        pass
    try:
        backend.remove_network(net)
    except Exception:  # noqa: BLE001
        pass
    backend.ensure_network(net)
    try:
        backend.ensure_env(WorkloadSpec(
            env=EnvSpec(image="python:3.11-slim"),
            command=("python", "-m", "http.server", "8000"),
            lifecycle="persistent", name="web", network=net,
        ))
        time.sleep(1.0)
        # one-shot client on the same network reaches the server by DNS name cos-web
        res = backend.run_job(WorkloadSpec(
            env=EnvSpec(image="alpine:3.19"),
            command=("wget", "-qO-", "http://cos-web:8000/"),
            network=net,
        ))
        assert res.exit_code == 0
        assert "Directory listing" in res.stdout or "<html" in res.stdout.lower()
        assert net in [n["name"] for n in backend.list_networks()]
    finally:
        try:
            backend.rm("web")
        except Exception:  # noqa: BLE001
            pass
        backend.remove_network(net)


def test_build_image_once_run_many(backend):
    """build_image → a managed, reusable tag runnable by many containers."""
    tag = "cos-test-img:latest"
    try:
        backend.remove_image(tag, force=True)
    except Exception:  # noqa: BLE001
        pass
    info = backend.build_image(
        tag, dockerfile_inline="FROM alpine:3.19\nRUN echo built-once > /marker\n")
    assert info["tag"] == tag
    try:
        # the tag is managed + listed
        assert any(tag in i["tags"] for i in backend.list_images())
        # run two jobs off the same tag
        for _ in range(2):
            res = backend.run_job(WorkloadSpec(
                env=EnvSpec(image=tag), command=("cat", "/marker")))
            assert res.exit_code == 0 and "built-once" in res.stdout
    finally:
        backend.remove_image(tag, force=True)
    assert not any(tag in i["tags"] for i in backend.list_images())


def test_gc_reclaims_stopped_network_and_unused_image(backend):
    net = "cos-test-gc-net"
    img = "cos-test-gc-img:latest"
    stopped, running = "cos-test-gc-stopped", "cos-test-gc-running"
    for n in (stopped, running):
        try:
            backend.rm(n)
        except Exception:  # noqa: BLE001
            pass
    backend.ensure_network(net)                       # empty managed network
    backend.build_image(img, dockerfile_inline="FROM alpine:3.19\n")  # unused managed image
    backend.ensure_env(WorkloadSpec(env=EnvSpec(image="alpine:3.19"),
                       command=("sleep", "120"), lifecycle="persistent", name=stopped))
    backend.stop(stopped)                             # stopped managed container
    backend.ensure_env(WorkloadSpec(env=EnvSpec(image="alpine:3.19"),
                       command=("sleep", "120"), lifecycle="persistent", name=running))
    try:
        r = backend.gc()
        assert f"cos-{stopped}" in r["containers"]     # prune returns docker names
        assert net in r["networks"]
        assert any(img in x for x in r["images"])
        # the RUNNING container must survive gc (list() returns logical names)
        assert running in [c.name for c in backend.list()]
    finally:
        for n in (stopped, running):
            try:
                backend.rm(n)
            except Exception:  # noqa: BLE001
                pass
        try:
            backend.remove_image(img, force=True)
        except Exception:  # noqa: BLE001
            pass


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
