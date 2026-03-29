# -*- coding: utf-8 -*-
import os
import json
import logging
import concurrent.futures
import base64
import ctypes
import hashlib
import ipaddress
import re
import socket
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
import sys
import platform

from flask import Flask, render_template, request, jsonify, session
from ai_engine import find_matches, precompute
from blockchain import safe_send_hush_metric, hush_status  # type: ignore
try:
    from db import init_db
except Exception:
    init_db = None
try:
    import psutil  # type: ignore
except Exception:
    psutil = None
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None
try:
    import google.generativeai as genai  # type: ignore
except Exception:
    genai = None
try:
    import anthropic  # type: ignore
except Exception:
    anthropic = None

FROZEN = bool(getattr(sys, "frozen", False))
BASE_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) if FROZEN else Path(__file__).resolve().parent
RUNTIME_PATH = Path(sys.executable).resolve().parent if FROZEN else BASE_PATH

if load_dotenv:
    dotenv_path = RUNTIME_PATH / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

ISSUES_PATH = BASE_PATH / "issues.json"
EXEC_LOG_PATH = RUNTIME_PATH / "execution_log.json"

app = Flask(__name__, template_folder=str(BASE_PATH / "templates"), static_folder=str(BASE_PATH / "static"))
_secret = os.environ.get("AI_TS_SESSION_SECRET", "")
if not _secret:
    import secrets as _secrets_mod
    _secret = _secrets_mod.token_hex(32)
app.secret_key = _secret
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB max upload

_GEMINI_MODEL = None
_GEMINI_MODEL_NAME = None
_GEMINI_MODEL_LOCK = threading.Lock()

_CLAUDE_CLIENT = None
_CLAUDE_CLIENT_LOCK = threading.Lock()


def _run_safe_command(cmd: str) -> dict:
    """Execute a shell command safely and return result dict."""
    if not cmd or not cmd.strip():
        raise ValueError("Empty command")
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return {
            "success": proc.returncode == 0,
            "output": proc.stdout.strip(),
            "error": proc.stderr.strip(),
            "return_code": proc.returncode,
            "simulation_mode": False,
        }
    except subprocess.TimeoutExpired:
        raise


def _windows_only_execution_error(cmd: str, desc: str, sid: int) -> dict:
    host = platform.system() or "Unknown"
    return {
        "success": False,
        "output": "",
        "error": (
            "Command execution is Windows-only, but this server is running on "
            f"{host}. Run FIXMATE on a Windows machine to execute remediation commands."
        ),
        "return_code": -1,
        "command": cmd,
        "simulation_mode": False,
        "solution_id": sid,
        "description": desc,
    }

if os.environ.get("AI_TS_DEBUG_GEMINI", "").strip().lower() in {"1", "true", "yes", "on"}:
    app.logger.setLevel(logging.INFO)


def is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_issues() -> List[dict]:
    try:
        mtime = ISSUES_PATH.stat().st_mtime
    except FileNotFoundError:
        return []
    cached = _ISSUES_CACHE.get("data")
    if cached is not None and _ISSUES_CACHE.get("mtime") == mtime:
        return cached  # type: ignore[return-value]
    try:
        with open(ISSUES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        app.logger.error("ISSUES: failed to load issues.json: %s", exc)
        return cached if cached is not None else []
    _ISSUES_CACHE["mtime"] = mtime
    _ISSUES_CACHE["data"] = data
    return data


def _extract_first_int(text: str, max_value: int) -> int | None:
    if not text:
        return None
    for match in re.finditer(r"\b(\d{1,3})\b", text):
        try:
            value = int(match.group(1))
        except Exception:
            continue
        if 1 <= value <= max_value:
            return value
    return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_text(value: str) -> str:
    return " ".join((value or "").lower().split())


def _hash_texts(texts: List[str]) -> str:
    payload = "\n".join(texts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _get_gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _gemini_configured() -> bool:
    return genai is not None and bool(_get_gemini_api_key())


def _gemini_debug_enabled() -> bool:
    return _env_flag("AI_TS_DEBUG_GEMINI", default=False)


def _log_gemini_debug(message: str, payload: object | None = None) -> None:
    if not _gemini_debug_enabled():
        return
    if payload is None:
        app.logger.info("Gemini debug: %s", message)
        return
    if isinstance(payload, str):
        app.logger.info("Gemini debug: %s\n%s", message, payload)
        return
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=True)
    except Exception:
        text = str(payload)
    app.logger.info("Gemini debug: %s\n%s", message, text)


def _is_windows() -> bool:
    return os.name == "nt"


def _is_frozen() -> bool:
    return FROZEN


def _console_hotkey_enabled() -> bool:
    return _env_flag("AI_TS_ENABLE_CONSOLE_HOTKEY", default=True)


def _should_hide_console() -> bool:
    return _env_flag("AI_TS_HIDE_CONSOLE", default=_is_frozen())


def _get_console_window() -> int:
    if not _is_windows():
        return 0
    return int(ctypes.windll.kernel32.GetConsoleWindow())


def _hide_console() -> None:
    if not _is_windows():
        return
    hwnd = _get_console_window()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)


def _show_console() -> None:
    if not _is_windows():
        return
    hwnd = _get_console_window()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 5)
        ctypes.windll.user32.SetForegroundWindow(hwnd)


