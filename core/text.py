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


# ------------------------------------------------------------------ speakable
# Spoken text is not written text. Edge reads "20%" as "twenty percent" already,
# but it reads "9:30" as "nine colon thirty", spells out URLs and can stumble on
# long unpunctuated sentences. Everything below turns a reply into something a
# person would actually say, while the UI keeps the original words.

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_TENS_ORD = {20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth",
             60: "sixtieth", 70: "seventieth", 80: "eightieth", 90: "ninetieth"}
_ORD_SMALL = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
              6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
              11: "eleventh", 12: "twelfth", 13: "thirteenth",
              14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
              17: "seventeenth", 18: "eighteenth", 19: "nineteenth"}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_MONTH_NAMES = "|".join(_MONTHS) + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"

# Abbreviations that are almost always read as words, not letters.
_ABBREVIATIONS = (
    (r"\be\.g\.", "for example"),
    (r"\bi\.e\.", "that is"),
    (r"\betc\.", "et cetera"),
    (r"\bvs\.", "versus"),
    (r"\bapprox\.", "approximately"),
    (r"\bdept\.", "department"),
    (r"\bgovt\.", "government"),
    (r"\bProf\.", "Professor"),
    (r"\bDr\.", "Doctor"),
    (r"\bMr\.", "Mister"),
    (r"\bMrs\.", "Missus"),
    (r"\bMs\.", "Miss"),
    (r"\bNo\.\s*(?=\d)", "number "),
)

# Units only when they follow a number, so a stray "L" or "GB" is left alone.
_UNITS = (
    (r"(?<=\d)\s*\u00b0C\b", " degrees Celsius"),
    (r"(?<=\d)\s*\u00b0F\b", " degrees Fahrenheit"),
    (r"(?<=\d)\s*km/h\b", " kilometres per hour"),
    (r"(?<=\d)\s*mph\b", " miles per hour"),
    (r"(?<=\d)\s*km\b", " kilometres"),
    (r"(?<=\d)\s*kg\b", " kilograms"),
    (r"(?<=\d)\s*cm\b", " centimetres"),
    (r"(?<=\d)\s*mm\b", " millimetres"),
    (r"(?<=\d)\s*ms\b", " milliseconds"),
    (r"(?<=\d)\s*GB\b", " gigabytes"),
    (r"(?<=\d)\s*MB\b", " megabytes"),
    (r"(?<=\d)\s*KB\b", " kilobytes"),
    (r"(?<=\d)\s*TB\b", " terabytes"),
    (r"(?<=\d)\s*ft\b", " feet"),
    (r"(?<=\d)\s*lbs?\b", " pounds"),
    (r"(?<=\d)\s*oz\b", " ounces"),
    (r"(?<=\d)\s*ml\b", " millilitres"),
    (r"(?<=\d)\s*L\b", " litres"),
)

_DEFAULT_PRONUNCIATIONS = {
    "SQL": "sequel",
    "JSON": "jason",
    "GUI": "gooey",
    "API": "A P I",
    "URL": "U R L",
    "HTTP": "H T T P",
    "AI": "A I",
    "CPU": "C P U",
    "GPU": "G P U",
    "SSD": "S S D",
    "USB": "U S B",
    "OS": "O S",
}

_EMOJI_RE = re.compile(
    "[" +
    "\U0001F300-\U0001FAFF" +
    "\U00002600-\U000027BF" +
    "\U0001F1E6-\U0001F1FF" +
    "\U00002190-\U000021FF" +
    "\U00002B00-\U00002BFF" +
    "\U0000FE00-\U0000FE0F" +
    "\U0000200D" +
    "]+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_WIN_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'<>|]+")
_POSIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[\w.-]+/){2,}[\w.-]+")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\b")
_TIME_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([AaPp])\.?\s*[Mm]\.?)?\b")
_EMOTICON_RE = re.compile(
    r"(?:(?<=\s)|^)(?:[:;=8xX][-^']?[)(\[\]dDpP/\\|@]|<3)(?=\s|$|[.,!?])")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_US_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_DATE_MONTH_RE = re.compile(
    r"\b(" + _MONTH_NAMES + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_PERCENT_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*%")
_CURRENCY_RE = re.compile(r"([$\u00a3\u20ac])\s?(\d[\d,]*(?:\.\d+)?)")
_ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)


