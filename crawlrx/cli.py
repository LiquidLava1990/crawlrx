"""cli.py -- the ``crawlrx`` command.

    crawlrx setup            install the browser, check models, save settings
    crawlrx serve            start the API on http://127.0.0.1:7337
    crawlrx doctor           check browser / model / search and suggest fixes
    crawlrx crawl <url>      fetch one page and print it as markdown
    crawlrx version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from typing import Optional, Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7337


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("crawlrx")
    except Exception:  # noqa: BLE001 -- running from a source checkout
        from . import __version__

        return __version__ + "+source"


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor, model_backend

    report = doctor.run_doctor()
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(doctor.format_report(report))
    if args.save and report.model.configured:
        resolved = model_backend.require_settings() if report.model_ready else report.model
        path = model_backend.save_settings(resolved)
        print(f"\nSaved model settings to {path}")
    return 0 if report.ok else 1


def install_browser() -> int:
    """Install Playwright's Chromium (plus OS deps on Linux when root)."""
    cmd = [sys.executable, "-m", "playwright", "install"]
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--with-deps")
    cmd.append("chromium")
    print("Installing Chromium for Playwright:", " ".join(cmd))
    return subprocess.call(cmd)


def _cmd_setup(args: argparse.Namespace) -> int:
    if not args.skip_browser:
        code = install_browser()
        if code != 0:
            print("Chromium install failed. Re-run: python -m playwright install chromium", file=sys.stderr)
            return code
    return _cmd_doctor(argparse.Namespace(json=False, save=True))


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def exposure_error(host: str) -> Optional[str]:
    """Refuse to serve an unauthenticated API beyond this machine.

    ``CRAWLRX_BIND`` states where the port is really published (Docker binds
    0.0.0.0 inside the container; compose publishes on 127.0.0.1 by default).
    """
    public_host = os.getenv("CRAWLRX_BIND", "").strip() or host
    if public_host in _LOOPBACK:
        return None
    if os.getenv("CRAWLRX_API_KEYS", "").strip():
        return None
    if os.getenv("CRAWLRX_ALLOW_NO_AUTH", "").strip().lower() in ("1", "true", "yes"):
        return None
    return (f"Refusing to serve on {public_host} without authentication: anyone who can reach it "
            "could use this machine (and your model API key) to scrape. Set CRAWLRX_API_KEYS=<key1,key2>, "
            "bind to 127.0.0.1, or set CRAWLRX_ALLOW_NO_AUTH=1 on a trusted network.")


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    problem = exposure_error(args.host)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if args.headful:
        os.environ["CRAWLRX_HEADLESS"] = "false"
        os.environ.setdefault("CRAWLRX_CAPTCHA_HANDOFF", "browser")
    print(f"CrawlRX API on http://{args.host}:{args.port}  (docs: /docs, dashboard: /dashboard)")
    uvicorn.run("crawlrx.main:app", host=args.host, port=args.port, log_level=args.log_level)
    return 0


async def _crawl_once(url: str, as_json: bool) -> int:
    from .crawl import crawl
    from .models import CrawlRequest

    result = await crawl(CrawlRequest(url=url, cache=False))
    if as_json:
        print(result.model_dump_json(indent=2, exclude={"html"}))
    elif result.success:
        print(result.markdown or result.text or "")
    else:
        print(f"Crawl failed: {result.error}", file=sys.stderr)
        if getattr(result, "captcha", None):
            print(result.captcha.get("action", ""), file=sys.stderr)
    return 0 if result.success else 1


def _cmd_crawl(args: argparse.Namespace) -> int:
    return asyncio.run(_crawl_once(args.url, args.json))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crawlrx", description="Self-hosted crawl / scrape / search / vision API.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the HTTP API")
    serve.add_argument("--host", default=os.getenv("CRAWLRX_HOST", DEFAULT_HOST),
                       help="Bind address (use 0.0.0.0 to expose on your network)")
    serve.add_argument("--port", type=int, default=int(os.getenv("CRAWLRX_PORT", str(DEFAULT_PORT))))
    serve.add_argument("--headful", action="store_true",
                       help="Show the browser window and let you solve captchas yourself")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_cmd_serve)

    doc = sub.add_parser("doctor", help="Check browser, model and search setup")
    doc.add_argument("--json", action="store_true", help="Machine-readable output")
    doc.add_argument("--save", action="store_true", help="Save the detected model endpoint to the config file")
    doc.set_defaults(func=_cmd_doctor)

    setup = sub.add_parser("setup", help="Install Chromium and save model settings (first run)")
    setup.add_argument("--skip-browser", action="store_true")
    setup.set_defaults(func=_cmd_setup)

    crawl_cmd = sub.add_parser("crawl", help="Fetch one URL and print markdown")
    crawl_cmd.add_argument("url")
    crawl_cmd.add_argument("--json", action="store_true", help="Print the full JSON result")
    crawl_cmd.set_defaults(func=_cmd_crawl)

    ver = sub.add_parser("version", help="Print the version")
    ver.set_defaults(func=lambda _a: print(_version()) or 0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
