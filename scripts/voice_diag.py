"""Diagnose the Edge TTS path: voice name, prosody strings, DNS, TCP and latency.

Run it before changing anything when Iris drops to the offline voice:

    venv\\Scripts\\python.exe scripts\\voice_diag.py

It synthesises five sentences with the configured Edge voice and prints, for
each one, whether it worked, the exact exception type and message when it did
not, and the time to the first audio chunk. It also validates the rate/pitch
strings that ``core.text.prosody`` produces, checks the configured voice exists
in the live voice list, and times DNS plus TCP connect to the Edge host.

Nothing is written into the project except small temp files, and no audio is
played. ``--timeout`` raises the per-sentence budget so a cold connection can be
told apart from a broken one.
"""

import argparse
import asyncio
import os
import re
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edge_tts  # noqa: E402

from core.config import CFG  # noqa: E402
from core.text import prosody  # noqa: E402

try:                                    # speakable arrives with the human voice
    from core.text import speakable
except ImportError:                     # diagnosis must run before that change
    def speakable(text, *_args, **_kwargs):
        return text

HOST = "speech.platform.bing.com"
PORT = 443

RATE_RE = re.compile(r"^[+-]\d+%$")
PITCH_RE = re.compile(r"^[+-]\d+Hz$")
VOLUME_RE = re.compile(r"^[+-]\d+%$")

SAMPLES = [
    "Hey boss, I am right here.",
    "That is a terrible idea, and I love it.",
    "Do you want me to open Chrome now?",
    "Your battery is at twenty percent, so plug in soon.",
    "I checked the logs, restarted the speech worker, cleared the cache and "
    "the voice is working again.",
]


def _timed(label, fn):
    start = time.perf_counter()
    try:
        value = fn()
        return value, time.perf_counter() - start, None
    except Exception as exc:  # noqa: BLE001 - diagnosis must never crash
        return None, time.perf_counter() - start, exc


def dns_and_tcp():
    """Return (dns_seconds, tcp_seconds, addresses, error)."""
    addresses, dns_s, err = _timed(
        "dns", lambda: socket.getaddrinfo(HOST, PORT, type=socket.SOCK_STREAM))
    if err is not None:
        return dns_s, None, [], err
    tcp_s = None
    try:
        start = time.perf_counter()
        with socket.create_connection((HOST, PORT), timeout=6):
            tcp_s = time.perf_counter() - start
    except OSError as exc:
        return dns_s, tcp_s, addresses, exc
    return dns_s, tcp_s, addresses, None


async def _voice_names(timeout=8.0):
    voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=timeout)
    return {str(v.get("ShortName")) for v in voices}


