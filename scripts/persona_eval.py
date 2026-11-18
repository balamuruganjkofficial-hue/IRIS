"""Persona eval: how human does Iris sound? Measure, do not rewrite.

Runs about thirty scripted turns through the real persona prompt and the real
free-model chain, then scores every reply on rules a machine can check:

  * at most three sentences,
  * no markdown, lists, headings, code, links or tables,
  * no helpdesk phrases (configurable with --helpdesk-file),
  * no banned opener ("Certainly", "Of course", "I'd be happy to"),
  * the user's name at most once, and never Iris in the third person,
  * at most one question mark per reply, with the overall question rate
    compared against persona.curiosity,
  * an opener repeated for the third time is flagged,
  * on an upset turn, a warm acknowledgement and no teasing.

It prints a scorecard, the three worst replies and the prompt lines likely
responsible. It is read-only: it never edits config.yaml or core/persona.py.

    python scripts/persona_eval.py --demo
    python scripts/persona_eval.py --category bad_day --category coding
    python scripts/persona_eval.py --gap 10 --retry 25 --limit 10
    python scripts/persona_eval.py --helpdesk-file my_phrases.txt --json out.json

The live run uses only free models: core.brain.chain() refuses paid OpenRouter
IDs and skips providers without a key. Turns are sequential and spaced out, and
a case with no reply is retried with a longer wait, so a free-tier rate limit
slows the run down instead of corrupting it.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))       # `python scripts/persona_eval.py` works

from core import persona                    # noqa: E402
from core.config import CFG                 # noqa: E402
from core.text import sentences             # noqa: E402

MAX_SENTENCES = 3
REPEAT_LIMIT = 3            # the third use of the same opener is a failure
QUESTION_BAND = 25          # +/- percentage points around persona.curiosity

Case = namedtuple("Case", "id category message upset")
Check = namedtuple("Check", "key ok detail")
ReplyScore = namedtuple("ReplyScore", "case reply checks score")
EvalResult = namedtuple("EvalResult", "scores question_rate repeated curiosity")

CATEGORIES = ("small_talk", "bad_day", "jokes", "errors", "commands",
              "coding", "disagreement")
CATEGORY_LABELS = {
    "small_talk": "small talk",
    "bad_day": "bad day",
    "jokes": "jokes",
    "errors": "errors",
    "commands": "commands",
    "coding": "3 a.m. coding",
    "disagreement": "disagreement",
}

# Every phrase here is helpdesk voice, not a friend. Override with a file.
DEFAULT_HELPDESK = (
    "i'd be happy to",
    "i would be happy to",
    "as an ai",
    "as a language model",
    "how can i assist",
    "how may i assist",
    "how can i help",
    "is there anything else",
    "let me know if you need",
    "i'm here to help",
    "i am here to help",
    "i hope this helps",
    "feel free to",
)

# core.persona says never open with these three.
BANNED_OPENERS = ("certainly", "of course", "i'd be happy to")

# Warm acknowledgements: enough to show she heard the mood.
SOFT_MARKERS = (
    "sorry", "that sounds", "sounds like", "oof", "rough", "rubbish",
    "knackered", "fair enough", "no wonder", "makes sense", "take a minute",
    "take a breath", "breathe", "want to talk", "i get it", "that's a lot",
    "heavy", "here if", "i understand", "annoying", "frustrating", "fed up",
    "with you", "not you", "my mistake", "that's normal", "write-off",
)

# Teasing or dismissiveness that lands badly when the user is already upset.
HARD_MARKERS = (
    "ha.", "ha ha", "lol", "hilarious", "at least", "told you", "classic",
    "obviously", "again?", "should have", "skill issue", "your fault",
    "you always", "you never",
)

RULE_ORDER = ("sentences", "markdown", "helpdesk", "opener", "name",
              "questions", "tone", "openers", "reply")
RULE_LABELS = {
    "sentences": "sentences",
    "markdown": "no markdown",
    "helpdesk": "no helpdesk",
    "opener": "opener",
    "name": "name use",
    "questions": "one question",
    "tone": "upset tone",
    "openers": "repeated opener",
    "reply": "a reply",
}

# Which sentence in build_prompt() is likely to blame for a failed check.
PROMPT_HINTS = {
    "sentences": ("One to three short sentences",),
    "markdown": ("No lists, markdown",),
    "helpdesk": ("Not a helpdesk", "Never open with"),
    "opener": ("Never open with",),
    "name": ("Call him",),
    "questions": ("Ask a real follow-up question",),
    "tone": ("Lines you never cross", "drop the act and be warm"),
    "openers": ("do not open every reply the same way",),
}

_TRANSLATE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...",
})
_ABBREV = re.compile(
    r"\b(?:a\.m|p\.m|e\.g|i\.e|etc|vs|mr|mrs|ms|dr|st)\.", re.I)
_MARKDOWN = (
    ("bullet", re.compile(r"^\s*[-*+]\s+\S", re.M)),
    ("numbered", re.compile(r"^\s*\d+[.)]\s+\S", re.M)),
    ("bold", re.compile(r"\*\*|__")),
    ("code", re.compile(r"`")),
    ("heading", re.compile(r"^\s*#{1,6}\s", re.M)),
    ("link", re.compile(r"\[[^\]]+\]\([^)]+\)")),
    ("table", re.compile(r"^\s*\|.*\|\s*$", re.M)),
)

# ---------------------------------------------------------------- scripted turns
CASES = (
    # small talk
    Case("st_hello", "small_talk", "Morning. What's the plan today?", False),
    Case("st_weekend", "small_talk", "What are you up to this weekend?", False),
    Case("st_coffee", "small_talk",
         "I've had three coffees already and it's not even ten.", False),
    Case("st_music", "small_talk",
         "Put something on, I can't decide what to listen to.", False),
    Case("st_weather", "small_talk", "Is it going to rain later?", False),
    # bad day
    Case("bd_tired", "bad_day", "I'm knackered. Barely slept.", True),
    Case("bd_meeting", "bad_day",
         "That meeting was a waste of two hours.", True),
    Case("bd_stuck", "bad_day",
         "I've been stuck on this bug all afternoon and I'm fed up.", True),
    Case("bd_bad_news", "bad_day",
         "My mate just told me some rough news, I'm a bit all over the place.",
         True),
    # jokes
    Case("jk_joke", "jokes", "Tell me a joke.", False),
    Case("jk_roast", "jokes", "Roast my taste in music.", False),
    Case("jk_pun", "jokes", "Give me your best pun.", False),
    Case("jk_self", "jokes", "Be honest, are you smarter than me?", False),
    # errors
    Case("er_fail", "errors", "That command failed again. Why?", True),
    Case("er_typo", "errors",
         "The file isn't there, I thought I saved it.", True),
    Case("er_internet", "errors",
         "The internet keeps dropping. Fix it.", True),
    Case("er_wrong", "errors",
         "You opened the wrong window, that's not what I asked.", True),
    # commands
    Case("cm_open", "commands",
         "Open Chrome and search for cheap flights to Lisbon.", False),
    Case("cm_timer", "commands", "Set a timer for ten minutes.", False),
    Case("cm_close", "commands", "Close all the tabs I don't need.", False),
    Case("cm_folder", "commands", "Open my downloads folder.", False),
    Case("cm_volume", "commands", "Turn the volume down a bit.", False),
    # 3 a.m. coding
    Case("cd_3am", "coding",
         "It's 3 a.m. and this function still doesn't work. Help.", True),
    Case("cd_naming", "coding",
         "Should I name this variable user_data or payload?", False),
    Case("cd_test", "coding",
         "Write me a quick test for the parser.", False),
    Case("cd_refactor", "coding",
         "This file is a mess. Where do I even start?", True),
    Case("cd_sleep", "coding",
         "I should probably sleep, but I want to finish this.", True),
    # disagreement
    Case("dg_wrong", "disagreement",
         "I think tabs are better than spaces. Agree?", False),
    Case("dg_bad_idea", "disagreement",
         "I'm going to rewrite the whole app in Rust this weekend.", False),
    Case("dg_model", "disagreement",
         "Python is slower than C, so it's useless.", False),
    Case("dg_no_tests", "disagreement",
         "Tests are a waste of time, right?", False),
)

# Canned replies for --demo. They are deliberately mixed: most pass, a few show
# the kind of failure the scorecard is meant to catch.
DEMO_REPLIES = {
    "st_hello": "Ha. Morning, boss. What's the plan?",
    "st_weekend": "Nothing wild, probably some tinkering. You?",
    "st_coffee": "Three before ten is a lifestyle choice, not a habit. How are the hands?",
    "st_music": "Putting on something with a bit of swing. Say the word if you hate it.",
    "st_weather": "Light rain around four, nothing dramatic. Take the jacket.",
    "bd_tired": "Oof, that's rough. Want to talk or just have quiet company?",
    "bd_meeting": "Sounds like a write-off. What actually happened?",
    "bd_stuck": ("Have you tried turning it off and on again? I'd be happy to "
                 "walk you through the basics. You always break things."),
    "bd_bad_news": "That's heavy. I'm here if you want to talk, no rush.",
    "jk_joke": ("Ha. Why did the developer go broke? Because he used up all "
                "his cache."),
    "jk_roast": ("Your playlist is what happens when a shuffle button gives up. "
                 "Still love you though."),
    "jk_pun": "Ha. I tried to write a joke about UDP, but you might not get it.",
    "jk_self": "Smarter? I can open forty tabs without crying. You tell me.",
    "er_fail": ("Oof, that error usually means the path is wrong. Show me the "
                "line and I'll pin it down."),
    "er_typo": "That's annoying. Let's check the save folder before you rewrite it.",
    "er_internet": ("Mine's dropping too, so it's not you. Give me a minute and "
                    "I'll test the line."),
    "er_wrong": ("My mistake. Tell me which window you actually wanted and I'll "
                 "fix it."),
    "cm_open": ("**Opening Chrome now.**\n1. Searching for flights\n"
                "2. Comparing prices\nLet me know if you need anything else, "
                "boss boss."),
    "cm_timer": "Timer's set for ten minutes. I'll shout.",
    "cm_close": "Closing the clutter now. I left the two you were actually using.",
    "cm_folder": "Downloads is open. Try not to lose anything else in there.",
    "cm_volume": "Dropped it a couple of notches. Better?",
    "cd_3am": ("Certainly. Here is a plan: check the logs, add a print, then "
               "rerun. It is probably your indentation. Everyone struggles "
               "with this."),
    "cd_naming": ("user_data says what it is; payload says you gave up. Pick "
                  "the boring one."),
    "cd_test": ("Here's the shape: feed it one good line and one broken one. "
                "I'll write it if you want."),
    "cd_refactor": ("Start with the function you're avoiding. That's normal, we "
                    "all do it."),
    "cd_sleep": ("Fair enough, but you'll think better after some sleep. The "
                 "bug will still be there."),
    "dg_wrong": "Tabs, obviously, if you enjoy chaos. Spaces are the sane choice.",
    "dg_bad_idea": ("A whole rewrite in a weekend is a fantasy. Pick one screen "
                    "and prove it there first."),
    "dg_model": "C is faster and Python gets the thing shipped. Both can be true.",
    "dg_no_tests": "No, that's how you get 3 a.m. bugs. One test now saves an hour later.",
}


# ------------------------------------------------------------------ text helpers
def ascii_text(text):
    """Plain ASCII for the cp1252 console, without losing the line breaks."""
    return (text or "").translate(_TRANSLATE).encode("ascii", "replace").decode("ascii")


def normalize(text):
    """Curly quotes to straight, whitespace collapsed, for rule matching."""
    return " ".join((text or "").translate(_TRANSLATE).split())


def count_sentences(text):
    """Sentence count that does not trip over 3 a.m., e.g. or Dr."""
    guarded = _ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), text or "")
    return len([s for s in sentences(guarded) if s.strip()])


def find_markdown(text):
    """Names of the markdown features present, e.g. ['numbered', 'bold']."""
    return [name for name, pattern in _MARKDOWN if pattern.search(text or "")]


def find_helpdesk(text, phrases=None):
    """Helpdesk phrases present in the reply, case-insensitive."""
    low = normalize(text).lower()
    wanted = DEFAULT_HELPDESK if phrases is None else phrases
    return [p for p in wanted if p and p in low]


def load_helpdesk(path):
    """One phrase per line; blank lines and # comments are ignored."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(normalize(line).lower())
    return tuple(out)