def _toggle_console() -> None:
    if not _is_windows():
        return
    hwnd = _get_console_window()
    if not hwnd:
        return
    visible = ctypes.windll.user32.IsWindowVisible(hwnd)
    if visible:
        _hide_console()
    else:
        _show_console()


def _console_hotkey_loop() -> None:
    if not _is_windows():
        return
    user32 = ctypes.windll.user32
    hotkey_id = 1
    mod_control = 0x0002
    mod_shift = 0x0004
    vk_c = 0x43
    if not user32.RegisterHotKey(None, hotkey_id, mod_control | mod_shift, vk_c):
        app.logger.warning("CONSOLE: failed to register hotkey (Ctrl+Shift+C)")
        return
    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                _toggle_console()
    finally:
        user32.UnregisterHotKey(None, hotkey_id)


def _setup_console_hotkey() -> None:
    if not _is_windows() or not _console_hotkey_enabled():
        return
    if _should_hide_console():
        _hide_console()
    thread = threading.Thread(target=_console_hotkey_loop, daemon=True)
    thread.start()


def _should_open_browser() -> bool:
    return _env_flag("AI_TS_OPEN_BROWSER", default=True)


def _browser_url() -> str:
    return os.environ.get("AI_TS_URL", "http://127.0.0.1:5050")


def _debug_mode() -> bool:
    return _env_flag("AI_TS_DEBUG", default=True)


def _should_open_browser_now(debug_mode: bool) -> bool:
    if debug_mode and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True


def _open_browser_delayed() -> None:
    if not _should_open_browser():
        return
    url = _browser_url()

    def _open() -> None:
        time.sleep(1)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()

def _gemini_timeout_seconds() -> float:
    raw = os.environ.get("GEMINI_TIMEOUT_SECONDS", "20").strip()
    try:
        value = float(raw)
    except Exception:
        value = 20.0
    if value <= 0:
        value = 20.0
    return value


def _format_solution_lines(solutions: List[dict], default_source: str | None = None) -> List[str]:
    lines = []
    for idx, sol in enumerate(solutions, 1):
        cmd = (sol.get("command") or "").strip()
        desc = (sol.get("description") or "").strip()
        source = sol.get("source") or default_source
        prefix = f"[{source}] " if source else ""
        if cmd and desc:
            lines.append(f"{idx}. {prefix}{cmd} | {desc}")
        elif cmd:
            lines.append(f"{idx}. {prefix}{cmd}")
        elif desc:
            lines.append(f"{idx}. {prefix}{desc}")
    return lines


def _is_loopback_ip(value: str) -> bool:
    if not value:
        return False
    try:
        addr = value.split("%", 1)[0]
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


class _TTLCache:
    """In-process TTL cache safe for concurrent Flask request threads.

    All mutations are serialised through ``_lock`` so callers do not need
    to coordinate externally.
    """

    def __init__(self, max_entries: int, ttl_seconds: float) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._data: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple) -> object | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._data[key]
                return None
            return value

    def set(self, key: tuple, value: object) -> None:
        now = time.monotonic()
        with self._lock:
            self._data[key] = (now + self._ttl_seconds, value)
            if len(self._data) > self._max_entries:
                self._prune(now)

    def _prune(self, now: float) -> None:
        # Must be called with self._lock already held.
        expired_keys = [key for key, (expiry, _) in self._data.items() if expiry <= now]
        for key in expired_keys:
            del self._data[key]
        if len(self._data) <= self._max_entries:
            return
        overflow = len(self._data) - self._max_entries
        for key in list(self._data.keys())[:overflow]:
            del self._data[key]


_ISSUES_CACHE: dict[str, object] = {"mtime": None, "data": None}
_GEMINI_SUGGEST_CACHE = _TTLCache(max_entries=256, ttl_seconds=300)


def _gemini_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _suggest_cache_key(user_error: str, problem: str, existing: List[dict]) -> tuple:
    commands = [sol.get("command", "").strip() for sol in existing if sol.get("command")]
    return (
        _gemini_model_name(),
        _normalize_text(user_error),
        _normalize_text(problem),
        _hash_texts(commands),
    )


def _get_ai_mode() -> str:
    modes = ["semantic"]
    if _gemini_configured():
        modes.append("gemini")
    if _get_claude_client() is not None:
        modes.append("claude")
    return "+".join(modes)


def _get_gemini_model() -> tuple[object | None, str | None]:
    if genai is None:
        return None, "Gemini client library not installed"
    api_key = _get_gemini_api_key()
    if not api_key:
        return None, "Gemini API key not configured"

    model_name = _gemini_model_name()
    _log_gemini_debug(
        "initialize",
        {"model": model_name, "api_key_configured": True, "client_loaded": True},
    )
    global _GEMINI_MODEL, _GEMINI_MODEL_NAME
    # Fast path: return cached model without lock acquisition.
    if _GEMINI_MODEL is not None and _GEMINI_MODEL_NAME == model_name:
        return _GEMINI_MODEL, None

    with _GEMINI_MODEL_LOCK:
        # Re-check inside the lock to handle concurrent first callers.
        if _GEMINI_MODEL is None or _GEMINI_MODEL_NAME != model_name:
            genai.configure(api_key=api_key)
            _GEMINI_MODEL_NAME = model_name
            _GEMINI_MODEL = genai.GenerativeModel(model_name)
    return _GEMINI_MODEL, None


