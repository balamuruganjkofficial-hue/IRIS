"""Generate the same six lines in eight free Edge voices so you can choose.

    venv\\Scripts\\python.exe scripts\\voice_audition.py

Each voice gets its own folder under ``data/voice_audition/<voice>/`` with six
mp3 files (casual, joke, question, serious, long explanation, time and number).
The folder opens when it is done. Nothing is chosen automatically: listen, then
put the voice you like in ``config.yaml`` as ``voice.tts_voice``. Voices that
are missing from the live Edge list are skipped, and rerunning only fills in
files that are missing (``--force`` redoes everything).
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edge_tts  # noqa: E402

from core.config import CFG, DATA  # noqa: E402
from core.text import prosody  # noqa: E402

try:                                    # speakable arrives with the human voice
    from core.text import speakable
except ImportError:                     # audition must work before that change
    def speakable(text):
        return text

VOICES = (
    "en-US-AvaMultilingualNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-AriaNeural",
    "en-US-JennyNeural",
    "en-GB-SoniaNeural",
    "en-IN-NeerjaNeural",
)

LINES = (
    ("01-casual", "Hey boss, I am right here. What are we doing today?"),
    ("02-joke", "I would tell you a joke about UDP, but you might not get it."),
    ("03-question", "Do you want me to open Chrome, or shall I leave it alone?"),
    ("04-serious", "Your battery is low. Please plug the laptop in before it "
                   "shuts down in the middle of something important."),
    ("05-long", "I checked the logs, restarted the speech worker, cleared the "
                "cache, and the voice is working again. It should stay stable "
                "now, but tell me if it drops out again."),
    ("06-time-and-number", "The meeting is at 9:30, room 12, and about 20 "
                           "percent of the team will join online."),
)


async def _voice_names():
    voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=10.0)
    return {str(v.get("ShortName")) for v in voices}


async def _save(text, voice, rate, pitch, path):
    tmp = path + ".part"
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, pitch=pitch,
                connect_timeout=6, receive_timeout=15)
            await asyncio.wait_for(communicate.save(tmp), timeout=35.0)
            os.replace(tmp, path)
            return None
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            try:
                os.remove(tmp)
            except OSError:
                pass
            if attempt >= 2:
                return exc
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


async def run(args):
    voice_cfg = CFG.get("voice") or {}
    rate = voice_cfg.get("rate", "+2%")
    pitch = voice_cfg.get("pitch", "+0Hz")
    style = voice_cfg.get("style", "warm")
    rj = int(voice_cfg.get("rate_jitter", 0) or 0)
    pj = int(voice_cfg.get("pitch_jitter", 0) or 0)

    try:
        live = await _voice_names()
    except Exception as exc:  # noqa: BLE001 - fall back to trying each voice
        print("could not fetch the voice list ({0}); trying every voice".format(
            exc))
        live = None
    if live is not None:
        print("live voice list: {0} voices".format(len(live)))

    root = DATA / "voice_audition"
    root.mkdir(parents=True, exist_ok=True)
    made = 0
    for voice in VOICES:
        if live is not None and voice not in live:
            print("skip {0}: not in the live list".format(voice))
            continue
        folder = root / voice
        folder.mkdir(parents=True, exist_ok=True)
        print("{0}".format(voice))
        for name, line in LINES:
            path = str(folder / (name + ".mp3"))
            if os.path.exists(path) and not args.force:
                print("  {0}: already here".format(name))
                made += 1
                continue
            spoken = speakable(line)
            voice_rate, voice_pitch = prosody(
                spoken, rate, pitch, rj, pj, True, style)
            error = await _save(spoken, voice, voice_rate, voice_pitch, path)
            if error is None:
                print("  {0}: ok".format(name))
                made += 1
            else:
                print("  {0}: FAILED ({1}: {2})".format(
                    name, type(error).__name__, error or "(no message)"))
    print()
    print("{0} file(s) in {1}".format(made, root))
    try:
        os.startfile(str(root))             # Windows: open the folder
    except Exception as exc:  # noqa: BLE001 - opening is a convenience
        print("open this folder yourself: {0} ({1})".format(root, exc))
    return 0 if made else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="regenerate files that already exist")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
