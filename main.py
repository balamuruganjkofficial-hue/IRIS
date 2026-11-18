"""Iris: a local, voice-first assistant.

Run `python main.py` for the dark red interface (or `python main.py --console`).
The microphone, the brain and the interface all live in separate threads; the
bus in core/bus.py keeps them in sync without circular imports.
"""

import argparse
import datetime
import logging
import logging.handlers
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
import winsound
from pathlib import Path

# --------------------------------------------------------------- interpreter
# A bare `python main.py` with the system interpreter would die on the first
# third-party import. If the project's virtual environment exists and we are not
# inside it, hand over to it and keep every argument (and the console) intact.
_ROOT = Path(__file__).resolve().parent
_VENV = Path(os.getenv("IRIS_VENV") or (_ROOT / "venv"))
_NAME = "pythonw.exe" if Path(sys.executable).name.lower().startswith("pythonw") else "python.exe"
_EXE = _VENV / "Scripts" / _NAME
if (not os.getenv("IRIS_REEXEC") and not getattr(sys, "frozen", False)
        and _EXE.exists() and Path(sys.executable).resolve() != _EXE.resolve()):
    os.environ["IRIS_REEXEC"] = "1"
    print("[iris] switching to {0}".format(_EXE))
    try:
        sys.exit(subprocess.call([str(_EXE)] + sys.argv))
    except OSError as exc:
        print("[iris] could not start {0}: {1}".format(_EXE, exc))
        os.environ.pop("IRIS_REEXEC", None)

# --doctor runs before the single-instance lock and every third-party import so
# it can still report when config.yaml or a package is broken. It also writes
# logs/doctor.txt and never prints an API key value.
if "--doctor" in sys.argv:
    from scripts.doctor import main as doctor_main  # noqa: E402
    sys.exit(doctor_main(sys.argv))

# Patch DNS before any SDK/HTTP library is imported. httpx (Groq/OpenAI),
# requests and aiohttp (edge-tts) all call socket.getaddrinfo at runtime.
from core import dns as _dns  # noqa: E402
_dns.install()

# Only one Iris at a time (so autostart and a manual run never fight over the mic)
_LOCK = socket.socket()
try:
    _LOCK.bind(("127.0.0.1", 47653))
except OSError:
    print("Iris is already running. Stop it first: "
          "Get-Process pythonw | Stop-Process")
    sys.exit(0)

import psutil  # noqa: E402

from core import bus, connectivity, memory  # noqa: E402
from core.config import CFG, LOGS  # noqa: E402

HEADLESS = sys.stdout is None          # True when started with pythonw (no console)
if HEADLESS:
    try:
        sys.stdout = sys.stderr = open(LOGS / "console.log", "a", buffering=1,
                                       encoding="utf-8")
    except OSError:
        pass

# Windows consoles default to cp1252, which cannot encode characters free model
# providers happily return (thin spaces, curly quotes). A stray glyph must never
# abort a valid model attempt, so degrade to '?' instead of raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def _setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        str(LOGS / "iris.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


_setup_logging()
sys.setswitchinterval(0.005)            # snappier hand-offs between threads

from core import agent, stt, voice, wake  # noqa: E402
from core.ears import ears  # noqa: E402
from core.text import answer as read_answer, just_thanks, wants_sleep  # noqa: E402