def _get_response_text(response: object, stage: str) -> str:
    try:
        text = response.text or ""
        if text:
            return text
    except Exception as exc:
        _log_gemini_debug(f"{stage} response_text_error", str(exc))

    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            reasons = [getattr(cand, "finish_reason", None) for cand in candidates]
            _log_gemini_debug(f"{stage} finish_reasons", reasons)
    except Exception as exc:
        _log_gemini_debug(f"{stage} response_meta_error", str(exc))

    try:
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            part_texts = []
            for part in parts:
                part_text = getattr(part, "text", None)
                if part_text:
                    part_texts.append(part_text)
            if part_texts:
                return "\n".join(part_texts)
    except Exception as exc:
        _log_gemini_debug(f"{stage} response_parse_error", str(exc))

    _log_gemini_debug(f"{stage} response_repr", repr(response))
    return ""


def _gemini_generate_content(
    model: object,
    prompt: str,
    generation_config: dict,
    stage: str,
) -> Tuple[object | None, str | None]:
    timeout_seconds = _gemini_timeout_seconds()

    def _call(config: dict) -> Tuple[object | None, str | None]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(model.generate_content, prompt, generation_config=config)
            try:
                return future.result(timeout=timeout_seconds), None
            except concurrent.futures.TimeoutError:
                return None, f"Gemini request timed out after {timeout_seconds}s"
            except Exception as exc:
                return None, str(exc)

    response, error = _call(generation_config)
    if error:
        _log_gemini_debug(f"{stage} request_error", error)
        fallback_config = {
            key: value
            for key, value in generation_config.items()
            if key not in {"response_mime_type", "response_schema"}
        }
        if fallback_config != generation_config:
            response, error = _call(fallback_config)
            if error:
                _log_gemini_debug(f"{stage} request_error", error)
                return None, f"Gemini request failed: {error}"
            return response, None
        return None, f"Gemini request failed: {error}"

    return response, None


def _safe_run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=5)
        return out.strip()
    except Exception:
        return None


def collect_system_info() -> dict:
    info: dict = {
        "ram": None,
        "disk": None,
        "battery": None,
        "network": None,
        "wifi": None,
        "processes": [],
    }

    # RAM
    if psutil:
        vm = psutil.virtual_memory()
        info["ram"] = {"percent": vm.percent, "used_gb": round(vm.used / (1024**3), 1), "total_gb": round(vm.total / (1024**3), 1)}
    # Disk
    if psutil:
        root_path = Path.home().anchor or "/"
        du = psutil.disk_usage(root_path)
        info["disk"] = {"percent": du.percent, "used_gb": round(du.used / (1024**3), 1), "total_gb": round(du.total / (1024**3), 1)}
    # Battery
    if psutil and hasattr(psutil, "sensors_battery"):
        batt = psutil.sensors_battery()
        if batt:
            info["battery"] = {"percent": batt.percent, "plugged": bool(batt.power_plugged)}
    # Network/WiFi
    if psutil:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        active = []
        for name, st in stats.items():
            if not st.isup:
                continue
            if "loopback" in name.lower():
                continue
            ips = []
            for addr in addrs.get(name, []):
                if getattr(addr, "family", None) not in {socket.AF_INET, socket.AF_INET6}:
                    continue
                if not addr.address or _is_loopback_ip(addr.address):
                    continue
                ips.append(addr.address)
            if ips:
                active.append({"interface": name, "speed": st.speed, "ips": ips})
        info["network"] = active
    # Wi-Fi SSID (Windows)
    if os.name == "nt":
        wifi_out = _safe_run(["netsh", "wlan", "show", "interfaces"])
        if wifi_out:
            ssid = None
            for line in wifi_out.splitlines():
                if "SSID" in line and "BSSID" not in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        ssid = parts[1].strip()
                        break
            if ssid:
                info["wifi"] = {"ssid": ssid}
    # Top processes
    if psutil:
        procs = []
        for p in psutil.process_iter(attrs=["pid", "name", "memory_percent", "cpu_percent"]):
            try:
                if p.info.get("pid") == 0:
                    continue
                if (p.info.get("name") or "").lower() == "system idle process":
                    continue
                p.info["cpu_percent"] = round(float(p.info.get("cpu_percent") or 0.0), 1)
                procs.append(p.info)
            except Exception:
                continue
        procs = sorted(
            procs,
            key=lambda x: (x.get("memory_percent", 0) or 0, x.get("cpu_percent", 0) or 0),
            reverse=True,
        )[:5]
        info["processes"] = procs

    return info


def _normalize_command(command: str) -> str:
    return " ".join(command.lower().split())


def _merge_solutions(primary: List[dict], extra: List[dict]) -> List[dict]:
    merged: List[dict] = []
    seen = set()
    for sol in primary + extra:
        cmd = (sol.get("command") or "").strip()
        desc = (sol.get("description") or "").strip()
        if not cmd and not desc:
            continue
        key = _normalize_command(cmd) if cmd else f"desc:{desc.lower()}"
        if key in seen:
            continue
        seen.add(key)
        entry = {"command": cmd, "description": desc}
        source = sol.get("source")
        if source:
            entry["source"] = source
        merged.append(entry)
    return merged


def _claude_model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")