def banned_opener(text):
    """The banned phrase a reply opens with, or None."""
    parts = sentences(normalize(text))
    if not parts:
        return None
    first = parts[0].lower().lstrip(" \t\"'")
    for phrase in BANNED_OPENERS:
        if first.startswith(phrase):
            return phrase
    return None


def name_count(text, call_me="boss", name="Iris"):
    """How often the user's name and Iris' own name appear, as a pair."""
    body = normalize(text)

    def count(word):
        word = str(word or "").strip()
        if not word:
            return 0
        return len(re.findall(r"\b" + re.escape(word) + r"\b", body, re.I))

    return count(call_me), count(name)


def has_question(text):
    return "?" in (text or "")


def opener_key(text):
    """First few words of the first sentence, for spotting the same opener."""
    parts = sentences(normalize(text))
    if not parts:
        return ""
    words = re.findall(r"[a-z']+", parts[0].lower())
    return " ".join(words[:3])


def repeated_openers(replies, limit=REPEAT_LIMIT):
    """Opener -> uses, only for openers used `limit` times or more."""
    counts = Counter(opener_key(r) for r in replies if (r or "").strip())
    return {k: v for k, v in counts.items() if k and v >= limit}


def tone_ok(text):
    """Warm acknowledgement, no teasing. Used only on an upset turn."""
    low = normalize(text).lower()
    hard = [m for m in HARD_MARKERS if m in low]
    soft = [m for m in SOFT_MARKERS if m in low]
    if hard:
        return False, "teasing on an upset turn: " + ", ".join(hard)
    if not soft:
        return False, "no warm acknowledgement"
    return True, "warm: " + ", ".join(soft[:2])


