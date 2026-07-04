"""`cos` CLI — smoke/ops over the same backend, plus `cos serve` for the MCP server."""
from __future__ import annotations

import argparse
import shlex
import sys

from cos.core.backend import DockerBackend
from cos.core.errors import CosError
from cos.core.spec import WorkloadSpec


def _backend() -> DockerBackend:
    return DockerBackend()


def _kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"error: expected K=V, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _mounts(specs: list[str]) -> list[dict]:
    out = []
    for s in specs or []:
        parts = s.split(":")
        if len(parts) < 2:
            sys.exit(f"error: --mount expects host:container[:ro], got {s!r}")
        out.append({"source": parts[0], "target": parts[1],
                    "read_only": len(parts) > 2 and parts[2] == "ro"})
    return out


def _cmd_ping(args) -> int:
    try:
        v = _backend().ping()
    except CosError as exc:
        sys.exit(str(exc))
    print(f"docker ok — server {v.get('Version', '?')} (api {v.get('ApiVersion', '?')})")
    return 0


def _cmd_run(args) -> int:
    d: dict = {
        "command": args.cmd,
        "env": _kv(args.env),
        "mounts": _mounts(args.mount),
        "ports": args.publish or [],
        "network": args.network,
        "stdin": args.stdin,
        "timeout_seconds": args.timeout,
    }
    if args.image:
        d["image"] = args.image
    if args.base:
        d["base"] = args.base
        d["provision"] = args.provision
    if args.build_context:
        d["build"] = {"context": args.build_context}
    if args.cpus or args.memory:
        d["limits"] = {"cpus": args.cpus, "memory": args.memory}
    try:
        res = _backend().run_job(WorkloadSpec.from_dict(d))
    except CosError as exc:
        sys.exit(str(exc))
    if res.stdout.strip():
        sys.stdout.write(res.stdout)
    if res.stderr.strip():
        sys.stderr.write(res.stderr)
    print(f"[exit {res.exit_code} in {res.duration_s}s]", file=sys.stderr)
    return res.exit_code


def _cmd_ps(args) -> int:
    rows = _backend().list()
    if not rows:
        print("(no managed containers)")
        return 0
    for c in rows:
        print(f"{c.name:<26} {c.lifecycle:<10} {c.status:<12} {c.image}")
    return 0


def _cmd_logs(args) -> int:
    try:
        print(_backend().logs(args.name, tail=args.tail))
    except CosError as exc:
        sys.exit(str(exc))
    return 0


def _cmd_exec(args) -> int:
    try:
        r = _backend().exec(args.name, args.command)
    except CosError as exc:
        sys.exit(str(exc))
    sys.stdout.write(r["output"])
    return r["exit_code"]


def _cmd_stop(args) -> int:
    try:
        _backend().stop(args.name)
    except CosError as exc:
        sys.exit(str(exc))
    print(f"stopped {args.name}")
    return 0


def _cmd_rm(args) -> int:
    try:
        _backend().rm(args.name)
    except CosError as exc:
        sys.exit(str(exc))
    print(f"removed {args.name}")
    return 0


def _cmd_reap(args) -> int:
    reaped = _backend().reap(only_expired=not args.all)
    print(f"reaped {len(reaped)}: {', '.join(reaped)}" if reaped else "nothing to reap")
    return 0


def _cmd_serve(args) -> int:
    try:
        from cos.mcp_server.server import build_server
    except ImportError:
        sys.exit("the MCP server needs the `mcp` extra: pip install '.[mcp]'")
    server = build_server(host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/mcp"
    print(f"serving MCP over streamable-HTTP at {url}", file=sys.stderr)
    server.run(transport="streamable-http")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cos", description="Docker control plane + MCP server.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="check the Docker daemon").set_defaults(fn=_cmd_ping)

    r = sub.add_parser("run", help="run a one-shot job")
    r.add_argument("image", nargs="?", help="image to run (or use --base / --build-context)")
    r.add_argument("--cmd", dest="cmd", help="command (shell string)")
    r.add_argument("--base", help="base image for base+provision")
    r.add_argument("--provision", action="append", default=[], help="setup step (repeatable)")
    r.add_argument("--build-context", dest="build_context", help="build a context dir")
    r.add_argument("--mount", action="append", default=[], help="host:container[:ro] (repeatable)")
    r.add_argument("--publish", action="append", default=[],
                   help="host:container port (repeatable; implies --network bridge)")
    r.add_argument("--env", action="append", default=[], help="K=V (repeatable)")
    r.add_argument("--network", default="none", choices=["none", "bridge"])
    r.add_argument("--cpus", type=float)
    r.add_argument("--memory")
    r.add_argument("--stdin", help="feed this string to the command's stdin")
    r.add_argument("--timeout", type=int, default=300)
    r.set_defaults(fn=_cmd_run)

    sub.add_parser("ps", help="list managed containers").set_defaults(fn=_cmd_ps)

    lg = sub.add_parser("logs", help="logs of a managed container")
    lg.add_argument("name")
    lg.add_argument("--tail", type=int)
    lg.set_defaults(fn=_cmd_logs)

    ex = sub.add_parser("exec", help="exec in a persistent container")
    ex.add_argument("name")
    ex.add_argument("command", help="command (shell string)")
    ex.set_defaults(fn=lambda a: _cmd_exec_wrap(a))

    st = sub.add_parser("stop", help="stop a persistent container")
    st.add_argument("name")
    st.set_defaults(fn=_cmd_stop)

    rm = sub.add_parser("rm", help="remove a managed container")
    rm.add_argument("name")
    rm.set_defaults(fn=_cmd_rm)

    rp = sub.add_parser("reap", help="remove ephemeral managed containers")
    rp.add_argument("--all", action="store_true", help="not just expired ones")
    rp.set_defaults(fn=_cmd_reap)

    sv = sub.add_parser("serve", help="run the MCP server (streamable-HTTP)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8770)
    sv.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


def _cmd_exec_wrap(args) -> int:
    args.command = shlex.split(args.command)
    return _cmd_exec(args)


if __name__ == "__main__":
    sys.exit(main())
