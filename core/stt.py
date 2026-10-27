import io
import os
import re
import time
import wave

import speech_recognition as sr
from groq import Groq

from core.config import CFG

_V = CFG["voice"]
_LANG = _V.get("stt_language", "en-US").split("-")[0]
_MODELS = _V.get("stt_models", ["whisper-large-v3-turbo", "whisper-large-v3"])
_client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=8.0, max_retries=0)
_WORDS = (["Iris", "Chrome", "Notepad", "YouTube", "Gmail", "LinkedIn", "Google",
           "screenshot", "reminder", "brightness", "volume", "Downloads"]
          + list(CFG.get("chrome", {}).get("profiles", {}))[:20])
_HINT = "Voice commands for a PC assistant named Iris: " + ", ".join(_WORDS) + "."
_FILLERS = {"mm", "hmm", "mhm", "um", "uh", "ah", "oh", "huh", "you", "ok", "okay",
            "thanks for watching", "thank you for watching"}


def _wav(raw):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(raw)
    return buf.getvalue()


def _whisper(wav):
    for model in _MODELS:
        try:
            r = _client.audio.transcriptions.create(
                file=("speech.wav", wav), model=model, language=_LANG,
                prompt=_HINT, temperature=0.0)
            return (getattr(r, "text", "") or "").strip()
        except Exception as e:
            print(f"[stt] {model} failed: {e}")
    return None


def _google(raw):
    try:
        rec = sr.Recognizer()
        return rec.recognize_google(sr.AudioData(raw, 16000, 2),
                                    language=_V.get("stt_language", "en-US"))
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print("[stt] google error:", e)
        return ""


def _junk(text):
    t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    return len(t) < 2 or t in _FILLERS


def transcribe(raw):
    """raw = 16 kHz mono 16-bit audio bytes. Returns text, or '' for silence/noise."""
    t0 = time.time()
    text = _whisper(_wav(raw))
    if text is None:
        print("[stt] using Google fallback")
        text = _google(raw)
    print(f"[stt] {time.time() - t0:.1f}s")
    return "" if _junk(text) else text