NAME = CFG["persona"]["name"]
CALL = CFG["persona"]["call_me"]
WAKE = str(CFG["voice"].get("wake_word", "iris")).replace("_", " ")
BYE_ACKS = [
    "Go on then. I'll be here.",
    "Fine, abandon me. I'll keep the laptop warm.",
    "Alright, {call}. Don't do anything I wouldn't.",
    "Off you go. Say Iris when you miss me.",
    "Goodnight, {call}. Actually sleep this time.",
]
THANKS_ACKS = [
    "Anytime, {call}.",
    "Don't mention it. Seriously, I'll get a big head.",
    "That's what I'm here for.",
    "You're welcome. You can repay me in coffee.",
    "Mm. Just don't make me repeat myself.",
]
GREETINGS = {
    "morning": [
        "Morning, {call}. I'm up. Barely. What are we doing?",
        "Morning. Coffee first, or are we straight into chaos?",
        "Morning, {call}. I've already judged your tabs. Say {wake} when you need me.",
    ],
    "afternoon": [
        "Afternoon, {call}. I'm here. What's the plan?",
        "Afternoon. You look like a man with a to-do list and no intention of using it.",
        "I'm up. Say {wake} whenever, or just start talking; I'll keep up.",
    ],
    "evening": [
        "Evening, {call}. Long day? You've got that look.",
        "Evening. Whatever it is, I'll do the boring half.",
        "Evening, {call}. I'm here. Try to make it interesting.",
    ],
    "night": [
        "It's late, {call}. I'm awake, obviously. What's the crisis?",
        "Past midnight. Bold. What are we doing?",
        "Night shift, then. Say {wake} and I'll pretend I'm fully functional.",
    ],
}
IDLE_LINES = [
    "Still there, {call}? I'm not going anywhere.",
    "You've gone quiet. Plotting something?",
    "Just so you know, I'm still here. Judging. Politely.",
    "If you're stuck, say it out loud. I'm bored.",
    "Hey. Water. Drink some. I'll wait.",
]
SLEEP_WORDS = {"goodbye", "bye", "goodnight", "night", "farewell", "nevermind"}

voice_mode = False
_ui = None
_quit = threading.Event()
_chime = bool(CFG["voice"].get("chime", True))
_greet = True
_last_activity = time.time()
_last_banter = 0.0
_initiative = None


def _touch():
    global _last_activity
    _last_activity = time.time()
    if _initiative is not None:
        _initiative.note_activity()


def chime(kind):
    if not _chime:
        return
    try:
        if kind == "wake":
            winsound.Beep(880, 70)
            winsound.Beep(1320, 90)
        elif kind == "ask":
            winsound.Beep(660, 55)
            winsound.Beep(990, 75)
        else:
            winsound.Beep(1000, 40)
    except Exception:  # noqa: BLE001
        pass


def _stdin_reader():
    if sys.stdin is None:
        return
    while not _quit.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:  # noqa: BLE001
            return
        if not line:
            return
        ears.events.put(("typed", line.strip()))


def interrupt():
    voice.stop()
    agent.cancel.set()


def say(text):
    """Speak a line and show it in the interface."""
    bus.publish("say", text)
    voice.speak(text)


# ------------------------------------------------------------------ confirm
def _voice_confirm(question):
    """Ask out loud and listen for a spoken yes/no. The UI only mirrors it."""
    perms = CFG.get("permissions") or {}
    timeout = float(perms.get("timeout", 9))
    attempts = max(1, int(perms.get("attempts", 2)))
    bus.publish("auth", {"question": question, "state": "asking"})
    voice.stop()                       # cut any narration still playing
    chime("ask")
    for attempt in range(attempts):
        voice.speak(question if attempt == 0 else "Sorry. Yes, or no?")
        voice.wait_done()
        ears.flush()
        ears.set_mode("listen", timeout + 1.5)
        deadline = time.time() + timeout
        verdict, heard = None, ""
        while time.time() < deadline:
            try:
                kind, payload = ears.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "timeout":
                break
            if kind == "typed":
                heard = str(payload)
            elif kind == "utterance":
                heard = stt.transcribe(payload)
            else:
                continue
            verdict = read_answer(heard)
            if verdict is not None:
                break
        ears.set_mode("busy")
        bus.publish("state", "thinking")
        if verdict is not None:
            bus.publish("auth", {"question": question,
                                 "state": "granted" if verdict else "denied",
                                 "answer": heard})
            print("[confirm] {0} -> {1}".format(question, "yes" if verdict else "no"))
            return verdict
        if attempt + 1 < attempts:
            chime("ask")
    bus.publish("auth", {"question": question, "state": "timeout", "answer": ""})
    print("[confirm] {0} -> no answer".format(question))
    return False