async def _synthesise(text, voice, rate, pitch, volume, timeout):
    """Return (first_audio_seconds, total_seconds, bytes, error)."""
    path = os.path.join(tempfile.gettempdir(),
                        "iris_diag_" + str(abs(hash(text)))[:10] + ".mp3")
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                       volume=volume)
    start = time.perf_counter()
    first = None
    total = 0
    stream = None
    try:
        stream = communicate.stream()
        deadline = start + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(
                    "no audio within {0:.1f}s".format(timeout))
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
            if chunk.get("type") == "audio":
                if first is None:
                    first = time.perf_counter() - start
                total += len(chunk.get("data") or b"")
        return first, time.perf_counter() - start, total, None
    except StopAsyncIteration:
        return first, time.perf_counter() - start, total, None
    except Exception as exc:  # noqa: BLE001
        return first, time.perf_counter() - start, total, exc
    finally:
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _validate(label, value, pattern):
    ok = bool(pattern.match(str(value)))
    print("  {0:<10} {1!r:<12} {2}".format(
        label, value, "OK" if ok else "INVALID"))
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", default=None,
                        help="override voice.tts_voice")
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-sentence budget in seconds (default: config)")
    parser.add_argument("--samples", type=int, default=5,
                        help="how many of the five samples to try")
    args = parser.parse_args(argv)

    voice_cfg = CFG.get("voice") or {}
    voice = args.voice or voice_cfg.get("tts_voice", "en-US-AvaMultilingualNeural")
    rate_cfg = voice_cfg.get("rate", "+0%")
    pitch_cfg = voice_cfg.get("pitch", "+0Hz")
    volume_cfg = voice_cfg.get("volume", "+0%")
    timeout = float(args.timeout if args.timeout is not None
                    else (float(voice_cfg.get("edge_connect_timeout", 6.0)) +
                          float(voice_cfg.get("edge_first_audio_cold", 8.0))))
    prosody_on = bool(voice_cfg.get("prosody", True))
    rj = int(voice_cfg.get("rate_jitter", 4) or 0)
    pj = int(voice_cfg.get("pitch_jitter", 3) or 0)
    style = voice_cfg.get("style", "warm")
    use_speakable = bool(voice_cfg.get("speakable", True))
    pronunciations = voice_cfg.get("pronunciations") or {}

    print("Iris voice diagnosis")
    print("=" * 60)
    print("config file : {0}".format(CFG.get("_path", "config.yaml")))
    print("voice       : {0}".format(voice))
    print("rate/pitch  : {0} / {1} (volume {2})".format(
        rate_cfg, pitch_cfg, volume_cfg))
    print("prosody     : {0} (jitter {1}% / {2}Hz, style {3})".format(
        prosody_on, rj, pj, style))
    print("timeout     : {0:.1f}s".format(timeout))
    print()

    # --- 1. DNS and TCP -----------------------------------------------------
    print("1. Network to {0}:{1}".format(HOST, PORT))
    dns_s, tcp_s, addresses, net_err = dns_and_tcp()
    print("  DNS        : {0:.3f}s ({1} address(es))".format(
        dns_s, len(addresses)))
    if tcp_s is not None:
        print("  TCP connect: {0:.3f}s".format(tcp_s))
    else:
        print("  TCP connect: FAILED")
    if net_err is not None:
        print("  network error: {0}: {1}".format(
            type(net_err).__name__, net_err))
    print()

    # --- 2. Voice name ------------------------------------------------------
    print("2. Voice name in the live Edge list")
    names, list_s, list_err = _timed(
        "voices", lambda: asyncio.run(_voice_names(timeout=8.0)))
    if list_err is not None:
        print("  list_voices FAILED after {0:.1f}s: {1}: {2}".format(
            list_s, type(list_err).__name__, list_err))
    else:
        print("  list_voices OK in {0:.1f}s ({1} voices)".format(
            list_s, len(names)))
        print("  {0} is {1}".format(
            voice, "PRESENT" if voice in names else "MISSING"))
    print()

    # --- 3. Prosody strings -------------------------------------------------
    print("3. Prosody strings (Edge only accepts forms like +8% and -3Hz)")
    bad = 0
    for text in SAMPLES[:max(1, args.samples)]:
        rate, pitch = prosody(text, rate_cfg, pitch_cfg, rj, pj, prosody_on,
                              style)
        ok = _validate("rate", rate, RATE_RE)
        ok = _validate("pitch", pitch, PITCH_RE) and ok
        if not ok:
            bad += 1
            print("    from: {0!r}".format(text[:60]))
    if not _validate("volume", volume_cfg, VOLUME_RE):
        bad += 1
    print("  invalid strings: {0}".format(bad))
    print()

    # --- 4. Live synthesis --------------------------------------------------
    print("4. Synthesis with {0} (budget {1:.1f}s each)".format(voice, timeout))
    ok_count = 0
    for index, text in enumerate(SAMPLES[:max(1, args.samples)], 1):
        spoken = speakable(text, pronunciations) if use_speakable else text
        rate, pitch = prosody(spoken, rate_cfg, pitch_cfg, rj, pj, prosody_on,
                              style)
        first, total, size, err = asyncio.run(
            _synthesise(spoken, voice, rate, pitch, volume_cfg, timeout))
        if err is None and size:
            ok_count += 1
            print("  [{0}] OK   first audio {1:.2f}s  total {2:.2f}s  "
                  "{3} bytes  rate {4} pitch {5}".format(
                      index, first if first is not None else -1, total,
                      size, rate, pitch))
        else:
            name = type(err).__name__ if err is not None else "EmptyAudio"
            message = str(err) if err is not None else "no audio data"
            print("  [{0}] FAIL after {1:.2f}s  {2}: {3}".format(
                index, total, name, message or "(empty message)"))
            print("      text: {0!r}".format(spoken[:70]))
    print()
    print("=" * 60)
    print("result: {0}/{1} sentences synthesised".format(
        ok_count, min(args.samples, len(SAMPLES))))
    if ok_count < min(args.samples, len(SAMPLES)):
        print("try again with --timeout 15 to tell a cold line from a fault")
    return 0 if ok_count == min(args.samples, len(SAMPLES)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
