"""Summarise logs/latency.csv: p50/p90 per stage for the last N turns.

Pure stdlib, read-only and ASCII-only. Run it with::

    python scripts/latency_report.py --last 50
    python -m scripts.latency_report --last 20 --csv logs/latency.csv

The bottleneck line is deliberately plain English: it names the slowest stage
and suggests the first thing to check, without exposing any raw log content.
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "logs" / "latency.csv"

# key, column in latency.csv, display name, plain-language first check
STAGES = (
    ("stt", "stt_ms", "speech-to-text",
     "check the network to the STT host, or keep commands short"),
    ("token", "token_ms", "first model token",
     "try a smaller free model or check the provider status"),
    ("say", "say_ms", "first spoken line",
     "ask for shorter replies so the first line arrives sooner"),
    ("tts", "tts_ms", "voice synthesis",
     "check Edge TTS reachability; common phrases warm the disk cache"),
    ("play", "play_ms", "audio start",
     "check the sound device and its output buffer"),
    ("tools", "tool_ms", "tool calls (total)",
     "look at tool_max_ms to find the one slow tool"),
    ("finish", "finish_ms", "wrap-up",
     "check whether trailing audio keeps the player open"),
    ("total", "total_ms", "end to end", "sum of the stages above"),
)
BOTTLENECK_KEYS = tuple(key for key, _, _, _ in STAGES if key != "total")


def default_last():
    """Read latency.report_turns from config, falling back when unavailable."""
    try:
        from core.config import CFG
        return max(1, int((CFG.get("latency") or {}).get("report_turns", 50)))
    except Exception:  # noqa: BLE001 - the report must work standalone
        return 50


def percentile(values, fraction):
    """Linear-interpolated percentile (numpy's default method).

    ``percentile([1, 2, 3, 4], 0.5)`` is 2.5. Empty input gives None, which the
    report renders as a dash rather than a misleading zero.
    """
    data = sorted(float(v) for v in values if v not in (None, ""))
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * min(1.0, max(0.0, float(fraction)))
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (rank - low)


def load_rows(path=None, last=50):
    """Return the last ``last`` rows of the CSV; missing files give []."""
    path = Path(path or DEFAULT_CSV)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []
    return rows[-max(1, int(last)):]


def _numbers(rows, column):
    out = []
    for row in rows:
        raw = (row or {}).get(column)
        if raw in (None, ""):
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def analyze(rows):
    """Compute n/p50/p90 in milliseconds for each stage."""
    stats = {}
    for key, column, label, hint in STAGES:
        values = _numbers(rows, column)
        stats[key] = {
            "label": label,
            "hint": hint,
            "n": len(values),
            "p50": percentile(values, 0.50),
            "p90": percentile(values, 0.90),
        }
    bottleneck = None
    best = 0.0
    for key in BOTTLENECK_KEYS:
        median = stats[key]["p50"] or 0.0
        if stats[key]["n"] and median > best:
            bottleneck, best = key, median
    return stats, bottleneck


def _ms(value):
    return "{0:8.0f} ms".format(value) if value is not None else "       -"


def format_report(stats, bottleneck, rows, path=None, last=50):
    """Return the complete plain-ASCII report as one string."""
    shown = Path(path or DEFAULT_CSV).name
    lines = ["Latency report - {0} (last {1} of {2} turns)".format(
        shown, len(rows), last)]
    if not rows:
        lines.append("No turns logged yet. Run Iris with a few voice commands first.")
        return "\n".join(lines) + "\n"

    lines.append("stage                     n      p50       p90")
    lines.append("-" * 50)
    for key, _, _, _ in STAGES:
        item = stats[key]
        lines.append("{0:<25} {1:>3}  {2}  {3}".format(
            item["label"][:25], item["n"], _ms(item["p50"]), _ms(item["p90"])))
    lines.append("")
    if bottleneck is None:
        lines.append("Biggest bottleneck: not enough stage data yet.")
    else:
        item = stats[bottleneck]
        lines.append("Biggest bottleneck: {0} (p50 {1:.0f} ms, p90 {2:.0f} ms).".format(
            item["label"], item["p50"] or 0.0, item["p90"] or 0.0))
        lines.append("  -> {0}".format(item["hint"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Iris per-turn latency report")
    parser.add_argument("--last", type=int, default=default_last(),
                        help="number of recent turns to summarise (default: %(default)s)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV),
                        help="path to latency.csv (default: %(default)s)")
    args = parser.parse_args(argv)
    rows = load_rows(args.csv, args.last)
    stats, bottleneck = analyze(rows)
    sys.stdout.write(format_report(stats, bottleneck, rows, args.csv, args.last))
    return 0


if __name__ == "__main__":
    sys.exit(main())