def confirm(question):
    """Permissions are answered by voice by default; clicking is only a fallback."""
    perms = CFG.get("permissions") or {}
    if perms.get("voice_confirm", True):
        # the stream may still be opening during the first seconds of a run
        deadline = time.time() + 2.5
        while not ears.mic_ok and time.time() < deadline:
            time.sleep(0.1)
        if ears.mic_ok:
            return _voice_confirm(question)
    if _ui is not None and perms.get("click_fallback", True):
        bus.publish("auth", {"question": question, "state": "fallback"})
        try:
            return bool(_ui.ask(question, timeout=20))
        finally:
            bus.publish("auth", {"question": question, "state": "closed"})
    print("[CONFIRM] {0} (y/n): ".format(question), end="", flush=True)
    end = time.time() + 30
    while time.time() < end:
        try:
            kind, payload = ears.events.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "typed":
            verdict = read_answer(payload)
            return bool(verdict) if verdict is not None else payload.lower().startswith("y")
    return False


def handle(text, spoken_input):
    global voice_mode
    voice_mode = spoken_input
    if text.lower().strip(" .!?") in ("stop", "be quiet", "cancel", "quiet"):
        voice.stop()
        return
    try:
        reply = agent.run(text, confirm)
        print("{0}: {1}".format(NAME, reply))
    except Exception as exc:  # noqa: BLE001
        logging.exception("agent failed")
        print("[error]", exc)
        bus.publish("toast", "Something went wrong on my side.")
        voice.speak("That one broke on my end. My fault, mostly.")
    finally:
        voice.wait_done()


def greet():
    hour = datetime.datetime.now().hour
    if 5 <= hour < 12:
        part = "morning"
    elif 12 <= hour < 17:
        part = "afternoon"
    elif 17 <= hour < 22:
        part = "evening"
    else:
        part = "night"
    line = random.choice(GREETINGS[part]).format(call=CALL, name=NAME, wake=WAKE)
    battery = psutil.sensors_battery()
    if battery and not battery.power_plugged and battery.percent <= 25:
        line += " Battery's at {0:.0f} percent, by the way. Bold choice.".format(
            battery.percent)
    say(line)


def _idle_line():
    """A gentle fallback line for when the model cannot decide."""
    persona = CFG.get("persona") or {}
    lines = persona.get("idle_lines") or IDLE_LINES
    return random.choice(lines).format(call=CALL, name=NAME, wake=WAKE)


def _initiative_context():
    """A short, factual snapshot for the unprompted decision."""
    bits = []
    silence = max(0.0, (time.time() - _last_activity) / 60.0)
    bits.append("He has been quiet for about {0:.0f} minutes.".format(silence))
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            bits.append("Battery {0:.0f}%{1}.".format(
                battery.percent, " charging" if battery.power_plugged else ""))
        bits.append("Memory {0:.0f}% used.".format(
            psutil.virtual_memory().percent))
    except Exception:  # noqa: BLE001 - hardware data is a bonus, not a need
        pass
    try:
        turns = memory.recent(4)
    except Exception:  # noqa: BLE001
        turns = []
    if turns:
        bits.append("Recent conversation:\n" + "\n".join(
            "{0}: {1}".format(turn["role"], turn["content"])[:200]
            for turn in turns))
    return "\n".join(bits)


def _run_initiative_tool(name, args):
    """Run only safe, automatic tools chosen by Iris herself."""
    from core import initiative as _auto
    from core.skill import REGISTRY, run_tool
    spec = REGISTRY.get(name)
    if spec is None or spec["level"] != 1 or not _auto.safe_tool(name):
        bus.publish("toast", "I thought better of that one.")
        return None
    _ok, result = run_tool(name, args, confirm)
    return result


def _maybe_initiative():
    """Let Iris decide, on her own, whether one useful thing is worth doing."""
    persona = CFG.get("persona") or {}
    if not persona.get("proactive", True) or _initiative is None:
        return False
    if ears.mode() != "idle" or voice.is_speaking():
        return False
    try:
        decision = _initiative.maybe(
            context=_initiative_context(),
            facts=memory.facts()[-3:],
            fallback=_idle_line)
    except Exception:  # noqa: BLE001 - initiative must never break the loop
        logging.exception("initiative failed")
        return False
    if not decision:
        return False
    line = str(decision.get("say") or "").strip()
    tool = decision.get("tool")
    args = decision.get("args") or {}
    if line:
        print("[iris] unprompted: {0}".format(line))
        say(line)
        voice.wait_done()
    if tool:
        print("[iris] unprompted tool: {0} {1}".format(tool, args))
        result = _run_initiative_tool(tool, args)
        if result and result.startswith("ERROR"):
            bus.publish("toast", result[:180])
    return True


