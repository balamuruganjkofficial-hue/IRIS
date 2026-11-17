"""Iris doctor: a plain-text, read-only health report.

Prints to the console and saves logs/doctor.txt. Every reported line starts
with OK, WARN or FAIL and carries a one-line fix hint. No API key value is ever
printed: only presence (yes/no). All network and device probes are bounded by
short timeouts and a total time budget, so the report also works offline.
"""

import importlib
import importlib.metadata
import math
import os
import platform
import re
import socket
import ssl
import sys
import threading
import time
from collections import deque, namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
REPORT = LOGS / "doctor.txt"

BUDGET = 22.0                    # hard wall-clock budget, well under 30 s
MIC_SECONDS = 2.0
HOSTS = (
    ("Groq", "api.groq.com"),
    ("OpenRouter", "openrouter.ai"),
    ("Edge TTS", "speech.platform.bing.com"),
    ("DDG web", "html.duckduckgo.com"),
    ("DDG news", "duckduckgo.com"),
)
DOWNLOAD_URL = "https://www.google.com/favicon.ico"
VOSK_DEFAULT = "data/vosk/vosk-model-small-en-us-0.15"

Check = namedtuple("Check", "status name detail hint")
Probe = namedtuple("Probe", "value error seconds")

_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-|gsk_|ghp_|bearer\s+)[A-Za-z0-9._-]{4,}"
    r"|\b(?:api[_-]?key|token|secret)\s*[:=]\s*\S+")

# Match the log *level*, not the word ERROR appearing inside an INFO message.
_LEVEL_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[ T][0-9:,.]+\s+)?(ERROR|WARNING)\b", re.I)


# ------------------------------------------------------------------ formatting
def ascii_text(value):
    """Force plain ASCII so cp1252 consoles and doctor.txt never choke."""
    return str(value or "").encode("ascii", "replace").decode("ascii")


def scrub(value):
    """Remove anything that looks like a credential and flatten to one line."""
    text = ascii_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    return _SECRET_RE.sub("[redacted]", text)


def check(status, name, detail, hint="none"):
    return Check(status, ascii_text(name), scrub(detail), scrub(hint))


def ok(name, detail, hint="none"):
    return check("OK", name, detail, hint)


def warn(name, detail, hint):
    return check("WARN", name, detail, hint)


def fail(name, detail, hint):
    return check("FAIL", name, detail, hint)


def format_line(item):
    """One check per line: 'OK   name  detail | fix: hint'."""
    return "{0:<4} {1:<14} {2} | fix: {3}".format(
        item.status, ascii_text(item.name)[:14],
        ascii_text(item.detail)[:190], ascii_text(item.hint)[:110])


def format_report(checks, elapsed):
    """Return the complete report, including a status-coded summary line."""
    items = list(checks or [])
    counts = {s: sum(1 for c in items if c.status == s)
              for s in ("OK", "WARN", "FAIL")}
    overall = ("FAIL" if counts["FAIL"] else "WARN" if counts["WARN"] else "OK")
    items.append(check(
        overall, "Summary",
        "{0} checks: {1} OK, {2} WARN, {3} FAIL in {4:.1f}s".format(
            len(checks or []), counts["OK"], counts["WARN"], counts["FAIL"],
            elapsed),
        "fix the WARN and FAIL lines above" if counts["WARN"] or counts["FAIL"]
        else "none"))
    return "\n".join(format_line(c) for c in items) + "\n"


