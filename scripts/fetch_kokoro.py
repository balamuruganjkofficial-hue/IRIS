"""One-time, resumable download of the optional Kokoro offline voice.

Kokoro is not needed for Iris to work. Run this only if you want a better
offline voice than Windows SAPI:

    venv\\Scripts\\python.exe scripts\\fetch_kokoro.py

It downloads the int8 model (about 114 MB) and the voice pack (about 28 MB)
from the free ``thewh1teagle/kokoro-onnx`` GitHub release into ``models/``.
A dropped connection resumes from the last byte instead of starting again, and
the files are verified by size before being renamed into place. Then install
the package and switch the engine:

    venv\\Scripts\\python.exe -m pip install kokoro-onnx
    # in config.yaml: voice.offline_engine: kokoro

Use ``--check`` to see what is present without downloading anything.
"""

import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import kokoro  # noqa: E402

BASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.1/")
FILES = (
    ("kokoro-v1.0.int8.onnx", 114_100_000),
    ("voices-v1.0.bin", 28_200_000),
)
CHUNK = 256 * 1024
TIMEOUT = (10, 30)                      # connect, read: a flaky line must fail fast


def _human(size):
    return "{0:.1f} MB".format(size / 1e6)


def _download(name, expected, target):
    url = BASE + name
    part = target + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if os.path.exists(target) and os.path.getsize(target) >= expected * 0.95:
        print("  {0}: already here ({1})".format(name, _human(os.path.getsize(target))))
        return True
    if os.path.exists(target):
        os.remove(target)
    headers = {"Range": "bytes={0}-".format(have)} if have else {}
    print("  {0}: {1} of about {2}".format(
        name, _human(have), _human(expected)))
    try:
        with requests.get(url, headers=headers, stream=True,
                          timeout=TIMEOUT, allow_redirects=True) as response:
            if response.status_code == 416:      # already complete
                response.close()
            elif response.status_code not in (200, 206):
                print("  {0}: HTTP {1}".format(name, response.status_code))
                return False
            else:
                mode = "ab" if have and response.status_code == 206 else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as handle:
                    last = time.time()
                    for block in response.iter_content(CHUNK):
                        if not block:
                            continue
                        handle.write(block)
                        have += len(block)
                        if time.time() - last > 2.0:
                            last = time.time()
                            print("    {0}: {1} ({2:.0f}%)".format(
                                name, _human(have),
                                100.0 * have / max(1, expected)), flush=True)
    except (requests.RequestException, OSError) as exc:
        print("  {0}: stopped ({1}). Run the script again to resume.".format(
            name, exc))
        return False
    size = os.path.getsize(part)
    if size < expected * 0.95:
        print("  {0}: only {1} downloaded, run again to resume".format(
            name, _human(size)))
        return False
    os.replace(part, target)
    print("  {0}: done ({1})".format(name, _human(size)))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="only report what is already installed")
    args = parser.parse_args(argv)

    print("Kokoro optional offline voice")
    print("=" * 60)
    print("model  : {0}".format(kokoro.model_path()))
    print("voices : {0}".format(kokoro.voices_path()))
    try:
        import kokoro_onnx  # noqa: F401
        print("package: kokoro-onnx installed")
    except Exception:  # noqa: BLE001
        print("package: kokoro-onnx NOT installed "
              "(pip install kokoro-onnx)")
    if args.check:
        print("ready  : {0}".format("yes" if kokoro.available() else "no"))
        return 0 if kokoro.available() else 1

    os.makedirs(os.path.dirname(kokoro.model_path()) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(kokoro.voices_path()) or ".", exist_ok=True)
    targets = {
        "kokoro-v1.0.int8.onnx": kokoro.model_path(),
        "voices-v1.0.bin": kokoro.voices_path(),
    }
    ok = True
    for name, expected in FILES:
        target = targets.get(
            name, os.path.join(os.path.dirname(kokoro.model_path()), name))
        ok = _download(name, expected, target) and ok
    print("=" * 60)
    if ok:
        print("downloaded. Now: pip install kokoro-onnx, then set")
        print("voice.offline_engine: kokoro in config.yaml and restart Iris.")
        return 0
    print("not finished; run the script again to resume.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