def monitor():
    warned_batt, last_ram = False, 0.0
    while not _quit.is_set():
        time.sleep(60)
        try:
            battery = psutil.sensors_battery()
            if battery and not battery.power_plugged and battery.percent <= 20:
                if not warned_batt:
                    say("Your battery's at {0:.0f} percent. Plug in before it gets "
                        "dramatic.".format(battery.percent))
                    warned_batt = True
            else:
                warned_batt = False
            if psutil.virtual_memory().percent >= 92 and time.time() - last_ram > 900:
                say("Memory's nearly full. Something's eating it - want me to look?")
                last_ram = time.time()
        except Exception:  # noqa: BLE001
            pass


def assist_loop():
    global voice_mode
    ears.on_interrupt = interrupt
    import skills.browser_agent as _ba
    _ba.CONFIRM = confirm
    bus.subscribe("surface_frame", lambda payload: _touch())   # a movie is not silence
    threading.Thread(target=_stdin_reader, daemon=True, name="stdin").start()
    threading.Thread(target=monitor, daemon=True, name="monitor").start()
    if HEADLESS:
        time.sleep(10)                  # let Wi-Fi connect after login
    print("{0} online. Say '{1}', or type a command (Enter alone = listen). "
          "Type 'quit' to exit.".format(NAME, WAKE))
    ears.set_mode("busy")
    if _greet:
        greet()
        voice.wait_done()
    ears.flush()
    ears.set_mode("idle")

    # One calm notice when the internet drops. The monitor publishes only on a
    # real state change, so this cannot repeat every few seconds.
    _offline_notice = {"said": False}

    def _on_connectivity(payload):
        online = (bool(payload.get("online")) if isinstance(payload, dict)
                  else bool(payload))
        if online:
            _offline_notice["said"] = False
        elif not _offline_notice["said"]:
            _offline_notice["said"] = True
            say("Internet's out. I'll keep the local stuff running.")

    bus.subscribe("connectivity", _on_connectivity)
    connectivity.start()

    while not _quit.is_set():
        try:
            kind, payload = ears.events.get(timeout=0.5)
        except queue.Empty:
            _maybe_initiative()
            continue
        _touch()
        try:
            if kind == "typed":
                if payload.lower() in ("quit", "exit"):
                    shutdown()
                    break
                if payload == "":
                    chime("wake")
                    ears.set_mode("listen", 7.0)
                    continue
                ears.set_mode("busy")
                bus.publish("heard", payload)
                if wants_sleep(payload, SLEEP_WORDS):
                    say(random.choice(BYE_ACKS).format(call=CALL))
                    voice.wait_done()
                    ears.set_mode("idle")
                    continue
                if just_thanks(payload):
                    say(random.choice(THANKS_ACKS).format(call=CALL))
                    voice.wait_done()
                    ears.set_mode("idle")
                    continue
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
                bus.publish("heard", text)
                if wants_sleep(text, SLEEP_WORDS):
                    say(random.choice(BYE_ACKS).format(call=CALL))
                    voice.wait_done()
                    ears.set_mode("idle")
                    continue
                if just_thanks(text):
                    say(random.choice(THANKS_ACKS).format(call=CALL))
                    voice.wait_done()
                    ears.set_mode("listen", 8.0)   # a thank-you is not a goodbye
                    continue
                handle(text, True)
                if not agent.cancel.is_set():
                    ears.set_mode("listen", 8.0)    # keep the conversation going
            elif kind == "timeout":
                bus.publish("state", "idle")
        except Exception:  # noqa: BLE001
            logging.exception("main loop error")
            bus.publish("toast", "That did not work.")
            ears.set_mode("idle")