# ----------------------------------------------------------------- timeouts
def brief_exception(exc):
    """Short, secret-free category for an exception. Never its message."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    name = type(exc).__name__
    if isinstance(exc, socket.gaierror):
        return "dns failed"
    if "RateLimit" in name or code in (402, 429):
        return "rate limited (429)"
    if "Authentication" in name or "Permission" in name or code in (401, 403):
        return "key rejected (401)"
    if "Timeout" in name:
        return "timeout"
    if "Connection" in name:
        return "connection failed"
    if code:
        return "http {0}".format(code)
    return "error"


def probe_many(tasks, timeout):
    """Run (name, function) pairs in daemon threads with one shared deadline.

    A stuck function is reported as 'timeout' and never blocks the caller.
    """
    results, workers = {}, []
    deadline = time.monotonic() + max(0.01, float(timeout))
    for name, fn in tasks:
        box = {}

        def target(fn=fn, box=box):
            started = time.monotonic()
            try:
                box["value"] = fn()
                box["error"] = None
            except BaseException as exc:  # noqa: BLE001 - report, never raise
                box["value"] = None
                box["error"] = brief_exception(exc)
            box["seconds"] = time.monotonic() - started

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        workers.append((name, thread, box))

    for _, thread, _ in workers:
        thread.join(max(0.0, deadline - time.monotonic()))

    for name, thread, box in workers:
        if thread.is_alive() or "error" not in box:
            results[name] = Probe(None, "timeout", float(timeout))
        else:
            results[name] = Probe(box.get("value"), box["error"],
                                  box["seconds"])
    return results


def probe(fn, timeout):
    return probe_many([("probe", fn)], timeout)["probe"]


# ---------------------------------------------------------------- environment
def check_python():
    version = platform.python_version()
    if sys.version_info < (3, 11):
        return fail("Python", "version {0}".format(version),
                    "install Python 3.11 or newer")
    return ok("Python", "version {0}".format(version))


def check_venv():
    if sys.prefix != sys.base_prefix:
        return ok("Venv", "virtual environment active")
    return warn("Venv", "running under the system interpreter",
                "start with venv\\Scripts\\python.exe main.py")


def installed_version(distribution):
    try:
        return importlib.metadata.version(distribution)
    except Exception:  # noqa: BLE001 - missing metadata is a normal result
        return None


def check_requirements(path=None, version_fn=installed_version):
    """Compare installed distributions with requirements.txt."""
    path = Path(path or (ROOT / "requirements.txt"))
    if not path.exists():
        return [fail("Packages", "requirements.txt is missing",
                     "restore requirements.txt from the project")]
    required = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)", line)
        if match:
            required.append((match.group(1), match.group(2)))
    if not required:
        return [warn("Packages", "no pinned packages found",
                     "restore requirements.txt from the project")]

    lines = []
    for name, want in required:
        try:
            got = version_fn(name)
        except Exception:  # noqa: BLE001 - injected test function may fail
            got = None
        if got is None:
            lines.append(fail("pkg " + name, "{0} is not installed (want {1})".format(
                name, want), "pip install -r requirements.txt"))
        elif str(got) != want:
            lines.append(warn("pkg " + name, "{0} {1}, want {2}".format(
                name, got, want), "pip install -r requirements.txt"))
        else:
            lines.append(ok("pkg " + name, "{0} {1}".format(name, got)))
    return lines


def missing_keys(data, defaults, prefix=""):
    """Dotted keys present in defaults but absent from the parsed config."""
    out = []
    for key, value in (defaults or {}).items():
        dotted = prefix + "." + key if prefix else key
        if not isinstance(data, dict) or key not in data:
            out.append(dotted)
        elif isinstance(value, dict) and isinstance(data[key], dict):
            out.extend(missing_keys(data[key], value, dotted))
    return out


def check_config(path=None, defaults=None):
    path = Path(path or (ROOT / "config.yaml"))
    if not path.exists():
        return [fail("Config", "config.yaml is missing",
                     "restore config.yaml from the project")]
    try:
        import yaml
    except Exception:  # noqa: BLE001
        return [fail("Config", "PyYAML is not installed",
                     "pip install -r requirements.txt")]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a bad config must not crash
        return [fail("Config", "invalid YAML ({0})".format(brief_exception(exc)),
                     "fix the syntax in config.yaml")]
    if not isinstance(data, dict):
        return [fail("Config", "top level is not a mapping",
                     "rewrite config.yaml with key: value pairs")]
    if defaults is None:
        try:
            from core.config import DEFAULTS as defaults
        except Exception:  # noqa: BLE001 - keep going with no comparison
            defaults = {}
    if not defaults:
        return [warn("Config", "valid YAML; defaults could not be loaded",
                     "check the core package installation")]
    absent = missing_keys(data, defaults)
    if absent:
        return [warn("Config", "valid; missing keys: " + ", ".join(absent[:8]),
                     "add the missing keys to config.yaml (defaults still apply)")]
    return [ok("Config", "valid; all expected keys present")]


# ------------------------------------------------------------------- hardware
def _mic_scan():
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        inputs, names = 0, []
        for index in range(max(0, pa.get_device_count())):
            info = pa.get_device_info_by_index(index)
            if int(info.get("maxInputChannels") or 0) > 0:
                inputs += 1
                if len(names) < 3:
                    names.append(scrub(info.get("name")))
        try:
            default = scrub(pa.get_default_input_device_info().get("name"))
        except Exception:  # noqa: BLE001 - no default input is reportable
            default = "none"
        return inputs, ", ".join(names), default
    finally:
        pa.terminate()


def _mic_level():
    import array
    import pyaudio
    pa = pyaudio.PyAudio()
    stream = None
    try:
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                         input=True, frames_per_buffer=1280)
        end = time.monotonic() + MIC_SECONDS
        values = []
        while time.monotonic() < end:
            data = stream.read(1280, exception_on_overflow=False)
            samples = array.array("h")
            samples.frombytes(data[:2560])
            if sys.byteorder != "little":
                samples.byteswap()
            if not samples:
                continue
            values.append(math.sqrt(
                sum(sample * sample for sample in samples) / len(samples)))
        return values
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:  # noqa: BLE001
            pass
        pa.terminate()


def check_mic(threshold=250.0):
    lines = []
    devices = probe(_mic_scan, 2.0)
    if devices.error or devices.value is None:
        lines.append(fail("Mic devices", "cannot list audio devices: " +
                          str(devices.error), "check microphone permissions"))
    else:
        count, names, default = devices.value
        if not count:
            lines.append(fail("Mic devices", "no input devices found",
                              "connect a microphone and allow app access"))
        else:
            lines.append(ok("Mic devices", "{0} input(s): {1}; default {2}".format(
                count, names, default)))

    level = probe(_mic_level, MIC_SECONDS + 1.5)
    if level.error or not level.value:
        lines.append(fail("Mic level", "2 s level test failed: " +
                          str(level.error), "check Windows microphone access"))
        return lines
    values = sorted(level.value)
    low, avg, high = values[0], sum(values) / len(values), values[-1]
    floor = values[len(values) // 4]
    ratio = high / max(floor, 1.0)
    separable = ratio >= 3.0 and high >= float(threshold)
    detail = ("2 s RMS min {0:.0f} avg {1:.0f} max {2:.0f}; floor {3:.0f}; "
              "peak {4:.0f}x; separable {5}".format(
                  low, avg, high, floor, ratio, "yes" if separable else "no"))
    if separable:
        lines.append(ok("Mic level", detail))
    elif high < float(threshold):
        lines.append(warn("Mic level", detail + "; no speech heard",
                          "say 'Iris Iris' during the next 2 s test"))
    else:
        lines.append(warn("Mic level", detail,
                          "lower voice.vad_min_rms or reduce background noise"))
    return lines


def check_beep():
    try:
        import winsound
    except Exception:  # noqa: BLE001 - non-Windows has no SystemBeep
        return warn("Speaker", "no system beep API on this platform",
                    "use the interface chime instead")
    result = probe(lambda: winsound.Beep(880, 80), 1.5)
    if result.error:
        return warn("Speaker", "test beep failed: " + str(result.error),
                    "check audio output device or volume")
    return ok("Speaker", "880 Hz beep played for 80 ms")


# -------------------------------------------------------------------- network
def _host_probe(host):
    out = {}
    started = time.monotonic()
    infos = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
    out["dns"] = (time.monotonic() - started) * 1000
    first = None
    for info in infos[:4]:
        family, socktype, proto, _, address = info
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(1.5)
            tick = time.monotonic()
            raw.connect(address)
            out["tcp"] = (time.monotonic() - tick) * 1000
            tick = time.monotonic()
            tls = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host)
            out["tls"] = (time.monotonic() - tick) * 1000
            tls.close()
            raw = None
            break
        except OSError as exc:
            if first is None:
                first = exc
        finally:
            if raw is not None:
                raw.close()
    if "tls" not in out:
        raise first or OSError("connect failed")
    return out


def _download_probe():
    import requests
    started = time.monotonic()
    response = requests.get(DOWNLOAD_URL, timeout=2.5,
                            headers={"User-Agent": "Iris-doctor/1.0"})
    size = len(response.content or b"")
    if not size:
        raise OSError("empty response")
    return size, time.monotonic() - started


def check_network(hosts=HOSTS, timeout=4.5, download_timeout=3.0):
    lines = []
    results = probe_many([(label, lambda h=host: _host_probe(h))
                          for label, host in hosts], timeout)
    for label, host in hosts:
        result = results.get(label, Probe(None, "timeout", timeout))
        if result.error or result.value is None:
            lines.append(fail("Net " + label,
                              "{0}: {1}".format(host, result.error),
                              "check internet/proxy; local skills still work"))
        else:
            data = result.value
            lines.append(ok("Net " + label,
                            "{0} dns {1:.0f}ms tcp {2:.0f}ms tls {3:.0f}ms".format(
                                host, data.get("dns", 0), data.get("tcp", 0),
                                data.get("tls", 0))))
    download = probe(_download_probe, download_timeout)
    if download.error or not download.value:
        lines.append(fail("Net download", "small file failed: " +
                          str(download.error), "check internet/proxy or DNS"))
    else:
        size, seconds = download.value
        lines.append(ok("Net download", "{0} B in {1:.0f} ms ({2:.0f} KB/s)".format(
            size, seconds * 1000, size / max(seconds, 0.001) / 1024)))
    return lines


# -------------------------------------------------------------- model / keys
def check_providers(config=None, timeout=6.0):
    """Key presence plus one 1-token call per configured free provider."""
    try:
        from core import brain
    except Exception:  # noqa: BLE001
        return [fail("Models", "model chain could not be imported",
                     "pip install -r requirements.txt")]

    lines = []
    for provider, meta in (getattr(brain, "_PROVIDERS", {}) or {}).items():
        if provider == "ollama":
            continue
        try:
            present = bool(brain._key(provider))
        except Exception:  # noqa: BLE001
            present = False
        if present:
            lines.append(ok("Key " + provider, "yes"))
        else:
            names = "/".join(meta.get("env") or ())
            lines.append(warn("Key " + provider, "no",
                              "add {0} to .env for its free tier".format(names)))

    try:
        chain = probe(lambda: brain.chain(), 5.0).value or []
    except Exception:  # noqa: BLE001
        chain = []
    models = {}
    for provider, model in chain:
        models.setdefault(provider, model)
    if not models and (config or {}).get("models", {}).get("llm"):
        models["groq"] = config["models"]["llm"]
    if not models:
        lines.append(warn("Models", "no free provider is configured",
                          "add GROQ_API_KEY to .env (free tier)"))
        return lines

    tasks = []
    for provider, model in models.items():
        def call(p=provider, m=model):
            client = brain._client(p)
            client.chat.completions.create(
                model=m, messages=[{"role": "user", "content": "hi"}],
                max_tokens=1, timeout=3.0)
            return True
        tasks.append((provider, call))
    results = probe_many(tasks, timeout)
    hints = {
        "rate limited (429)": "free tier is rate limited; retry in a few seconds",
        "key rejected (401)": "replace the key in .env with a valid free-tier key",
        "timeout": "check network/proxy for this provider",
        "connection failed": "check network/proxy for this provider",
    }
    for provider in models:
        result = results.get(provider, Probe(None, "timeout", timeout))
        if result.error:
            lines.append(warn("API " + provider,
                              "1-token call failed: " + str(result.error),
                              hints.get(str(result.error),
                                        "check the provider status page")))
        else:
            lines.append(ok("API " + provider, "1-token call in {0:.1f}s".format(
                result.seconds)))
    return lines


def check_vosk(config=None):
    config = config or {}
    voice = config.get("voice") or {}
    raw = str(voice.get("vosk_model") or VOSK_DEFAULT)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    ready = (path / "am" / "final.mdl").exists() or (path / "conf").exists()
    if ready:
        shown = str(path.relative_to(ROOT)) if str(path).startswith(str(ROOT)) else str(path)
        return ok("Vosk model", "present in {0}".format(shown))
    engine = str(voice.get("wake_engine") or "auto").lower()
    if engine == "openwakeword":
        return warn("Vosk model", "missing, but wake_engine is openwakeword",
                    "check the openwakeword model if 'Iris' is not heard")
    return fail("Vosk model", "missing at {0}".format(str(path.name)),
                "run Iris once online to download the free 41 MB model")


# ------------------------------------------------------------------- browser
def check_browser():
    try:
        spec = importlib.util.find_spec("playwright")
    except Exception:  # noqa: BLE001
        spec = None
    lines = []
    if spec is None:
        lines.append(fail("Playwright", "package is not installed",
                          "pip install -r requirements.txt"))
    else:
        lines.append(ok("Playwright", "package is importable"))

    cache = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH") or
                 (Path.home() / "AppData" / "Local" / "ms-playwright"))
    chromium = cache.exists() and any(
        p.name.startswith("chromium-") for p in cache.iterdir())
    roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"),
             os.getenv("LOCALAPPDATA")]
    chrome = any(p and (Path(p) / "Google" / "Chrome" / "Application" /
                        "chrome.exe").exists() for p in roots)
    if chrome or chromium:
        lines.append(ok("Chrome", "Chrome or bundled Chromium is installed"))
    else:
        lines.append(warn("Chrome", "no Chrome/Chromium found",
                          "python -m playwright install chromium"))
    return lines


# ----------------------------------------------------------------- resources
def check_resources():
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return [fail("Resources", "psutil is not installed",
                     "pip install -r requirements.txt")]
    lines = []
    ram = psutil.virtual_memory().available / (1024.0 ** 3)
    status = "FAIL" if ram < 0.5 else "WARN" if ram < 1.0 else "OK"
    lines.append(check(status, "RAM", "free {0:.1f} GB".format(ram),
                       "close heavy apps to free memory" if status != "OK"
                       else "none"))
    free = psutil.disk_usage(str(ROOT)).free / (1024.0 ** 3)
    status = "FAIL" if free < 1.0 else "WARN" if free < 5.0 else "OK"
    lines.append(check(status, "Disk", "free {0:.1f} GB on {1}".format(
        free, ROOT.drive or ROOT), "delete old files to free space"
        if status != "OK" else "none"))
    return lines


# ----------------------------------------------------------------------- logs
def check_logs(path=None, limit=20):
    path = Path(path or (LOGS / "iris.log"))
    if not path.exists():
        return [warn("iris.log", "no log file yet", "run Iris once to create it")]
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            recent = deque(handle, maxlen=400)
    except OSError as exc:
        return [warn("iris.log", "cannot read log: " + brief_exception(exc),
                     "check file permissions on logs/iris.log")]

    hits = []
    for raw in recent:
        match = _LEVEL_RE.match(raw)
        if match:
            hits.append(("FAIL" if match.group(1).upper() == "ERROR" else "WARN",
                         raw))
    hits = hits[-limit:]
    if not hits:
        return [ok("iris.log", "no ERROR/WARNING lines in the last 400")]
    lines = []
    for index, (status, raw) in enumerate(hits, 1):
        lines.append(check(status, "log {0:02d}".format(index), scrub(raw)[:190],
                           "see logs/iris.log for the full context"))
    return lines


# ---------------------------------------------------------------------- driver
def load_config():
    """Parse config.yaml directly so a broken file cannot crash the doctor."""
    try:
        import yaml
        path = ROOT / "config.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def run_checks(config=None, deadline=None):
    """Build the check list. Never raises; heavy work is bounded by deadline."""
    config = load_config() if config is None else config
    start = time.monotonic()
    end = deadline if deadline is not None else start + BUDGET
    checks = []

    def add(*items):
        checks.extend(item for item in items if item is not None)

    def room(seconds):
        return time.monotonic() + seconds < end

    add(check_python(), check_venv())
    add(*check_config())
    add(*check_requirements())

    if room(MIC_SECONDS + 3.5):
        add(*check_mic(float((config.get("voice") or {}).get("vad_min_rms", 250))))
    else:
        add(warn("Mic", "skipped: time budget exhausted", "rerun with --doctor"))
    if room(1.5):
        add(check_beep())
    else:
        add(warn("Speaker", "skipped: time budget exhausted", "rerun with --doctor"))

    if room(5.0):
        add(*check_network())
    else:
        add(warn("Network", "skipped: time budget exhausted", "rerun with --doctor"))
    if room(6.5):
        add(*check_providers(config))
    else:
        add(warn("Models", "skipped: time budget exhausted", "rerun with --doctor"))

    add(check_vosk(config))
    add(*check_browser())
    add(*check_resources())
    add(*check_logs())
    return checks, time.monotonic() - start


def main(argv=None):
    start = time.monotonic()
    try:
        checks, elapsed = run_checks()
    except BaseException as exc:  # noqa: BLE001 - a doctor must never traceback
        checks = [fail("Doctor", "report interrupted: " + brief_exception(exc),
                       "rerun with --console --doctor")]
        elapsed = time.monotonic() - start
    text = format_report(checks, elapsed)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(ascii_text(text), encoding="ascii", errors="replace")
    except OSError:
        text += format_line(fail("Report", "could not write logs/doctor.txt",
                                 "check write access to the logs folder"))
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
