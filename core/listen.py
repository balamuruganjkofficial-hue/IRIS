import os
import re
import time

import speech_recognition as sr
from groq import Groq

from core.config import CFG

_r = sr.Recognizer()
_r.pause_threshold = 1.1            # waits longer before deciding you finished
_r.non_speaking_duration = 0.6
_client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=8.0, max_retries=0)

_V = CFG["voice"]
_LANG = _V.get("stt_language", "en-US").split("-")[0]
_MODELS = _V.get("stt_models", ["whisper-large-v3-turbo", "whisper-large-v3"])
_WORDS = (["Iris", "Chrome", "Notepad", "YouTube", "Gmail", "LinkedIn", "Google",
           "screenshot", "reminder", "brightness", "volume", "Downloads"]
          + list(CFG.get("chrome", {}).get("profiles", {}))[:20])
_HINT = "Voice commands for a PC assistant named Iris: " + ", ".join(_WORDS) + "."
_FILLERS = {"mm", "hmm", "mhm", "um", "uh", "ah", "oh", "huh", "you", "ok", "okay",
            "thanks for watching", "thank you for watching"}


def _junk(text):
    t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    return len(t) < 2 or t in _FILLERS


def _whisper(audio):
    wav = audio.get_wav_data(convert_rate=16000, convert_width=2)
    for model in _MODELS:
        try:
            r = _client.audio.transcriptions.create(
                file=("speech.wav", wav), model=model, language=_LANG,
                prompt=_HINT, temperature=0.0)
            return (getattr(r, "text", "") or "").strip()
        except Exception as e:
            print(f"[stt] {model} failed: {e}")
    return None


def _google(audio):
    try:
        return _r.recognize_google(audio, language=_V.get("stt_language", "en-US"))
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        print("[stt] google error:", e)
        return ""


def listen(timeout=8, phrase_limit=15):
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

    seconds = len(audio.frame_data) / (audio.sample_rate * audio.sample_width)
    if seconds < 0.5:
        return ""

    t0 = time.time()
    text = _whisper(audio)
    if text is None:
        print("[stt] using Google fallback")
        text = _google(audio)
    print(f"[stt] {time.time() - t0:.1f}s")
    return "" if _junk(text) else text