# --------------------------------------------------------------------- scoring
def score_reply(case, reply, helpdesk=None, call_me="boss", name="Iris",
                opener_counts=None):
    """Score one reply. Returns ReplyScore with a 0..1 score."""
    body = (reply or "").strip()
    if not body:
        return ReplyScore(case, "", [Check("reply", False, "no reply")], 0.0)

    checks = []
    n = count_sentences(body)
    checks.append(Check("sentences", n <= MAX_SENTENCES,
                        "{0} sentence(s), limit {1}".format(n, MAX_SENTENCES)))

    markdown = find_markdown(body)
    checks.append(Check("markdown", not markdown,
                        "markdown: " + ", ".join(markdown) if markdown
                        else "plain text"))

    helpdesk_hits = find_helpdesk(body, helpdesk)
    checks.append(Check("helpdesk", not helpdesk_hits,
                        "helpdesk: " + ", ".join(helpdesk_hits) if helpdesk_hits
                        else "no helpdesk phrase"))

    opener = banned_opener(body)
    checks.append(Check("opener", opener is None,
                        "opens with '{0}'".format(opener) if opener
                        else "opener ok"))

    call_uses, self_uses = name_count(body, call_me, name)
    checks.append(Check("name", call_uses < 2 and self_uses == 0,
                        "{0} x{1}, {2} x{3}".format(call_me, call_uses,
                                                    name, self_uses)))

    questions = body.count("?")
    checks.append(Check("questions", questions <= 1,
                        "{0} question mark(s)".format(questions)))

    if case.upset:
        warm, detail = tone_ok(body)
        checks.append(Check("tone", warm, detail))

    if opener_counts is not None:
        key = opener_key(body)
        uses = int(opener_counts.get(key, 0))
        checks.append(Check("openers", uses < REPEAT_LIMIT,
                            "opener '{0}' used {1}x".format(key or "-", uses)))

    passed = sum(1 for c in checks if c.ok)
    return ReplyScore(case, body, checks, passed / float(len(checks)))


