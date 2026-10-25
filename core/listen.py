import speech_recognition as sr
from core.config import CFG

_r = sr.Recognizer()
_r.pause_threshold = 0.8


def listen(timeout=8, phrase_limit=12):
    try:
        with sr.Microphone() as src:
            _r.adjust_for_ambient_noise(src, duration=0.5)
            print("(speak now)")
            audio = _r.listen(src, timeout=timeout, phrase_time_limit=phrase_limit)
    except sr.WaitTimeoutError:
        print("[mic] no speech started within", timeout, "seconds")
        return ""
    except OSError as e:
        print("[mic error]", e)
        return ""
    try:
        return _r.recognize_google(audio, language=CFG["voice"]["stt_language"])
    except sr.UnknownValueError:
        print("[mic] heard sound but could not understand it")
        return ""
    except sr.RequestError as e:
        print("[mic] speech service error:", e)
        return ""