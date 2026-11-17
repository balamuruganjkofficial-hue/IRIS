"""Text helpers shared by speech, the brain, the ears and the agent."""

import hashlib
import re

SENT_RE = re.compile(r"(?<=[.!?])\s+")
_MARKUP_RE = re.compile(r"[*_`#]+")
_TOOLTAG_RE = re.compile(
    r"<\s*/?\s*(?:tool_code|tool_call|tool_name|function|parameter|string|value)\b.*",
    re.I | re.S)
_WORD_RE = re.compile(r"[a-z0-9']+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

FILLERS = {"mm", "hmm", "mhm", "um", "uh", "ah", "oh", "huh", "you", "ok", "okay",
           "thanks for watching", "thank you for watching"}

_THANKS = {"thanks", "thank", "cheers", "ta", "ty", "thx", "appreciate",
           "appreciated", "you", "so", "much", "a", "lot", "very", "really",
           "again", "mate", "boss", "iris", "dear", "ok", "okay"}

_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
        "allow", "allowed", "grant", "granted", "affirmative", "approve", "approved",
        "do it", "go ahead", "go for it", "please do", "proceed", "y"}
_NO = {"no", "nope", "nah", "deny", "denied", "negative", "cancel", "stop", "never",
       "nevermind", "never mind", "don't", "do not", "not now", "n"}


def clean(text):
    """Strip markdown that should never be read aloud."""
    return _MARKUP_RE.sub("", text or "").strip()


def sentences(text):
    return [s.strip() for s in SENT_RE.split(text or "") if s.strip()]


def tokens(text):
    """Lowercase word tokens, for fact matching."""
    return _TOKEN_RE.findall((text or "").lower())


def is_junk(text, min_len=2):
    """True for silence, noise transcripts and whisper's favourite fillers."""
    t = re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()
    return len(t) < min_len or t in FILLERS


def wants_sleep(text, sleep_words, max_words=4):
    """True only when the whole utterance is a goodbye.

    The old check fired on any short phrase containing 'thanks', so
    'thanks, open Chrome' ended the conversation. A farewell now has to be
    the first word and at most two words long.
    """
    ws = re.findall(r"[a-z]+", (text or "").lower())
    if not ws or len(ws) > max_words:
        return False
    joined = "".join(ws)
    if "thatsall" in joined or "gotosleep" in joined:
        return True
    return ws[0] in sleep_words and len(ws) <= 2


def just_thanks(text, max_words=5):
    """True for 'thanks', 'cheers boss', 'thank you so much' - and nothing else.

    Used so a quick thank-you gets a warm line and the conversation carries on
    instead of being treated as a farewell.
    """
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words or len(words) > max_words:
        return False
    return all(w in _THANKS for w in words)


def _number(value, fallback=0):
    match = re.search(r"([+-]?\d+)", str(value or ""))
    return int(match.group(1)) if match else fallback


def prosody(text, rate="+8%", pitch="+0Hz", rate_jitter=4, pitch_jitter=3,
            enabled=True):
    """Stable, subtle per-phrase rate/pitch variation for a less robotic voice.

    The same phrase always gets the same tweak (so the TTS cache still hits),
    but neighbouring sentences drift a little instead of a flat monotone.
    """
    if not enabled:
        return str(rate), str(pitch)
    digest = int(hashlib.sha1(("prosody|" + str(text or "")).encode("utf-8"))
                 .hexdigest()[:8], 16)
    r, p = _number(rate), _number(pitch)
    rj, pj = max(0, int(rate_jitter or 0)), max(0, int(pitch_jitter or 0))
    if rj:
        r += (digest % (2 * rj + 1)) - rj
    if pj:
        p += ((digest >> 8) % (2 * pj + 1)) - pj
    return "{0:+d}%".format(r), "{0:+d}Hz".format(p)


def answer(text):
    """Read a spoken yes/no answer. True = yes, False = no, None = unclear."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return None
    joined = " ".join(words)
    for phrase in ("do not", "don't", "no thanks", "no thank", "not allowed"):
        if phrase in joined:
            return False
    if joined in _YES or words[0] in _YES:
        return True
    if joined in _NO or words[0] in _NO:
        return False
    if any(w in _YES for w in words):
        return True
    if any(w in _NO for w in words):
        return False
    return None