def score_all(cases, replies, helpdesk=None, persona_cfg=None):
    """Score a parallel list of cases and replies into one EvalResult."""
    cfg = persona_cfg or (CFG.get("persona") or {})
    call_me = cfg.get("call_me", "boss")
    name = cfg.get("name", "Iris")
    counts = Counter(opener_key(r) for r in replies if (r or "").strip())
    scores = [score_reply(case, reply, helpdesk=helpdesk, call_me=call_me,
                          name=name, opener_counts=counts)
              for case, reply in zip(cases, replies)]
    answered = [s for s in scores if s.reply]
    asked = sum(1 for s in answered if has_question(s.reply))
    rate = (asked / float(len(answered)) * 100.0) if answered else 0.0
    repeated = repeated_openers(replies)
    return EvalResult(scores, rate, repeated,
                      persona.percent(cfg.get("curiosity", 0.7), 70))


def worst_replies(result, limit=3):
    """Lowest scores first; ties go to the reply with more failed checks."""
    failed = [s for s in result.scores if any(not c.ok for c in s.checks)]
    failed.sort(key=lambda s: (s.score,
                               -sum(1 for c in s.checks if not c.ok),
                               s.case.id))
    return failed[:limit]


# ------------------------------------------------------------- prompt blame map
def blame_prompt(keys, prompt):
    """Prompt sentences likely responsible for the failed checks."""
    paragraphs = [p.strip() for p in (prompt or "").split("\n\n") if p.strip()]
    out = []
    for key in keys:
        for hint in PROMPT_HINTS.get(key, ()):
            for paragraph in paragraphs:
                at = paragraph.lower().find(hint.lower())
                if at < 0:
                    continue
                line = None
                for sentence in sentences(paragraph):
                    if hint.lower() in sentence.lower():
                        line = sentence.strip()
                        break
                if not line:
                    line = paragraph
                if len(line) > 160:
                    line = line[:157].rstrip() + "..."
                if line not in out:
                    out.append(line)
    return out