def _get_claude_client():
    """Return a cached Anthropic client, constructing it at most once per process.

    Creating ``anthropic.Anthropic()`` on every request is wasteful — it sets up
    HTTP connection pools and reads environment state each time.  Using a module-
    level cached instance with double-checked locking is both correct under the
    GIL and avoids redundant object construction on every call.
    """
    global _CLAUDE_CLIENT
    if anthropic is None:
        return None
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    # Fast path: return already-constructed client without lock overhead.
    if _CLAUDE_CLIENT is not None:
        return _CLAUDE_CLIENT
    with _CLAUDE_CLIENT_LOCK:
        # Second check inside the lock handles concurrent first callers.
        if _CLAUDE_CLIENT is None:
            try:
                _CLAUDE_CLIENT = anthropic.Anthropic(api_key=api_key)
            except Exception:
                return None
    return _CLAUDE_CLIENT


def _extract_claude_text(message: object) -> str:
    content = getattr(message, "content", None) or []
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _clean_json_block(text: str) -> str:
    value = (text or "").strip()
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    if lines and lines[0].strip().lower() == "json":
        lines = lines[1:]
    return "\n".join(lines).strip()


def claude_verify_fixes(user_problem: str, matched_issue: str, solutions: List[dict], system_context: dict | None = None) -> List[dict]:
    if not solutions:
        return []
    client = _get_claude_client()
    if client is None:
        return solutions

    sol_lines = "\n".join(
        f"{index + 1}. Command: {sol.get('command', '')} | Description: {sol.get('description', '')}"
        for index, sol in enumerate(solutions)
    )

    # Build system context string if available
    sys_ctx = ""
    if system_context:
        ram = system_context.get("ram")
        disk = system_context.get("disk")
        ctx_parts = []
        if ram:
            ctx_parts.append(
                f"RAM usage: {ram.get('percent', '?')}% ({ram.get('used_gb', '?')} / {ram.get('total_gb', '?')} GB)"
            )
        if disk:
            ctx_parts.append(
                f"Disk usage: {disk.get('percent', '?')}% ({disk.get('used_gb', '?')} / {disk.get('total_gb', '?')} GB)"
            )
        if ctx_parts:
            sys_ctx = "Current system state: " + ", ".join(ctx_parts) + ".\n"

    prompt = (
        f'A Windows user reported this EXACT problem: "{user_problem}"\n\n'
        f'{sys_ctx}'
        f'Our knowledge base matched this to the issue category: "{matched_issue}"\n\n'
        f"Proposed fixes to evaluate:\n{sol_lines}\n\n"
        "Your tasks:\n"
        "1. Keep only fixes that are directly relevant and safe for this EXACT user problem. Remove anything unrelated or risky.\n"
        "2. Re-order fixes from MOST likely to succeed to least likely, considering the user's specific symptoms and system state.\n"
        "3. For each fix you keep, assign:\n"
        "   - confidence: exactly one of 'high', 'medium', or 'low' -- how likely this specific fix resolves this specific problem\n"
        "   - plain_english: 1-2 sentences explaining what the fix does for a non-technical user (no jargon)\n"
        "   - claude_note: '[High confidence] Plain English here.' -- combine confidence label + plain_english into one short note\n"
        "4. Return ONLY valid JSON -- no markdown fences, no extra text. Format:\n"
        '[{"command":"...","description":"...","source":"database","confidence":"high|medium|low","plain_english":"...","claude_note":"[Confidence] Plain explanation."}]\n'
        "If ALL proposed fixes are wrong or irrelevant, return exactly: []"
    )
    try:
        response = client.messages.create(
            model=_claude_model_name(),
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = _clean_json_block(_extract_claude_text(response))
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            return solutions
        verified: List[dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            command = (item.get("command") or "").strip()
            description = (item.get("description") or "").strip()
            if not command and not description:
                continue
            verified.append(
                {
                    "command": command,
                    "description": description,
                    "claude_note": (item.get("claude_note") or "").strip(),
                    "confidence": (item.get("confidence") or "medium").strip().lower(),
                    "plain_english": (item.get("plain_english") or "").strip(),
                    "source": item.get("source", "database"),
                }
            )
        return verified
    except Exception as exc:
        app.logger.error("Claude verify failed: %s", exc)
        return solutions


def claude_generate_fixes(user_problem: str, system_context: dict | None = None) -> List[dict]:
    client = _get_claude_client()
    if client is None:
        return []

    # Build system context string if available
    sys_ctx = ""
    if system_context:
        ram = system_context.get("ram")
        disk = system_context.get("disk")
        ctx_parts = []
        if ram:
            ctx_parts.append(
                f"RAM: {ram.get('percent', '?')}% used ({ram.get('used_gb', '?')} / {ram.get('total_gb', '?')} GB)"
            )
        if disk:
            ctx_parts.append(
                f"Disk: {disk.get('percent', '?')}% used ({disk.get('used_gb', '?')} / {disk.get('total_gb', '?')} GB)"
            )
        if ctx_parts:
            sys_ctx = "Current system state -- " + ", ".join(ctx_parts) + ".\n"

    prompt = (
        f'A Windows user has this specific problem: "{user_problem}"\n\n'
        f'{sys_ctx}'
        "No match was found in the knowledge base, so you must generate fixes from scratch.\n\n"
        "Generate up to 5 safe Windows troubleshooting fixes. Requirements:\n"
        "1. Order fixes from LEAST invasive to MOST invasive (e.g. restart a service before reinstalling software).\n"
        "2. Every command must be directly copy-pasteable into a Windows cmd.exe or PowerShell terminal -- no placeholders.\n"
        "3. For each fix, provide:\n"
        "   - step_by_step: a brief explanation of WHY this fix works for this specific problem (1-2 sentences)\n"
        "   - plain_english: what the command does in plain language a non-technical person can understand\n"
        "   - confidence: 'high', 'medium', or 'low'\n"
        "   - claude_note: '[Confidence] Plain English explanation.' combined into one note\n"
        "4. Only use standard cmd.exe or PowerShell built-in commands. No destructive or irreversible actions.\n"
        "5. Return ONLY valid JSON -- no markdown fences, no extra text. Format:\n"
        '[{"command":"actual runnable command","description":"what it does","source":"claude","confidence":"high|medium|low","plain_english":"...","step_by_step":"why this works","claude_note":"[Confidence] Plain explanation."}]\n'
        "If you cannot generate any safe relevant fix, return exactly: []"
    )
    try:
        response = client.messages.create(
            model=_claude_model_name(),
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = _clean_json_block(_extract_claude_text(response))
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            return []
        generated: List[dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            command = (item.get("command") or "").strip()
            description = (item.get("description") or "").strip()
            if not command and not description:
                continue
            generated.append(
                {
                    "command": command,
                    "description": description,
                    "source": item.get("source", "claude"),
                    "claude_note": (item.get("claude_note") or "").strip(),
                    "confidence": (item.get("confidence") or "medium").strip().lower(),
                    "plain_english": (item.get("plain_english") or "").strip(),
                    "step_by_step": (item.get("step_by_step") or "").strip(),
                }
            )
        return generated[:5]
    except Exception as exc:
        app.logger.error("Claude fallback failed: %s", exc)
        return []


def _gemini_suggest_commands(user_error: str, problem: str, existing: List[dict]) -> Tuple[List[dict], str | None]:
    model, err = _get_gemini_model()
    if err:
        _log_gemini_debug("suggest_commands model_error", err)
        return [], err

    cache_key = _suggest_cache_key(user_error, problem, existing)
    cached = _GEMINI_SUGGEST_CACHE.get(cache_key)
    if isinstance(cached, list):
        app.logger.info("ANALYZE: gemini suggest cache hit (%d)", len(cached))
        return cached, None

    existing_lines = "\n".join(
        f"- {sol.get('command', '').strip()}" for sol in existing if sol.get("command")
    )
    if not existing_lines:
        existing_lines = "- (none)"

    prompt = (
        "You are a Windows troubleshooting assistant.\n"
        "Return either:\n"
        "1) The exact text: I don't know\n"
        "2) Up to 5 lines, each line: <command> :: <description>\n"
        "No extra text. Use only cmd.exe or PowerShell commands.\n"
        "Use safe, common troubleshooting commands. Avoid destructive commands.\n"
        "Use placeholders like C:\\path\\to\\file if needed.\n\n"
        f"User issue: {user_error}\n"
        f"Selected problem statement: {problem}\n"
        "Existing database commands:\n"
        f"{existing_lines}\n"
    )
    _log_gemini_debug("suggest_commands prompt", prompt)

    response, request_error = _gemini_generate_content(
        model, prompt, {"temperature": 0.2, "max_output_tokens": 256}, "suggest_commands"
    )
    if request_error:
        return [], request_error

    response_text = _get_response_text(response, "suggest_commands")
    if not response_text:
        return [], "Gemini response was empty"
    _log_gemini_debug("suggest_commands response", response_text)
    if "i don't know" in response_text.lower():
        _log_gemini_debug("suggest_commands parsed", "I don't know")
        _GEMINI_SUGGEST_CACHE.set(cache_key, [])
        return [], None

    suggestions: List[dict] = []
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*\\d\\.\\)\\s]+", "", line).strip()
        if not line:
            continue
        if "::" in line:
            cmd_part, desc_part = line.split("::", 1)
        elif "|" in line:
            cmd_part, desc_part = line.split("|", 1)
        else:
            continue
        cmd = cmd_part.strip()
        desc = desc_part.strip()
        if not cmd or not desc:
            continue
        suggestions.append({"command": cmd, "description": desc, "source": "gemini"})
        if len(suggestions) >= 5:
            break

    _log_gemini_debug("suggest_commands parsed", suggestions)
    _GEMINI_SUGGEST_CACHE.set(cache_key, suggestions)
    return suggestions, None


def load_exec_log() -> List[dict]:
    try:
        from db import get_recent_command_logs  # type: ignore

        return get_recent_command_logs(limit=5000)
    except Exception:
        if not EXEC_LOG_PATH.exists():
            return []
        try:
            return json.loads(EXEC_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []


def save_exec_log(entries: List[dict]) -> None:
    try:
        from db import init_db as _db_init  # type: ignore

        _db_init()
        return
    except Exception:
        EXEC_LOG_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def log_execution(user_problem: str, matched_problem: str, description: str, command: str, result: dict) -> None:
    try:
        from db import log_command_execution  # type: ignore

        log_command_execution(
            user_problem=user_problem,
            matched_problem=matched_problem,
            description=description,
            command=command,
            result=result,
            session_id=request.remote_addr or "",
        )
        return
    except Exception:
        entries = load_exec_log()
        entries.append(
            {
                "timestamp": datetime.now().isoformat(),
                "user_problem": user_problem,
                "matched_problem": matched_problem,
                "solution_description": description,
                "command_executed": command,
                "execution_success": result.get("success", False),
                "return_code": result.get("return_code", -1),
                "output": result.get("output", ""),
                "error": result.get("error", ""),
                "simulation_mode": result.get("simulation_mode", False),
                "session_id": request.remote_addr,
            }
        )
        save_exec_log(entries)


def confidence_label(score: float) -> str:
    if score >= 0.9:
        return "> 90%"
    if score >= 0.8:
        return "> 80%"
    if score >= 0.7:
        return "> 70%"
    if score >= 0.6:
        return "> 60%"
    if score >= 0.5:
        return "> 50%"
    return "Low Confidence"


def _solution_success_rate_percent(command: str) -> int:
    try:
        from db import get_solution_success_rate  # type: ignore

        return int(get_solution_success_rate(command) * 100)
    except Exception:
        return 85


def _build_solution_payload(solutions_raw: List[dict]) -> List[dict]:
    return [
        {
            "id": index,
            "command": (sol.get("command") or "").strip(),
            "description": (sol.get("description") or "").strip(),
            "source": (sol.get("source") or "database").strip(),
            "claude_note": (sol.get("claude_note") or "").strip(),
            "confidence": (sol.get("confidence") or "").strip().lower(),
            "plain_english": (sol.get("plain_english") or "").strip(),
            "step_by_step": (sol.get("step_by_step") or "").strip(),
            "success_rate": _solution_success_rate_percent((sol.get("command") or "").strip()),
        }
        for index, sol in enumerate(solutions_raw)
    ]


def _analyze_issue_text(user_error: str, safety_toggle: bool) -> dict:
    issues = load_issues()
    if not issues:
        return {"error": "No issues database loaded", "status": 500}

    matches, found = find_matches(user_error, issues, top_k=5, threshold=0.35)
    claude_used = False
    fallback_used = False
    match_source = "none"
    score = 0.0

    if not found:
        app.logger.warning("ANALYZE: no semantic match, trying Claude fallback")
        matched_problem = "AI-generated (no database match)"
        solutions_raw = claude_generate_fixes(user_error, system_context=collect_system_info())
        claude_used = bool(solutions_raw)
        fallback_used = bool(solutions_raw)
    else:
        best, score = matches[0]
        matched_problem = (best.get("problem") or "").strip()
        solutions_raw = list(best.get("solutions", []))
        match_source = "semantic"
        app.logger.info("ANALYZE: semantic match selected: %s", matched_problem)

        if safety_toggle and solutions_raw:
            app.logger.info("ANALYZE: safety toggle on, verifying fixes with Claude")
            verified = claude_verify_fixes(user_error, matched_problem, solutions_raw, system_context=collect_system_info())
            if verified:
                solutions_raw = verified
            claude_used = _get_claude_client() is not None

        if _gemini_configured():
            gemini_solutions, gemini_error = _gemini_suggest_commands(
                user_error, matched_problem, solutions_raw
            )
            if gemini_error:
                app.logger.error("Gemini suggestions failed: %s", gemini_error)
            else:
                solutions_raw = _merge_solutions(solutions_raw, gemini_solutions)

    if not solutions_raw:
        return {"error": "No matching solution found", "status": 404}

    solution_payload = _build_solution_payload(solutions_raw)
    return {
        "status": 200,
        "problem": matched_problem,
        "score": score,
        "match_source": match_source,
        "claude_used": claude_used,
        "fallback_used": fallback_used,
        "solutions_raw": solutions_raw,
        "solutions": solution_payload,
    }


@app.route("/")
def home():
    return render_template("enhanced_index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True)
    user_error = (data.get("error") or "").strip()
    if not user_error:
        return jsonify({"error": "No error message provided"}), 400
    safety_toggle = bool(data.get("safety_toggle", False))
    result = _analyze_issue_text(user_error, safety_toggle)
    status = int(result.get("status", 500))
    if status != 200:
        return jsonify({"error": result.get("error", "Analysis failed")}), status

    session["matched_issue"] = {
        "problem": result.get("problem", ""),
        "solutions": result.get("solutions_raw", []),
    }
    session["user_error"] = user_error
    session["confidence"] = float(result.get("score", 0.0))

    solutions = result.get("solutions", [])
    return jsonify(
        {
            "success": True,
            "problem": result.get("problem", ""),
            "confidence_display": confidence_label(float(result.get("score", 0.0))),
            "user_error": user_error,
            "solutions": solutions,
            "solution_count": len(solutions),
            "match_source": result.get("match_source", "none"),
            "claude_used": bool(result.get("claude_used")),
            "fallback_used": bool(result.get("fallback_used")),
        }
    )


@app.route("/analyze-image", methods=["POST"])
def analyze_image():
    if "image" not in request.files:
        return jsonify({"error": "No image provided"}), 400

    image_file = request.files["image"]
    image_bytes = image_file.read()
    if not image_bytes:
        return jsonify({"error": "Empty image"}), 400

    if genai is None:
        return jsonify({"error": "Gemini client library not installed"}), 503
    api_key = _get_gemini_api_key()
    if not api_key:
        return jsonify({"error": "Gemini API key not configured"}), 503

    _ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
    mime_type = (image_file.content_type or "").strip().lower()
    if mime_type not in _ALLOWED_IMAGE_MIMES:
        return jsonify({"error": f"Unsupported file type '{mime_type}'. Upload PNG, JPG, or WEBP."}), 415
    genai.configure(api_key=api_key)
    vision_model = genai.GenerativeModel(_gemini_model_name())
    extract_prompt = (
        "Read the screenshot and extract only the main Windows error message/code. "
        "Return a single plain text sentence. "
        "If no usable error text is visible, return exactly: no error detected"
    )
    image_part = {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("utf-8")}
    try:
        response = vision_model.generate_content([extract_prompt, image_part])
        extracted = _get_response_text(response, "analyze_image").strip()
    except Exception as exc:
        return jsonify({"error": f"Vision failed: {exc}"}), 500

    if not extracted or "no error detected" in extracted.lower():
        return jsonify({"error": "No error found in screenshot"}), 404

    safety_toggle = (request.form.get("safety_toggle", "false").lower() == "true")
    result = _analyze_issue_text(extracted, safety_toggle)
    status = int(result.get("status", 500))
    if status != 200:
        return jsonify({"error": result.get("error", "Analysis failed"), "extracted_text": extracted}), status

    session["matched_issue"] = {
        "problem": result.get("problem", ""),
        "solutions": result.get("solutions_raw", []),
    }
    session["user_error"] = extracted
    session["confidence"] = float(result.get("score", 0.0))

    solutions = result.get("solutions", [])
    return jsonify(
        {
            "success": True,
            "extracted_text": extracted,
            "problem": result.get("problem", ""),
            "confidence_display": confidence_label(float(result.get("score", 0.0))),
            "solutions": solutions,
            "solution_count": len(solutions),
            "match_source": "vision+" + str(result.get("match_source", "none")),
            "claude_used": bool(result.get("claude_used")),
            "fallback_used": bool(result.get("fallback_used")),
        }
    )


@app.route("/feedback", methods=["POST"])
def feedback():
    data = request.get_json(force=True)
    result = (data.get("result") or "unknown").strip()
    solution_command = (data.get("solution_command") or "").strip()
    solution_description = (data.get("solution_description") or "").strip()
    if not solution_command:
        return jsonify({"error": "solution_command required"}), 400

    matched = session.get("matched_issue") or {}
    user_error = session.get("user_error", "")
    matched_problem = matched.get("problem", "")

    try:
        from db import log_fix_outcome, get_solution_success_rate  # type: ignore

        log_fix_outcome(
            user_problem=user_error,
            matched_problem=matched_problem,
            solution_command=solution_command,
            solution_description=solution_description,
            result=result,
        )

        # Send a lightweight Hush Chain event in background context.
        hush_result = safe_send_hush_metric(
            "fix_outcome",
            {
                "user_problem": user_error,
                "matched_problem": matched_problem,
                "solution_command": solution_command,
                "solution_description": solution_description,
                "result": result,
            },
        )

        new_rate = int(get_solution_success_rate(solution_command) * 100)
        response_payload = {
            "success": True,
            "new_success_rate": new_rate,
            "message": f"Thanks! Success rate updated to {new_rate}%",
            "hush": hush_result,
        }
        return jsonify(response_payload)
    except Exception as exc:
        return jsonify({"error": f"feedback unavailable: {exc}"}), 500


@app.route("/execute", methods=["POST"])
def execute():
    data = request.get_json(force=True)
    sid = int(data.get("solution_id", 0))
    matched = session.get("matched_issue") or {}
    user_error = session.get("user_error", "")
    solutions = matched.get("solutions", [])
    if not solutions or sid >= len(solutions):
        return jsonify({"error": "No pending solutions or invalid ID"}), 400

    if not is_admin():
        return jsonify({"error": "This action requires Administrator privileges. Please run app.py as Administrator."}), 403

    sol = solutions[sid]
    cmd = sol.get("command", "")
    desc = sol.get("description", "")

    if not _is_windows():
        result = _windows_only_execution_error(cmd, desc, sid)
        log_execution(user_error, matched.get("problem", ""), desc, cmd, result)
        return jsonify(result), 400

    app.logger.info("APPLY: command: %s", cmd)
    try:
        _result = _run_safe_command(cmd)
        result = {**_result, "command": cmd, "solution_id": sid, "description": desc}
    except ValueError as e:
        result = {
            "success": False, "output": "", "error": str(e), "return_code": -1,
            "command": cmd, "simulation_mode": False, "solution_id": sid, "description": desc,
        }
    except subprocess.TimeoutExpired:
        result = {
            "success": False, "output": "", "error": "Command timed out (30s)", "return_code": -1,
            "command": cmd, "simulation_mode": False, "solution_id": sid, "description": desc,
        }
    except Exception as e:
        result = {
            "success": False, "output": "", "error": str(e), "return_code": -1,
            "command": cmd, "simulation_mode": False, "solution_id": sid, "description": desc,
        }

    if result.get("success"):
        output = result.get("output") or ""
        if output:
            app.logger.info("APPLY: output: %s", output)
        else:
            app.logger.info("APPLY: output: Successfully executed (no output)")
    else:
        app.logger.error("APPLY: error: %s", result.get("error") or "Unknown error")

    log_execution(user_error, matched.get("problem", ""), desc, cmd, result)
    return jsonify(result)


@app.route("/execute-all", methods=["POST"])
def execute_all():
    matched = session.get("matched_issue") or {}
    user_error = session.get("user_error", "")
    solutions = matched.get("solutions", [])
    if not solutions:
        return jsonify({"error": "No pending solutions"}), 400

    if not is_admin():
        return jsonify({"error": "This action requires Administrator privileges. Please run app.py as Administrator."}), 403

    results = []
    overall = True
    for i, sol in enumerate(solutions):
        cmd = sol.get("command", "")
        desc = sol.get("description", "")

        if not _is_windows():
            res = _windows_only_execution_error(cmd, desc, i)
            results.append(res)
            overall = False
            log_execution(user_error, matched.get("problem", ""), desc, cmd, res)
            continue

        app.logger.info("APPLY: command: %s", cmd)
        try:
            _res_data = _run_safe_command(cmd)
            res = {**_res_data, "command": cmd, "description": desc, "solution_id": i}
        except ValueError as e:
            res = {
                "success": False, "output": "", "error": str(e), "return_code": -1,
                "command": cmd, "simulation_mode": False, "description": desc, "solution_id": i,
            }
        except subprocess.TimeoutExpired:
            res = {
                "success": False, "output": "", "error": "Command timed out (30s)", "return_code": -1,
                "command": cmd, "simulation_mode": False, "description": desc, "solution_id": i,
            }
        except Exception as e:
            res = {
                "success": False, "output": "", "error": str(e), "return_code": -1,
                "command": cmd, "simulation_mode": False, "description": desc, "solution_id": i,
            }
        if not res["success"]:
            overall = False
            app.logger.error("APPLY: error: %s", res.get("error") or "Unknown error")
        else:
            output = res.get("output") or ""
            if output:
                app.logger.info("APPLY: output: %s", output)
            else:
                app.logger.info("APPLY: output: Successfully executed (no output)")
        results.append(res)
        log_execution(user_error, matched.get("problem", ""), desc, cmd, res)

    return jsonify({
        "success": overall,
        "results": results,
        "total_solutions": len(results),
        "successful_solutions": sum(1 for r in results if r.get("success")),
    })


@app.route("/logs")
def logs():
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        from db import get_recent_command_logs  # type: ignore

        entries = get_recent_command_logs(limit=limit)
    except Exception:
        entries = load_exec_log()
        entries = sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
    return jsonify({"success": True, "logs": entries, "total_count": len(entries)})


@app.route("/stats")
def stats():
    try:
        from db import get_stats as db_get_stats, get_top_problems, get_recent_24h_count  # type: ignore

        base = db_get_stats()
        top = get_top_problems(limit=5)
        total = int(base.get("total_executions", 0))
        success = int(base.get("successful_executions", 0))
        rate = float(base.get("success_rate", 0))
    except Exception:
        entries = load_exec_log()
        total = len(entries)
        success = sum(1 for e in entries if e.get("execution_success"))
        rate = round((success / total * 100) if total else 0, 1)
        top = []
    return jsonify({
        "success": True,
        "stats": {
            "total_executions": total,
            "successful_executions": success,
            "success_rate": rate,
            "recent_activity_24h": get_recent_24h_count(),
            "most_common_problems": top,
            "ai_mode": _get_ai_mode(),
        },
    })


@app.route("/hush/status")
def hush_status_route():
    try:
        status = hush_status()
        return jsonify({"success": True, "hush_status": status})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "mode": _get_ai_mode(),
        "ai_components": "loaded",
        "active_plugin": "semantic-engine"
    })


@app.route("/system-info")
def system_info():
    return jsonify({"success": True, "platform": platform.system(), "data": collect_system_info()})


# Plugin stubs to satisfy UI calls
@app.route("/plugins")
def plugins():
    claude_enabled = _get_claude_client() is not None
    return jsonify({
        "success": True,
        "status": "loaded",
        "message": "Core AI components are active.",
        "active_plugin": "semantic-engine",
        "ai_components": {
            "semantic_search": "sentence-transformers/all-MiniLM-L6-v2 (local)",
            "vision": f"{_gemini_model_name()} (configured={_gemini_configured()})",
            "claude_safety": f"{_claude_model_name()} (configured={claude_enabled})",
            "fallback": "Claude generated fixes when semantic match is low confidence",
        },
    })


@app.route("/plugins/switch", methods=["POST"])
def plugins_switch():
    return jsonify(
        {
            "success": False,
            "message": "Plugin switching is disabled. The semantic engine is fixed for this build.",
            "active_plugin": "semantic-engine",
        }
    ), 400


@app.route("/plugins/benchmark", methods=["POST"])
def plugins_benchmark():
    return jsonify(
        {
            "success": False,
            "message": "Synthetic plugin benchmarks removed; use /stats and /logs for real runtime data.",
            "active_plugin": "semantic-engine",
        }
    ), 400


if __name__ == "__main__":
    # Elevate on Windows if not already admin
    if os.name == "nt":
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                # Relaunch with admin privileges
                script = f'"{Path(__file__).resolve()}"'
                params = script
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, params, None, 1
                )
                raise SystemExit(0)
        except Exception:
            pass

    issues = load_issues()
    if callable(init_db):
        init_db()
    precompute(issues)
    print(f"Embeddings ready for {len(issues)} issues.")

    debug_mode = _debug_mode()
    _setup_console_hotkey()
    if _should_open_browser_now(debug_mode):
        _open_browser_delayed()

    app.run(host="0.0.0.0", port=5050, debug=debug_mode, use_reloader=debug_mode)