# ------------------------------------------------------------------ lifecycle
def shutdown(*_):
    if _quit.is_set():
        return
    _quit.set()
    print("[iris] shutting down")
    try:
        voice.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        connectivity.stop()
    except Exception:  # noqa: BLE001
        pass
    if _ui is not None:
        _ui.post_close()


def _really_exit():
    try:
        from skills.browser_agent import _shutdown as close_browser
        close_browser()
    except Exception:  # noqa: BLE001
        pass
    try:
        ears.close()
    except Exception:  # noqa: BLE001
        pass
    logging.shutdown()
    os._exit(0)


def open_in_iris(url):
    """Open a link the user clicked in the interface, in Iris' own browser."""
    browser_command("open", url=url)


def browser_command(action, **kwargs):
    """Run a browser control from the interface without blocking the Tk thread."""
    def _run():
        try:
            import skills.browser_agent as ba
            result = ba.browser_control(action, **kwargs)
            if str(result).startswith("ERROR"):
                bus.publish("toast", str(result))
        except Exception:  # noqa: BLE001
            logging.exception("browser command failed")
            bus.publish("toast", "That browser action did not work.")
    threading.Thread(target=_run, daemon=True, name="ui-browser").start()


def open_path_in_iris(target):
    """Open a file, folder, app or settings page from a click in a surface."""
    def _run():
        try:
            from skills import surface
            result = surface.open_path_inside(target)
            if str(result).startswith("ERROR"):
                bus.publish("toast", str(result))
        except Exception:  # noqa: BLE001
            logging.exception("surface open failed")
            bus.publish("toast", "Could not open that.")
    threading.Thread(target=_run, daemon=True, name="ui-surface").start()


def window_command(action, **kwargs):
    """Forward a click, scroll or key to a mirrored app window."""
    def _run():
        try:
            from skills import capture
            if action == "click":
                capture.click(kwargs.get("hwnd"), kwargs.get("x", 0),
                              kwargs.get("y", 0))
            elif action == "scroll":
                capture.scroll(kwargs.get("hwnd"), kwargs.get("x", 0),
                               kwargs.get("y", 0), kwargs.get("dy", 0))
            elif action == "key":
                capture.key(kwargs.get("hwnd"), kwargs.get("key", ""))
            elif action == "type":
                capture.type_text(kwargs.get("hwnd"), kwargs.get("text", ""))
        except Exception:  # noqa: BLE001
            logging.exception("window input failed")
    threading.Thread(target=_run, daemon=True, name="ui-input").start()


def start_ui():
    global _ui
    try:
        from interfaces.ui import IrisUI
        _ui = IrisUI(send=lambda text: ears.events.put(("typed", text)),
                     stop=interrupt, open_url=open_in_iris,
                     browser_cmd=browser_command, open_path=open_path_in_iris,
                     window_cmd=window_command)
        return True
    except Exception:  # noqa: BLE001
        logging.exception("could not start the interface")
        _ui = None
        return False


def main(argv=None):
    global _ui, _greet, _initiative
    parser = argparse.ArgumentParser(description="Iris, a local voice assistant")
    parser.add_argument("--console", action="store_true", help="terminal only")
    parser.add_argument("--no-greeting", action="store_true", help="skip the hello")
    parser.add_argument("--doctor", action="store_true",
                        help="print a health report, save it and exit")
    args = parser.parse_args(argv)
    if args.doctor:
        from scripts.doctor import main as doctor_main
        return doctor_main(argv or sys.argv)
    _greet = not args.no_greeting
    from core.initiative import Initiative
    _initiative = Initiative(CFG)
    wake.warmup()                       # prefetch/teach the wake word in the background

    use_ui = not args.console and bool(CFG.get("ui", {}).get("enabled", True))
    if use_ui and not start_ui():
        print("[iris] interface unavailable, running in the terminal")
        use_ui = False

    bus.subscribe("quit", shutdown)
    threading.Thread(target=assist_loop, daemon=True, name="assist").start()

    if use_ui:
        _ui.run()
        _really_exit()
    else:
        try:
            while not _quit.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            shutdown()
        shutdown()
        _really_exit()


if __name__ == "__main__":
    main()
