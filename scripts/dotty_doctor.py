#!/usr/bin/env python3
"""dotty doctor — health-check CLI for the Dotty/StackChan stack.

Runs the same checks as `make doctor` but as a portable Python script
that can be invoked on any host (workstation, RPi, CI) without make.

Usage:
    python scripts/dotty_doctor.py [options]

Options:
    --config PATH   Path to .config.yaml (default: auto-discovered)
    --bridge-url U  Override bridge health URL
    --server-url U  Override xiaozhi server URL
    --timeout N     HTTP timeout in seconds (default: 5)
    --json          Output results as JSON to stdout
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ── ANSI colour helpers ───────────────────────────────────────────────────────

def _supports_color() -> bool:
    return (
        hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
        and os.getenv("NO_COLOR") is None
    )


_COLOR = _supports_color()
GREEN  = "\033[0;32m" if _COLOR else ""
RED    = "\033[0;31m" if _COLOR else ""
YELLOW = "\033[0;33m" if _COLOR else ""
BOLD   = "\033[1m"    if _COLOR else ""
RESET  = "\033[0m"    if _COLOR else ""


# ── Result type ───────────────────────────────────────────────────────────────

class Result:
    __slots__ = ("label", "status", "detail")

    def __init__(self, label: str, status: str, detail: str = "") -> None:
        assert status in ("pass", "fail", "skip", "warn")
        self.label = label
        self.status = status
        self.detail = detail

    def print_line(self) -> None:
        tag = {
            "pass": f"{GREEN}PASS{RESET}",
            "fail": f"{RED}FAIL{RESET}",
            "skip": f"{YELLOW}SKIP{RESET}",
            "warn": f"{YELLOW}WARN{RESET}",
        }[self.status]
        suffix = f"  ({self.detail})" if self.detail else ""
        print(f"  {tag}  {self.label}{suffix}")

    def to_dict(self) -> dict:
        return {"label": self.label, "status": self.status, "detail": self.detail}


# ── Config discovery ──────────────────────────────────────────────────────────

def _find_config(hint: Optional[str] = None) -> Optional[Path]:
    if hint:
        p = Path(hint).expanduser()
        return p if p.exists() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        p = candidate / ".config.yaml"
        if p.exists():
            return p
    return None


def _extract_unraid_ip(config_text: str) -> Optional[str]:
    m = re.search(r"ws://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", config_text)
    return m.group(1) if m else None


def _extract_rpi_ip(config_text: str) -> Optional[str]:
    m = re.search(r"url:\s*http://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", config_text)
    return m.group(1) if m else None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_config_exists(config_path: Optional[Path]) -> Result:
    label = ".config.yaml exists"
    if config_path is None:
        return Result(label, "fail", "not found — run `make setup`")
    return Result(label, "pass", str(config_path))


def check_no_placeholders(config_path: Optional[Path]) -> Result:
    label = ".config.yaml — no unsubstituted placeholders"
    if config_path is None:
        return Result(label, "skip", "config not found")
    found = re.findall(r"<[A-Z][A-Z0-9_]+>", config_path.read_text())
    if found:
        unique = sorted(set(found))
        return Result(label, "fail", "placeholders present: " + ", ".join(unique))
    return Result(label, "pass")


def check_models_sensevoice(config_path: Optional[Path]) -> Result:
    label = "SenseVoiceSmall model files present"
    root = config_path.parent if config_path else Path.cwd()
    model_dir = root / "models" / "SenseVoiceSmall"
    if not model_dir.is_dir():
        return Result(label, "fail", f"{model_dir} missing — run `make fetch-models`")
    files = list(model_dir.iterdir())
    if not files:
        return Result(label, "fail", f"{model_dir} is empty")
    return Result(label, "pass", f"{len(files)} files")


def check_models_piper(config_path: Optional[Path]) -> Result:
    label = "Piper TTS model (*.onnx) present"
    root = config_path.parent if config_path else Path.cwd()
    piper_dir = root / "models" / "piper"
    if not piper_dir.is_dir():
        return Result(label, "fail", f"{piper_dir} missing — run `make fetch-models`")
    onnx_files = list(piper_dir.glob("*.onnx"))
    if not onnx_files:
        return Result(label, "fail", "no .onnx files in models/piper/")
    return Result(label, "pass", onnx_files[0].name)


def check_http(label: str, url: str, timeout: int) -> Result:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as exc:
        return Result(label, "fail", f"unreachable — {str(exc)[:80]}")
    if status < 500:
        return Result(label, "pass", f"HTTP {status}")
    return Result(label, "fail", f"HTTP {status}")


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_checks(
    config_path: Optional[Path],
    bridge_url: Optional[str],
    server_url: Optional[str],
    timeout: int,
) -> list[Result]:
    results: list[Result] = []

    results.append(check_config_exists(config_path))
    results.append(check_no_placeholders(config_path))
    results.append(check_models_sensevoice(config_path))
    results.append(check_models_piper(config_path))

    config_text = config_path.read_text() if config_path else ""

    if bridge_url is None:
        rpi_ip = _extract_rpi_ip(config_text)
        if rpi_ip:
            bridge_url = f"http://{rpi_ip}:8080/health"
    if bridge_url:
        results.append(check_http("Bridge /health reachable", bridge_url, timeout))
    else:
        results.append(Result(
            "Bridge /health reachable", "skip",
            "pass --bridge-url or ensure config has 'url: http://<RPI_IP>:8080'",
        ))

    if server_url is None:
        unraid_ip = _extract_unraid_ip(config_text)
        if unraid_ip:
            server_url = f"http://{unraid_ip}:8003/xiaozhi/ota/"
    if server_url:
        results.append(check_http("Xiaozhi OTA endpoint reachable", server_url, timeout))
    else:
        results.append(Result(
            "Xiaozhi OTA endpoint reachable", "skip",
            "pass --server-url or ensure config has ws://<UNRAID_IP>:8000",
        ))

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dotty health-check CLI (portable alternative to `make doctor`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH",
                        help="Path to .config.yaml (auto-discovered if omitted)")
    parser.add_argument("--bridge-url", metavar="URL",
                        help="Bridge health URL, e.g. http://192.168.1.20:8080/health")
    parser.add_argument("--server-url", metavar="URL",
                        help="Xiaozhi OTA URL, e.g. http://192.168.1.10:8003/xiaozhi/ota/")
    parser.add_argument("--timeout", metavar="N", type=int, default=5,
                        help="HTTP timeout in seconds (default: 5)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON array of results to stdout")
    args = parser.parse_args()

    config_path = _find_config(args.config)

    if not args.json:
        print(f"\n{BOLD}Dotty doctor{RESET}\n")

    results = run_checks(
        config_path=config_path,
        bridge_url=args.bridge_url,
        server_url=args.server_url,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            r.print_line()
        passed  = sum(1 for r in results if r.status == "pass")
        failed  = sum(1 for r in results if r.status == "fail")
        skipped = sum(1 for r in results if r.status in ("skip", "warn"))
        print(f"\n{BOLD}Results: {passed} passed, {failed} failed, {skipped} skipped.{RESET}\n")

    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
