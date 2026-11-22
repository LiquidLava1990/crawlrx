"""doctor.py -- first-run environment check for CrawlRX.

``crawlrx doctor`` answers "will this work on my machine?" in one screen:

* Is the Playwright Chromium browser installed (needed for rendering)?
* Is a model server reachable (Ollama, LM Studio, or a configured endpoint)?
* How much RAM / GPU memory is there, and what model size fits?
* Is a SearXNG instance reachable (optional; better search results)?

Every failing check prints exactly what to run to fix it.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import model_backend

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Hardware probes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hardware:
    ram_gb: float
    gpu_name: str
    gpu_vram_gb: float
    unified_memory: bool  # Apple Silicon: GPU shares system RAM

    @property
    def model_memory_gb(self) -> float:
        """Memory realistically available to hold model weights."""
        if self.gpu_vram_gb >= 4:
            return self.gpu_vram_gb
        if self.unified_memory:
            return self.ram_gb * 0.65
        return self.ram_gb * 0.5  # CPU inference: leave room for the OS + browser


def _ram_windows() -> float:
    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return 0.0
    return status.ullTotalPhys / GIB


def _ram_linux() -> float:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024 / GIB
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def _run_quiet(cmd: list[str], timeout_s: float = 5.0) -> str:
    """Run a probe command; empty string on any failure."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _ram_macos() -> float:
    raw = _run_quiet(["sysctl", "-n", "hw.memsize"])
    return int(raw) / GIB if raw.isdigit() else 0.0


def total_ram_gb() -> float:
    if os.name == "nt":
        return _ram_windows()
    if sys.platform == "darwin":
        return _ram_macos()
    return _ram_linux()


