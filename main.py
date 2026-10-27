import datetime
import logging
import queue
import random
import socket
import sys
import threading
import time
import winsound

# Only one Iris at a time (so autostart and a manual run never fight over the mic)
_lock = socket.socket()
try:
    _lock.bind(("127.0.0.1", 47653))
except OSError:
    print("Iris is already running. Stop it first: "
          "Get-Process pythonw | Stop-Process")
    sys.exit(0)

import psutil  # noqa: E402

from core.config import ROOT, CFG  # noqa: E402

(ROOT / "logs").mkdir(exist_ok=True)
HEADLESS = sys.stdout is None          # True when started with pythonw (no console)
if HEADLESS:
    _f = open(ROOT / "logs" / "console.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = _f
logging.basicConfig(filename=str(ROOT / "logs" / "iris.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

from core import agent, stt, voice  # noqa: E402
from core.ears import ears  # noqa: E402

CALL = CFG["persona"]["call_me"]
voice_mode = False
BYE_ACKS = ["Anytime.", "Just call me.", "I'll be here."]
SLEEP_WORDS = {"goodbye", "bye", "thanks", "thank", "nevermind"}


def chime(kind):
    try:
        if kind == "wake":
            winsound.Beep(880, 70)
            winsound.Beep(1320, 90)
        else:
            winsound.Beep(1000, 40)
    except Exception:  # noqa: BLE001
        pass


def _stdin_reader():
    if sys.stdin is None:
        return
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        ears.events.put(("typed", line.strip()))


def interrupt():
    voice.stop()
    agent.cancel.set()


def confirm(question):
    if voice_mode:
        voice.speak(question + " Say yes or no.")
        voice.wait_done()
        ears.flush()
        ears.set_mode("listen", 8.0)
        try:
            kind, payload = ears.events.get(timeout=12)
        except queue.Empty:
            ears.set_mode("busy")
            return False
        ears.set_mode("busy")
        if kind != "utterance":
            return False
        ans = stt.transcribe(payload).lower()
        return ans.startswith("yes") or "confirm" in ans or "go ahead" in ans
    print(f"[CONFIRM] {question} (y/n): ", end="", flush=True)
    end = time.time() + 30
    while time.time() < end:
        try:
            kind, payload = ears.events.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "typed":
            return payload.lower().startswith("y")
    return False


def handle(text, spoken_input):
    global voice_mode
    voice_mode = spoken_input
    if text.lower().strip(" .!?") in ("stop", "be quiet", "cancel", "quiet"):
        voice.stop()
        return
    try:
        print("Iris:", agent.run(text, confirm))
    except Exception as e:  # noqa: BLE001
        logging.exception("agent failed")
        print("[error]", e)
        voice.speak("Sorry, something went wrong on my side.")
    voice.wait_done()


def wants_sleep(low):
    words = low.replace("'", "").split()
    joined = "".join(words)
    return len(words) <= 4 and (bool(SLEEP_WORDS & set(words))
                                or "thatsall" in joined or "gotosleep" in joined)


def greet():
    h = datetime.datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
    b = psutil.sensors_battery()
    extra = f" Battery is at {b.percent:.0f} percent." if b else ""
    voice.speak(f"Good {part}, {CALL}. Iris is online.{extra} Say hey jarvis whenever "
                "you need me.")


def monitor():
    warned_batt, last_ram = False, 0.0
    while True:
        time.sleep(60)
        try:
            b = psutil.sensors_battery()
            if b and not b.power_plugged and b.percent <= 20:
                if not warned_batt:
                    voice.speak(f"Heads up, your battery is down to {b.percent:.0f} "
                                "percent. You might want to plug in.")
                    warned_batt = True
            else:
                warned_batt = False
            if psutil.virtual_memory().percent >= 92 and time.time() - last_ram > 900:
                voice.speak("Memory is nearly full. Closing a few heavy apps would help.")
                last_ram = time.time()
        except Exception:  # noqa: BLE001
            pass


def main():
    if HEADLESS:
        time.sleep(10)                  # let Wi-Fi connect after login
    ears.on_interrupt = interrupt
    import skills.browser_agent as _ba
    _ba.CONFIRM = confirm
    threading.Thread(target=_stdin_reader, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    print("Iris online. Say 'hey jarvis', or type a command (Enter alone = listen). "
          "Type 'quit' to exit.")
    ears.set_mode("busy")
    greet()
    voice.wait_done()
    ears.flush()
    ears.set_mode("idle")
    while True:
        try:
            kind, payload = ears.events.get(timeout=0.5)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            voice.stop()
            break
        try:
            if kind == "typed":
                if payload.lower() in ("quit", "exit"):
                    break
                if payload == "":
                    chime("wake")
                    ears.set_mode("listen", 7.0)
                    continue
                ears.set_mode("busy")
                handle(payload, False)
                ears.set_mode("idle")
            elif kind == "wake":
                chime("wake")
                ears.set_mode("listen", 7.0)
            elif kind == "utterance":
                chime("heard")
                text = stt.transcribe(payload)
                print("You said:", text)
                if not text:
                    ears.set_mode("idle")
                    continue
                if wants_sleep(text.lower().strip(" .!?")):
                    voice.speak(random.choice(BYE_ACKS))
                    voice.wait_done()
                    ears.set_mode("idle")
                    continue
                handle(text, True)
                if not agent.cancel.is_set():
                    ears.set_mode("listen", 8.0)    # keep the conversation going
        except KeyboardInterrupt:
            voice.stop()
            break


if __name__ == "__main__":
    main()
