"""Command line for the local Pi adapter."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from pi_qoder_adapter.catalog import render_pi_config
from pi_qoder_adapter.probe import run_probe
from pi_qoder_adapter.server import create_app

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_loopback(host: str) -> str:
    if host.lower() not in LOOPBACK_HOSTS:
        raise SystemExit(f"拒绝监听 {host}。适配器只能绑定 127.0.0.1、localhost 或 ::1。")
    return host


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pi-qoder-adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动只监听本机的 OpenAI 兼容接口")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    config = sub.add_parser("print-config", help="打印要合并进 Pi models.json 的片段")
    config.add_argument("--port", type=int, default=8765)

    probe = sub.add_parser("probe", help="验证 Qoder 工具可以挂起并交还给调用方")
    probe.add_argument("--model", default="efficient")
    probe.add_argument("--wait-seconds", type=float, default=60)

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "serve":
        host = require_loopback(args.host)
        uvicorn.run(create_app(refresh_on_startup=True), host=host, port=args.port)
        return
    if args.command == "print-config":
        print(render_pi_config(args.port))
        return
    if args.command == "probe":
        raise SystemExit(asyncio.run(run_probe(model=args.model, wait_seconds=args.wait_seconds)))
    raise SystemExit(f"未知命令: {args.command}")


if __name__ == "__main__":
    main(sys.argv[1:])