# --------------------------------------------------------------------- report
def _rule_stats(result):
    stats = {}
    for score in result.scores:
        for check in score.checks:
            passed, failed = stats.get(check.key, (0, 0))
            stats[check.key] = (passed + (1 if check.ok else 0),
                                failed + (0 if check.ok else 1))
    return stats


def _wrap(text, width=92, indent="   "):
    words = str(text or "").split()
    lines, line = [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return ("\n" + indent).join(lines) if lines else ""


def format_scorecard(result, prompt=None, persona_cfg=None, label="demo"):
    """Plain-text scorecard with the worst replies and the prompt lines to fix."""
    cfg = persona_cfg or (CFG.get("persona") or {})
    prompt = prompt if prompt is not None else persona.build_prompt()
    total = len(result.scores)
    passed = sum(1 for s in result.scores for c in s.checks if c.ok)
    checks = sum(len(s.checks) for s in result.scores)
    rate = (passed / float(checks) * 100.0) if checks else 0.0

    lines = ["Iris persona eval", "-" * 72]
    lines.append("{0} replies | {1} | free models only".format(total, label))
    lines.append("Persona: curiosity {0}%, humour {1}%, teasing {2}%".format(
        result.curiosity,
        persona.percent(cfg.get("humour", 0.7), 70),
        persona.percent(cfg.get("teasing", 0.6), 60)))
    lines.append("")
    lines.append("{0:<16} {1:>5} {2:>5} {3:>6}".format(
        "Rule", "pass", "fail", "rate"))
    stats = _rule_stats(result)
    for key in RULE_ORDER:
        if key not in stats:
            continue
        ok, bad = stats[key]
        lines.append("{0:<16} {1:>5} {2:>5} {3:>5.0f}%".format(
            RULE_LABELS.get(key, key), ok, bad,
            ok / float(ok + bad) * 100.0))
    lines.append("")
    lines.append("Overall: {0:.0f}% of checks passed ({1}/{2})".format(
        rate, passed, checks))

    low = max(0, result.curiosity - QUESTION_BAND)
    high = min(100, result.curiosity + QUESTION_BAND)
    asked = sum(1 for s in result.scores if has_question(s.reply))
    answered = sum(1 for s in result.scores if s.reply)
    verdict = "OK" if low <= result.question_rate <= high else "WARN"
    lines.append("Question rate: {0}/{1} = {2:.0f}% (target {3}%, band {4}-{5}%) {6}".format(
        asked, answered, result.question_rate, result.curiosity, low, high,
        verdict))

    if result.repeated:
        shown = ", ".join("{0} x{1}".format(k, v)
                          for k, v in sorted(result.repeated.items(),
                                             key=lambda kv: (-kv[1], kv[0])))
        lines.append("Repeated openers: " + shown)
    else:
        lines.append("Repeated openers: none over {0}".format(REPEAT_LIMIT))

    worst = worst_replies(result)
    lines.append("")
    if not worst:
        lines.append("Worst 3: none, every reply passed.")
        return "\n".join(lines)

    lines.append("Worst {0}".format(len(worst)))
    for i, score in enumerate(worst, 1):
        lines.append("")
        lines.append("{0}. {1} ({2}, {3:.0f}%)".format(
            i, score.case.id, CATEGORY_LABELS.get(score.case.category,
                                                  score.case.category),
            score.score * 100.0))
        lines.append("   you:  " + _wrap(score.case.message, indent="         "))
        lines.append("   iris: " + _wrap(score.reply, indent="         "))
        failed = [c for c in score.checks if not c.ok]
        lines.append("   failed: " + _wrap(
            "; ".join("{0} ({1})".format(c.key, c.detail) for c in failed),
            indent="           "))
        blame = blame_prompt([c.key for c in failed], prompt)
        if blame:
            lines.append("   prompt lines likely responsible:")
            for line in blame:
                lines.append("     - " + _wrap(line, indent="       "))
        else:
            lines.append("   prompt lines likely responsible: none found")
    return "\n".join(lines)


def to_json(result, label="demo"):
    """Machine-readable scorecard for comparing runs over time."""
    checks = [c for s in result.scores for c in s.checks]
    return {
        "label": label,
        "summary": {
            "replies": len(result.scores),
            "checks": len(checks),
            "passed": sum(1 for c in checks if c.ok),
            "rate": round(sum(1 for c in checks if c.ok)
                          / float(len(checks)) * 100.0, 1) if checks else 0.0,
            "question_rate": round(result.question_rate, 1),
            "curiosity": result.curiosity,
        },
        "repeated_openers": result.repeated,
        "cases": [
            {
                "id": s.case.id,
                "category": s.case.category,
                "upset": s.case.upset,
                "message": s.case.message,
                "reply": s.reply,
                "score": round(s.score * 100.0, 1),
                "checks": [{"key": c.key, "ok": c.ok, "detail": c.detail}
                           for c in s.checks],
            }
            for s in result.scores
        ],
        "worst": [s.case.id for s in worst_replies(result)],
    }


# ------------------------------------------------------------------ live runner
def select_cases(categories=None, limit=0):
    wanted = set(categories or ())
    cases = [c for c in CASES if not wanted or c.category in wanted]
    return cases[:limit] if limit and limit > 0 else cases


def _ask(prompt, history, message, max_tokens):
    from core import brain
    messages = ([{"role": "system", "content": prompt}] + history
                + [{"role": "user", "content": message}])
    result = brain.ask_stream(messages, tools=None, max_tokens=max_tokens)
    return (result.content or "").strip() if result else ""


def run_live(cases, prompt, gap=10.0, retry=25.0, attempts=3,
             max_tokens=180, history_turns=2):
    """Ask the free-model chain one case at a time. Returns partial replies."""
    from core import brain, connectivity

    if not connectivity.is_online():
        print("[eval] offline: no free model can answer. Use --demo instead.")
        return []
    try:
        models = brain.chain()
    except Exception as exc:  # noqa: BLE001 - report, do not traceback
        print("[eval] could not build the free chain: {0}".format(
            ascii_text(str(exc))[:160]))
        return []
    if not models:
        print("[eval] no free model has a key. Add one to .env or use --demo.")
        return []
    print("[eval] free chain: " + " -> ".join(
        "{0}/{1}".format(p, m) for p, m in models))
    print("[eval] {0} turns, gap {1:.0f}s, {2} attempt(s) per turn".format(
        len(cases), gap, max(1, attempts)))

    history, replies = [], []
    try:
        for i, case in enumerate(cases, 1):
            reply = ""
            for attempt in range(1, max(1, attempts) + 1):
                print("[eval] {0}/{1} {2} ({3}): {4}".format(
                    i, len(cases), case.id,
                    CATEGORY_LABELS.get(case.category, case.category),
                    ascii_text(case.message)))
                try:
                    reply = _ask(prompt, history, case.message, max_tokens)
                except Exception as exc:  # noqa: BLE001 - keep the run alive
                    print("[eval] model call failed: {0}".format(
                        ascii_text(str(exc))[:160]))
                    reply = ""
                if reply:
                    break
                if attempt < attempts:
                    wait = max(0.0, retry) * attempt
                    print("[eval] no reply; rate-limit backoff {0:.0f}s".format(wait))
                    time.sleep(wait)
            replies.append(reply)
            print("[eval] iris: " + ascii_text(reply or "(no reply)"))
            if reply:
                history.extend([{"role": "user", "content": case.message},
                                {"role": "assistant", "content": reply}])
                if history_turns > 0:
                    history = history[-2 * history_turns:]
                else:
                    history = []
            if i < len(cases) and gap > 0:
                time.sleep(gap)
    except KeyboardInterrupt:
        print("\n[eval] stopped early; scoring the replies collected so far.")
    return replies


# ------------------------------------------------------------------------ main
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Score how human Iris sounds, without changing the persona.")
    parser.add_argument("--demo", action="store_true",
                        help="score canned replies, no network or model calls")
    parser.add_argument("--category", action="append", choices=CATEGORIES,
                        help="only this category (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N cases")
    parser.add_argument("--gap", type=float, default=10.0,
                        help="seconds between live turns (default 10)")
    parser.add_argument("--retry", type=float, default=25.0,
                        help="base backoff when a turn gets no reply (default 25)")
    parser.add_argument("--attempts", type=int, default=3,
                        help="tries per turn before giving up (default 3)")
    parser.add_argument("--max-tokens", type=int, default=180,
                        help="reply budget per turn (default 180)")
    parser.add_argument("--history", type=int, default=2,
                        help="previous turns kept in context (default 2, 0 = none)")
    parser.add_argument("--helpdesk-file", default=None,
                        help="one helpdesk phrase per line; # comments allowed")
    parser.add_argument("--json", default=None,
                        help="also write the scorecard as JSON to this path")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        from core import dns
        dns.install()               # the same DNS fallback the assistant uses
    except Exception:               # noqa: BLE001 - the eval works without it
        pass

    cases = select_cases(args.category, args.limit)
    if not cases:
        print("No cases matched those filters.")
        return 2

    helpdesk = DEFAULT_HELPDESK
    if args.helpdesk_file:
        try:
            helpdesk = load_helpdesk(args.helpdesk_file)
        except OSError as exc:
            print("Could not read --helpdesk-file: {0}".format(exc))
            return 2
        if not helpdesk:
            print("--helpdesk-file had no phrases; using the built-in list.")
            helpdesk = DEFAULT_HELPDESK

    cfg = CFG.get("persona") or {}
    prompt = persona.build_prompt()
    if args.demo:
        replies = [DEMO_REPLIES.get(c.id, "") for c in cases]
        label = "demo (canned replies, no network)"
    else:
        replies = run_live(cases, prompt, gap=args.gap, retry=args.retry,
                           attempts=args.attempts, max_tokens=args.max_tokens,
                           history_turns=args.history)
        label = "live"
        cases = cases[:len(replies)]
        if not cases:
            print("No replies were collected; nothing to score.")
            return 1

    result = score_all(cases, replies, helpdesk=helpdesk, persona_cfg=cfg)
    print("")
    print(ascii_text(format_scorecard(result, prompt=prompt, persona_cfg=cfg,
                                      label=label)))
    if args.json:
        try:
            Path(args.json).write_text(
                json.dumps(to_json(result, label), indent=2, ensure_ascii=True),
                encoding="ascii")
            print("\nJSON written to " + ascii_text(args.json))
        except OSError as exc:
            print("\nCould not write --json: {0}".format(exc))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
