import logging
from core.config import ROOT

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=str(ROOT / "logs" / "iris.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

from core import agent, listen, voice  # noqa: E402

voice_mode = False


def confirm(question):
    if voice_mode:
        voice.speak(question + " Say yes or no.")
        voice.wait_done()
        ans = listen.listen(timeout=6).lower()
        return ans.startswith("yes") or "confirm" in ans
    return input(f"[CONFIRM] {question} (y/n): ").strip().lower().startswith("y")


def main():
    global voice_mode
    print("Iris online. Type a command, press Enter alone to speak, or type 'quit'.")
    voice.speak("Iris online.")
    while True:
        try:
            text = input("You (Enter = speak): ").strip()
            voice_mode = False
            if text.lower() in ("quit", "exit"):
                break
            if text == "":
                print("Listening...")
                text = listen.listen()
                if not text:
                    print("I did not catch that.")
                    continue
                voice_mode = True
                print("You said:", text)
            if text.lower() in ("stop", "be quiet", "cancel"):
                voice.stop()
                continue
            print("Iris:", agent.run(text, confirm))
            voice.wait_done()
        except KeyboardInterrupt:
            voice.stop()
            break


if __name__ == "__main__":
    main()