def _nvidia_gpu() -> tuple[str, float]:
    raw = _run_quiet(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not raw:
        return "", 0.0
    best_name, best_mib = "", 0.0
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            try:
                mib = float(parts[1])
            except ValueError:
                continue
            if mib > best_mib:
                best_name, best_mib = parts[0], mib
    return best_name, best_mib / 1024


def detect_hardware() -> Hardware:
    ram = total_ram_gb()
    gpu_name, vram = _nvidia_gpu()
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    if apple_silicon and not gpu_name:
        gpu_name = "Apple Silicon (unified memory)"
    return Hardware(ram_gb=round(ram, 1), gpu_name=gpu_name, gpu_vram_gb=round(vram, 1),
                    unified_memory=apple_silicon)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelRecommendation:
    tier: str
    text_model: str
    vision_model: str
    note: str

    @property
    def pull_commands(self) -> list[str]:
        models = dict.fromkeys(m for m in (self.text_model, self.vision_model) if m)
        return [f"ollama pull {m}" for m in models]


# (minimum model memory GB, recommendation). Ollama tags; one multimodal model
# covers both extraction and vision wherever it fits.
_TIERS: tuple[tuple[float, ModelRecommendation], ...] = (
    (20.0, ModelRecommendation("large", "qwen2.5vl:32b", "qwen2.5vl:32b",
                               "Best quality; needs a 24 GB GPU or 48 GB+ Apple Silicon.")),
    (6.0, ModelRecommendation("medium", "qwen2.5vl:7b", "qwen2.5vl:7b",
                              "Good quality for extraction and vision.")),
    (3.0, ModelRecommendation("small", "qwen2.5vl:3b", "qwen2.5vl:3b",
                              "Default. Runs on most laptops (about 3 GB).")),
    (0.0, ModelRecommendation("tiny", "qwen2.5:1.5b", "moondream",
                              "Low-memory machine: small text model plus a tiny vision model.")),
)


def recommend_model(hardware: Hardware) -> ModelRecommendation:
    budget = hardware.model_memory_gb
    for minimum, rec in _TIERS:
        if budget >= minimum:
            return rec
    return _TIERS[-1][1]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True


@dataclass
class DoctorReport:
    hardware: Hardware
    recommendation: ModelRecommendation
    model: model_backend.ModelSettings
    servers: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def model_ready(self) -> bool:
        return any(c.name == "model" and c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "model_ready": self.model_ready,
            "hardware": self.hardware.__dict__,
            "recommendation": self.recommendation.__dict__,
            "model": self.model.public_dict(),
            "local_servers": self.servers,
            "checks": [c.__dict__ for c in self.checks],
        }


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 -- any launcher failure means "not installed"
        return False


def check_browser() -> Check:
    if _chromium_installed():
        return Check("browser", True, "Playwright Chromium is installed.")
    return Check("browser", False, "Playwright Chromium is not installed.",
                 fix=f"{Path(sys.executable).name} -m playwright install chromium   (or: crawlrx setup)")


def _model_check(settings: model_backend.ModelSettings, rec: ModelRecommendation) -> Check:
    pull = " && ".join(rec.pull_commands)
    if not settings.configured:
        return Check("model", False, "No model server found (optional: only /extract and /vision need one).",
                     fix=f"Install Ollama (https://ollama.com/download), then: {pull}\n"
                         + model_backend.setup_hint(), required=False)
    try:
        resolved = model_backend.require_settings()
    except model_backend.ModelBackendUnavailable as exc:
        return Check("model", False, str(exc), fix=pull, required=False)
    vision = resolved.vision_model or "none (vision disabled)"
    return Check("model", True,
                 f"{resolved.base_url} [{settings.source}] text={resolved.model} vision={vision}",
                 required=False)


def _searxng_check() -> Check:
    url = os.getenv("SEARXNG_URL", "http://localhost:8888").rstrip("/")
    try:
        resp = httpx.get(f"{url}/healthz", timeout=2.0, headers={"X-Real-IP": "127.0.0.1"})
        if resp.status_code < 500:
            return Check("searxng", True, f"SearXNG reachable at {url}.", required=False)
    except httpx.HTTPError:
        pass
    return Check("searxng", False,
                 f"SearXNG not reachable at {url} (optional; search falls back to built-in engines).",
                 fix="docker compose up -d searxng   (or set SEARXNG_URL)", required=False)


def _captcha_check() -> Check:
    """Captcha mode, and in solve mode which solvers are usable."""
    from .captcha_handoff import captcha_mode

    mode = captcha_mode()
    if mode != "solve":
        return Check("captcha", True, "mode=handoff (a human solves captchas; solvers are off).", required=False)
    from . import solvers

    rows = solvers.solver_status()
    summary = ", ".join(f"{row['name']}: {row['state']}" for row in rows)
    if any(row["state"] == "configured" for row in rows):
        return Check("captcha", True, f"mode=solve ({summary}); failures fall back to the hand-off.",
                     required=False)
    return Check("captcha", False, f"mode=solve but no solver is usable ({summary}).",
                 fix="Install ffmpeg for reCAPTCHA audio, start the challenge-gateway service and set CRAWLRX_FLARESOLVERR_URL "
                     "(Docker does both), or set CRAWLRX_SOLVER_SERVICE_URL. CRAWLRX_CAPTCHA_MODE=handoff "
                     "turns solving off.",
                 required=False)


def run_doctor(check_browser_binary: bool = True) -> DoctorReport:
    hardware = detect_hardware()
    rec = recommend_model(hardware)
    servers = model_backend.detect_local_servers()
    settings = model_backend.resolve_settings(refresh=True)
    report = DoctorReport(hardware=hardware, recommendation=rec, model=settings, servers=servers)
    if check_browser_binary:
        report.checks.append(check_browser())
    report.checks.append(_model_check(settings, rec))
    report.checks.append(_searxng_check())
    report.checks.append(_captcha_check())
    return report


def _hardware_line(hw: Hardware) -> str:
    gpu = f"{hw.gpu_name} ({hw.gpu_vram_gb:g} GB)" if hw.gpu_vram_gb else (hw.gpu_name or "none detected")
    return f"RAM {hw.ram_gb:g} GB | GPU {gpu}"


def format_report(report: DoctorReport) -> str:
    lines = ["CrawlRX doctor", "=" * 40, _hardware_line(report.hardware)]
    rec = report.recommendation
    lines.append(f"Recommended model ({rec.tier}): {rec.text_model}"
                 + (f" + {rec.vision_model}" if rec.vision_model != rec.text_model else "")
                 + f" -- {rec.note}")
    if report.servers:
        for server in report.servers:
            lines.append(f"Found {server['name']} at {server['base_url']} "
                         f"({len(server['models'])} model(s))")
    lines.append("")
    for check in report.checks:
        mark = "OK  " if check.ok else ("FAIL" if check.required else "WARN")
        lines.append(f"[{mark}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.extend(f"       {row}" for row in check.fix.splitlines())
    lines.append("")
    lines.append("Ready." if report.ok else "Not ready: fix the FAIL items above.")
    return "\n".join(lines)


def startup_summary() -> Optional[str]:
    """One-line model status for server boot logs (no browser probe)."""
    settings = model_backend.resolve_settings(refresh=True)
    if not settings.configured:
        return "No AI model configured; /extract and /vision are disabled until one is. Run `crawlrx doctor`."
    return f"AI model endpoint: {settings.base_url} ({settings.source})"


__all__ = [
    "Check",
    "DoctorReport",
    "Hardware",
    "ModelRecommendation",
    "detect_hardware",
    "format_report",
    "recommend_model",
    "run_doctor",
    "startup_summary",
]