def _int_words(value):
    """Cardinal words for an integer. Handles the range a laptop reply needs."""
    n = int(value)
    if n < 0:
        return "minus " + _int_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        head = _ONES[n // 100] + " hundred"
        return head + (" and " + _int_words(rest) if rest else "")
    for size, name in ((1_000_000_000, " billion"), (1_000_000, " million"),
                       (1_000, " thousand")):
        if n >= size:
            rest = n % size
            head = _int_words(n // size) + name
            if not rest:
                return head
            if rest < 100:
                return head + " and " + _int_words(rest)
            return head + " " + _int_words(rest)
    return str(n)


def _year_words(value):
    """Read a four-digit year the way people say it: nineteen ninety-nine."""
    n = int(value)
    if 1000 <= n <= 1099:
        rest = n % 100
        return "one thousand" + (" and " + _int_words(rest) if rest else "")
    if 2000 <= n <= 2009:
        rest = n % 100
        return "two thousand" + (" and " + _int_words(rest) if rest else "")
    if 1100 <= n <= 1999 or 2010 <= n <= 2099:
        high, low = divmod(n, 100)
        if low == 0:
            return _int_words(high) + " hundred"
        if low < 10:
            return _int_words(high) + " oh " + _ONES[low]
        return _int_words(high) + " " + _int_words(low)
    return _int_words(n)


def _ordinal(value):
    """Ordinal words: 1 -> first, 22 -> twenty-second, 31 -> thirty-first."""
    n = int(value)
    if n <= 0:
        return _int_words(n)
    if n < 20:
        return _ORD_SMALL[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        if not rest:
            return _TENS_ORD.get(n, _int_words(n) + "th")
        return _TENS[tens] + "-" + _ORD_SMALL[rest]
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = _ONES[hundreds] + " hundred"
        return head if not rest else head + " and " + _ordinal(rest)
    return _int_words(n) + "th"


def _number_words(raw):
    text = str(raw).replace(",", "")
    if "." in text:
        whole, _, frac = text.partition(".")
        return (_int_words(int(whole or 0)) + " point " +
                " ".join(_ONES[int(d)] for d in frac if d.isdigit()))
    return _int_words(int(text or 0))


def _say_time(match):
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    suffix = ""
    if hour >= 13:
        hour -= 12
        suffix = " p m" if not meridiem else ""
    elif hour == 0:
        hour = 12
    elif meridiem and hour == 12:
        pass
    if meridiem:
        suffix = " " + meridiem.lower() + " m"
    if minute == 0:
        spoken = "o'clock"
    elif minute < 10:
        spoken = "oh " + _ONES[minute]
    else:
        spoken = _int_words(minute)
    return _int_words(hour) + " " + spoken + suffix


def _say_currency(match):
    symbol, raw = match.group(1), match.group(2)
    unit = {"$": ("dollar", "cent"), "\u00a3": ("pound", "penny"),
            "\u20ac": ("euro", "cent")}[symbol]
    text = raw.replace(",", "")
    if "." in text:
        whole, _, frac = text.partition(".")
        cents = int((frac + "00")[:2])
        value = int(whole or 0)
        if value == 0 and cents:
            return (_int_words(cents) + " " + unit[1] +
                    ("s" if cents != 1 else ""))
        out = _int_words(value) + " " + unit[0] + ("s" if value != 1 else "")
        if cents:
            out += " and " + _int_words(cents) + " " + unit[1] + ("s" if cents != 1 else "")
        return out
    value = int(text or 0)
    return _int_words(value) + " " + unit[0] + ("s" if value != 1 else "")


def _say_date_iso(match):
    year, month, day = (int(match.group(1)), int(match.group(2)),
                        int(match.group(3)))
    if not 1 <= month <= 12:
        return match.group(0)
    return "{0} {1}, {2}".format(_MONTHS[month - 1], _ordinal(day),
                                 _year_words(year))


def _say_date_us(match):
    month, day, year = (int(match.group(1)), int(match.group(2)),
                        int(match.group(3)))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return match.group(0)
    if year < 100:
        year += 2000
    return "{0} {1}, {2}".format(_MONTHS[month - 1], _ordinal(day),
                                 _year_words(year))


def _say_date_month(match):
    name, day = match.group(1), int(match.group(2))
    if not 1 <= day <= 31:
        return match.group(0)
    full = name if len(name) > 3 else next(
        (m for m in _MONTHS if m.lower().startswith(name.lower())), name)
    return "{0} {1}".format(full, _ordinal(day))


def _apply_pronunciations(text, pronunciation):
    for word, spoken in (pronunciation or {}).items():
        if not word or not spoken:
            continue
        text = re.sub(r"\b" + re.escape(str(word)) + r"\b", str(spoken), text,
                      flags=re.I)
    return text


def _split_long_sentence(sentence, max_words):
    """Turn a comma into a full stop until each piece is a comfortable length."""
    if max_words <= 0 or len(sentence.split()) <= max_words:
        return sentence
    cuts = [m.start() for m in re.finditer(r",\s*", sentence)]
    if not cuts:
        return sentence
    middle = len(sentence) / 2.0
    cut = min(cuts, key=lambda pos: abs(pos - middle))
    left = sentence[:cut].rstrip(" ,;:")
    right = sentence[cut:].lstrip(" ,;:")
    if not left or not right:
        return sentence
    if right[:1].islower():
        right = right[:1].upper() + right[1:]
    return (_split_long_sentence(left + ".", max_words) + " " +
            _split_long_sentence(right, max_words))


def speakable(text, pronunciations=None, max_words=25):
    """Turn a written reply into words a person would say out loud.

    Markdown, emoji, URLs and long file paths are removed; times, dates,
    percentages, currency, units and plain numbers become words; common
    abbreviations are expanded; ellipses become a short pause; and a sentence
    longer than ``max_words`` is split at its middle comma. ``pronunciations``
    is the config map, e.g. ``{"SQL": "sequel"}``.
    """
    spoken = str(text or "")
    if not spoken:
        return ""
    spoken = _CODE_FENCE_RE.sub(" ", spoken)
    spoken = _MD_LINK_RE.sub(lambda m: m.group(1), spoken)
    spoken = _URL_RE.sub(" ", spoken)
    spoken = _WIN_PATH_RE.sub(" ", spoken)
    spoken = _POSIX_PATH_RE.sub(" ", spoken)
    spoken = _EMOJI_RE.sub(" ", spoken)
    spoken = _EMOTICON_RE.sub(" ", spoken)
    spoken = clean(spoken)
    spoken = re.sub(r"(?m)^\s*[-+*>]\s+", "", spoken)

    for pattern, replacement in _ABBREVIATIONS:
        spoken = re.sub(pattern, replacement, spoken)
    spoken = _DATE_ISO_RE.sub(_say_date_iso, spoken)
    spoken = _DATE_US_RE.sub(_say_date_us, spoken)
    spoken = _DATE_MONTH_RE.sub(_say_date_month, spoken)
    spoken = _TIME_RE.sub(_say_time, spoken)
    spoken = _CURRENCY_RE.sub(_say_currency, spoken)
    spoken = _PERCENT_RE.sub(
        lambda m: _number_words(m.group(1)) + " percent", spoken)
    for pattern, replacement in _UNITS:
        spoken = re.sub(pattern, replacement, spoken, flags=re.I)
    spoken = _ORDINAL_RE.sub(lambda m: _ordinal(int(m.group(1))), spoken)
    spoken = _NUMBER_RE.sub(lambda m: _number_words(m.group(0)), spoken)
    spoken = _apply_pronunciations(
        spoken, _DEFAULT_PRONUNCIATIONS if pronunciations is None
        else pronunciations)

    spoken = spoken.replace("...", ", ").replace("\u2026", ", ")
    spoken = re.sub(r"[ \t]+", " ", spoken)
    spoken = re.sub(r"\s+([,.!?;:])", r"\1", spoken)
    spoken = re.sub(r"\s{2,}", " ", spoken).strip()

    spoken = " ".join(_split_long_sentence(s, max_words)
                      for s in sentences(spoken))
    return spoken.strip()


# -------------------------------------------------------------------- prosody
# Edge TTS accepts only "+8%" / "-3Hz" style strings. The helpers below always
# emit that form, even if the config file was edited by hand.

STYLES = {
    # style: (rate low, rate high, pitch low, pitch high), all integers
    "calm": (0, 3, -3, 2),
    "warm": (0, 5, -2, 4),
    "energetic": (3, 10, 1, 7),
}


def _number(value, fallback=0):
    match = re.search(r"([+-]?\d+)", str(value or ""))
    return int(match.group(1)) if match else fallback


def style_bounds(style):
    """The rate and pitch range for a named style; warm is the safe default."""
    return STYLES.get(str(style or "warm").lower(), STYLES["warm"])


def _clamp(value, low, high):
    return max(int(low), min(int(high), int(value)))


def prosody(text, rate="+2%", pitch="+0Hz", rate_jitter=4, pitch_jitter=3,
            enabled=True, style=None):
    """Stable, subtle per-phrase rate/pitch variation for a less robotic voice.

    The same phrase always gets the same tweak (so the TTS cache still hits),
    but neighbouring sentences drift a little instead of a flat monotone.
    Punctuation adds a small, natural contour: questions lift and slow down,
    exclamations brighten, trailing thoughts fall, long sentences slow down and
    very short reactions get a little quicker. With ``style`` set, the result is
    clamped to that style's range; the output is always a valid Edge string.
    """
    r, p = _number(rate, 0), _number(pitch, 0)
    if not enabled:
        r = _clamp(r, -20, 30)
        p = _clamp(p, -20, 20)
        if style is not None:
            r_low, r_high, p_low, p_high = style_bounds(style)
            r = _clamp(r, r_low, r_high)
            p = _clamp(p, p_low, p_high)
        return "{0:+d}%".format(r), "{0:+d}Hz".format(p)

    spoken = str(text or "")
    digest = int(hashlib.sha1(("prosody|" + spoken).encode("utf-8"))
                 .hexdigest()[:8], 16)
    tail = spoken.rstrip()
    if tail.endswith("?"):
        p += 3
        r -= 2
    elif tail.endswith("!"):
        p += 2
        r -= 1
    elif tail.endswith("..."):
        p -= 2
        r -= 2
    words = len(spoken.split())
    if words >= 26:
        r -= 2
    elif 0 < words <= 3:
        r += 2
    rj, pj = max(0, int(rate_jitter or 0)), max(0, int(pitch_jitter or 0))
    if rj:
        r += (digest % (2 * rj + 1)) - rj
    if pj:
        p += ((digest >> 8) % (2 * pj + 1)) - pj
    if style is not None:
        r_low, r_high, p_low, p_high = style_bounds(style)
        r = _clamp(r, r_low, r_high)
        p = _clamp(p, p_low, p_high)
    else:
        r = _clamp(r, -20, 30)
        p = _clamp(p, -20, 20)
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
