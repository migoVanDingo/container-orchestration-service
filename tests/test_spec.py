"""Spec validation + parsing — no Docker needed."""
from __future__ import annotations

import pytest

from cos.core.errors import SpecError
from cos.core.spec import EnvSpec, Mount, WorkloadSpec


def test_env_requires_exactly_one_source():
    with pytest.raises(SpecError):
        EnvSpec().validate()
    with pytest.raises(SpecError):
        EnvSpec(image="a", base="b").validate()
    EnvSpec(image="alpine").validate()  # ok


def test_provision_requires_base():
    with pytest.raises(SpecError):
        EnvSpec(image="a", provision=("x",)).validate()
    EnvSpec(base="alpine", provision=("apk add curl",)).validate()  # ok


def test_from_dict_image_command_split():
    s = WorkloadSpec.from_dict({"image": "alpine", "command": "echo hi there"})
    assert s.command == ("echo", "hi", "there")
    assert s.network == "none"  # sandbox-first default


def test_from_dict_base_provision():
    s = WorkloadSpec.from_dict({"base": "debian:12", "provision": ["apt-get update"]})
    assert s.env.base == "debian:12" and s.env.provision == ("apt-get update",)


def test_from_dict_mounts_and_env():
    s = WorkloadSpec.from_dict({
        "image": "alpine",
        "mounts": [{"source": "/host", "target": "/in", "read_only": True}],
        "env": {"K": "v"},
    })
    assert s.mounts[0] == Mount(source="/host", target="/in", read_only=True)
    assert s.env_vars == {"K": "v"}


def test_persistent_requires_name():
    with pytest.raises(SpecError):
        WorkloadSpec.from_dict({"image": "alpine", "lifecycle": "persistent"})
    WorkloadSpec.from_dict({"image": "alpine", "lifecycle": "persistent", "name": "svc"})


def test_bad_network_rejected():
    with pytest.raises(SpecError):
        WorkloadSpec.from_dict({"image": "alpine", "network": "host"})


def test_bad_mount_type_rejected():
    with pytest.raises(SpecError):
        WorkloadSpec.from_dict({"image": "alpine",
                                "mounts": [{"source": "x", "target": "y", "type": "weird"}]})


def test_ports_parsed_from_string_and_dict():
    s = WorkloadSpec.from_dict({
        "image": "alpine", "network": "bridge",
        "ports": ["50505:8000", {"host": 9000, "container": 9001, "protocol": "udp"}],
    })
    assert (s.ports[0].host, s.ports[0].container, s.ports[0].protocol) == (50505, 8000, "tcp")
    assert (s.ports[1].host, s.ports[1].container, s.ports[1].protocol) == (9000, 9001, "udp")


def test_ports_require_non_none_network():
    with pytest.raises(SpecError):
        WorkloadSpec.from_dict({"image": "alpine", "ports": ["50505:8000"]})  # network=none default


def test_port_out_of_range_rejected():
    with pytest.raises(SpecError):
        WorkloadSpec.from_dict({"image": "alpine", "network": "bridge", "ports": ["70000:8000"]})
