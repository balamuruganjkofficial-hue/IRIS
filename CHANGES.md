# Iris — Change Log / What Changed

**Date:** 16 November 2026
**Scope:** bug fixes, performance work, new interface, tests, project hygiene,
then a second pass for full laptop autonomy, voice-only permissions, in-interface
web search and a cybernetic HUD, a third pass for the Iris wake word and the
surface that holds everything, a fourth pass that makes her sound human, a
fifth pass for skills she writes herself, a warmer voice and a human avatar,
and a sixth pass that keeps her answering when a free model hits its limit.
**Cost:** still 100% free — no new paid services, APIs or hardware. The UI uses Python's built-in `tkinter`; speech, STT and the LLM continue to use the free tiers of Edge TTS, Google/Whisper and Groq.

---

## 1. How to run

```bat
cd C:\dev\IRIS
venv\Scripts\python.exe main.py
```

Plain `python main.py` works as well: if the project's virtual environment
exists and the current interpreter is a different one, `main.py` re-launches
itself with `venv\Scripts\python.exe` and passes every argument through. Set
`IRIS_VENV` to point at a different environment, or `IRIS_REEXEC=1` to disable
the hand-off entirely.

Useful flags:

| Command | What it does |
|---|---|
| `python main.py` | Dark sharp-red interface + voice + mic |
| `python main.py --console` | Terminal only, no window |
| `python main.py --no-greeting` | Skip the spoken hello at startup |
| `python main.py --console --no-greeting` | Quiet terminal start |
| `python main.py --doctor` | Plain-text health report, also saved to `logs/doctor.txt` |

Tests:

```bat
venv\Scripts\python.exe -m unittest discover -s tests -v
```

Wake word is read from `config.yaml` (default `iris`, spotted locally by Vosk).
Type `quit` in the console, press the window's X, or ask Iris to shut down.

---

## 2. New files

| File | Lines | Purpose |
|---|---:|---|
| `interfaces/ui.py` | ~1280 | The complete pink-red cybernetic interface (frameless window, canvas HUD, chat bubbles, dialogs, voice-permission overlay, search cards, live browser panel, resize/drag, preview demo) |
| `skills/laptop.py` | 450 | User-level laptop control: open/close any app, window or settings page, shortcuts, PowerShell escape hatch |
| `skills/websearch.py` | 154 | Free web/news search with in-interface clickable result cards |
| `interfaces/__init__.py` | 0 | Makes `interfaces` a package |
| `core/bus.py` | 31 | Tiny thread-safe event bus (`subscribe` / `publish` / `unsubscribe` / `has_subscribers`) so UI, voice, ears and agent never import each other |
| `core/text.py` | 33 | Shared text helpers: sentence splitting, markdown cleaning, tokenising, junk-transcript filter, safe goodbye detection |
| `core/net.py` | 41 | Public-address guard: validates IP literals, DNS results and every redirect hop before fetching |
| `tests/test_core.py` | 265 | 27 stdlib `unittest` tests (text, yes/no, net, skills, memory, workspace sandbox, settings links, protected processes, result normalising, free-model fallback) |

---

## 3. Modified files (what and why)

### `main.py` — rewritten lifecycle
- Assistant loop runs in a **daemon thread**; Tk `mainloop()` owns the main thread (Tk is not thread-safe).
- Rotating log file: `logs/iris.log`, **2 MB × 3** backups (previously unbounded).
- **Single-instance lock** on `127.0.0.1:47653` — autostart + manual run can no longer fight over the mic.
- Wake word and chimes now come from config, not hard-coded strings.
- **Fixed the accidental-sleep bug**: "thanks, open Chrome" no longer ends the session; only an actual goodbye does (`core/text.py::wants_sleep`).
- `--console` and `--no-greeting` flags added.
- Works with `pythonw` (no console): stdout/stderr redirected to `logs/console.log`.
- Added `shutdown()` / `_really_exit()`: closes browser, ears, audio, logging cleanly.
- UI answers destructive-action confirmations through a modal dialog when not in voice mode.
- Microphone errors and agent failures publish a toast instead of killing the loop.
- `sys.setswitchinterval(0.005)` for snappier thread hand-offs.
- Typed input, wake word, barge-in and timeouts all flow through one event queue.

### `core/config.py` — rewritten
- `DEFAULTS` dictionary + **deep merge**, so a trimmed `config.yaml` still boots.
- Exposes `DATA`, `LOGS`, `WORKSPACE`, `ROOT`, `CFG` and `get(*keys)`.
- Environment overrides: `IRIS_CONFIG`, `IRIS_DATA_DIR`, `IRIS_WORKSPACE`.
- Creates data/logs/workspace folders on import.
- `.env` loaded after config (so API keys are available to `stt`/`brain`).
- New sections: `brain`, `ui`, memory `max_turns`, voice `stt_models` / `cache_*` / `chime`.

### `core/bus.py` — new
- One lock-protected subscriber map; listeners run on the publisher's thread.
- A failing listener is swallowed so one bad callback can never hurt Iris.
- Topics used: `state`, `say`, `reply`, `heard`, `tool`, `level`, `latency`, `toast`, `quit`, plus `auth`, `view` and `results` for the autonomy/cyber upgrade (section 10).

### `core/text.py` — new
- `SENT_RE`, `clean()`, `sentences()`, `tokens()`, `is_junk()`, `wants_sleep()`.
- `is_junk` filters silence/noise/whisper fillers (`um`, `hmm`, `thanks for watching`).
- `wants_sleep` requires the whole utterance to be a short farewell, fixing the false-sleep bug.
- `answer()` parses spoken yes/no answers, used by voice permissions (section 10).

### `core/net.py` — new
- `ip_is_public`, `host_is_public`, `is_public_url`, `get_public`.
- Rejects private, loopback, link-local, multicast, reserved and unspecified addresses.
- Follows redirects **manually**, re-validating each hop (closes the open-redirect hole).

### `core/voice.py` — rewritten TTS pipeline
- Two background workers: synthesiser (one reused asyncio loop) and player.
- pygame mixer initialised once with a low-latency buffer (`1024`), hidden support prompt.
- Disk cache for short lines, **periodic prune** (400 files max, every 15 min) and abandoned-temp cleanup — the cache can no longer grow forever.
- `speak()`, `wait_done()`, `is_speaking()`, `stop()` (real barge-in: drops queues and cuts audio).
- Prewarms common phrases ("Done.", "Opened Chrome." …) so replies start instantly.
- Publishes `state` (`speaking`/`idle`) and `toast` on failure.
- Safe when there is no sound card (logs a warning, keeps running).

### `core/ears.py` — rewritten microphone loop
- One PyAudio stream, 80 ms frames, idle/listen/busy modes.
- Supervisor thread automatically restarts the stream after mic unplug/sleep-resume.
- Noise floor adapts while idle; voice-activity detection ignores clicks and coughs.
- Wake word thresholds, barge-in, VAD RMS and end-silence come from config.
- Publishes smoothed mic **level** only when the UI is listening (no wasted work in console mode).
- `flush()` and `close()` added for clean shutdown and confirmations.

### `core/stt.py`
- Uses `core.text.is_junk` instead of duplicated logic.
- Model list from `stt_models` config (turbo first, fallback second).
- Language derived from `stt_language`; Google fallback kept.
- Free-tier friendly client: 8 s timeout, zero retries.

### `core/brain.py` — rewritten streaming client
- Groq streaming with sentence-level callbacks (`on_sentence`) and tool-`say` extraction (`on_say`).
- Primary + fallback model, one polite retry on rate limits, short retry on connection/timeout errors.
- `warmup()` opens the HTTPS connection early so the first real reply is faster.
- Never restarts a reply that already began speaking.
- Reasoning effort low, configurable max tokens/temperature.

### `core/agent.py` — the tool loop
- Publishes `state` / `latency` / `say` / `tool` / `reply` to the bus for the UI.
- Config-driven `max_steps` and `max_tool_calls`.
- Drops all non-browser tools once she starts browsing (faster, fewer tokens).
- Trims old page snapshots to save tokens.
- Uses shared `SENT_RE`, streams sentences to TTS immediately.
- Cancellation (`interrupt`) stops the loop and speech.

### `core/memory.py` — rewritten persistence
- One SQLite connection **per thread**, WAL mode, `synchronous=NORMAL`, busy timeout.
- `max_turns` bound + automatic pruning.
- Facts use **FTS5** search with a plain-Python scoring fallback for SQLite builds without FTS5.
- `init(db_path)` for tests, `close()`, `facts()`, `forget_fact()`.

### `core/skill.py`
- `tool_schemas(prefix)` — can return only browser tools.
- Clearer `say` field description for the model.
- Encoding cleanup (UTF-8 BOM removed).

### `core/wake.py`
- Encoding cleanup only (UTF-8 BOM removed). No behaviour change.

### `skills/hands.py`
- Removed duplicated/weaker `_public_url`; now uses `core.net.get_public` (validates redirects too).
- `WORKSPACE` comes from `core.config` (env-overridable).
- Unused imports (`ipaddress`, `socket`, `urlparse`, `ROOT`) removed.
- `stop_iris` publishes a `quit` bus event so the UI closes too.

### `skills/browser_agent.py`
- `browser_open` now uses `core.net.is_public_url` (DNS-aware, covers all private ranges).
- Removed unused `urlparse` import.

### `skills/basic.py`
- `screenshot()` uses `ImageGrab.grab(all_screens=True)` with an older-Pillow fallback and error return.
- `notify()` catches failures instead of crashing.

### `config.yaml`
- Rebuilt and grouped: `persona`, `models`, `brain`, `voice`, `memory`, `paths`, `chrome`, `ui`.
- Existing Chrome profile mapping preserved.
- New UI palette, size, FPS and always-on-top keys.
- New voice tuning keys: `stt_models`, `barge_in`, `barge_in_threshold`, `vad_min_rms`, `end_silence`, `wake_debug`, `chime`, `cache_chars`, `cache_files`.

### `requirements.txt` — rebuilt
Direct dependencies only, versions matching the tested venv:

```
beautifulsoup4, ddgs, edge-tts, groq, lxml, numpy, openai, openwakeword,
Pillow, playwright, plyer, psutil, PyAudio, PyAutoGUI, pygame,
python-dotenv, pywin32, PyYAML, requests, screen-brightness-control,
SpeechRecognition
```

Browser skills still need one free command: `python -m playwright install chromium`.

### `.gitignore` — rewritten
- Fixed the broken `__pycache__/workspace/` line (used to ignore nothing).
- Now ignores `venv/`, `.env`, `keys/`, `data/`, `logs/`, `workspace/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `*.log`, OS junk.

### `__pycache__` files
- Untracked/staged for removal from git (they were committed binaries).
- BOM (UTF-8 signature) removed from 9 Python files for clean, consistent encoding.

---

## 4. Deleted files

| File | Reason |
|---|---|
| `core/listen.py` | Dead code — superseded by `core/ears.py` and no longer imported anywhere |
| `core/__pycache__/*`, `skills/__pycache__/*` | Compiled artefacts removed from version control and ignored |

---

## 5. The new interface (`interfaces/ui.py`)

**Look**

- Frameless window, deep plum/black gradient, pink-red accent (`#FF2E63`).
- **IRIS** centred in large type with a subtle shadow and gentle float.
- Breathing halo, rotating arcs, 24 orbiting particles, expanding ripples while listening.
- 28-bar equaliser reacting to the microphone while listening and animating while speaking.
- Status text (`ready`, `listening…`, `thinking…`, the running tool, or a toast).
- "first word X.Xs" latency readout in the corner.
- Colors and size come from the `ui:` block in `config.yaml`.

**Interaction**

- Drag from the title bar; double-click to maximise.
- Corner grip to resize; the stage, halo, type and equaliser all re-layout live.
- Minimise / maximise / close.
- Chat transcript with rounded bubbles (yours right, Iris left), scroll wheel.
- Streamed replies: the bubble grows as she talks.
- Text box (placeholder, focus colour), Enter to send, Send button.
- Mic button = start listening (same as pressing Enter on an empty line).
- `Esc` = stop speech/cancel (barge-in).
- Yes/No modal for anything risky or irreversible (voice confirmation in voice mode).
- Toasts for mic/audio/network hiccups.

**Architecture / smoothness**

- The assistant never touches Tk widgets; bus callbacks only push to a `queue.Queue`.
- Tk main loop drains that queue and animates at 30 fps (configurable).
- Transcript rebuilding is debounced (120 ms) so window resizing stays fluid.
- Canvas items are updated in place — no widget churn per frame.
- If the animation tick ever throws, it logs and continues instead of freezing.
- DPI aware (125 % Windows scaling handled).
- Preview demo: `venv\Scripts\python.exe interfaces\ui.py`.

---

## 6. Bug fixes at a glance

| Problem found | Status |
|---|---|
| `requirements.txt` did not match the venv | Fixed, direct deps pinned |
| `__pycache__` committed to git | Removed, ignored |
| Dead `core/listen.py` | Deleted |
| Wake word hard-coded | From config |
| "thanks …" ended the conversation | Fixed (`wants_sleep`) |
| URL guard bypass (DNS / redirects) | Central `core/net.py` |
| Unbounded log file | Rotating handler |
| TTS cache grew forever | Capped + pruned every 15 min |
| SQLite concurrency / no fact search | WAL, thread-local connections, FTS5 |
| Trimmed config crashed | Defaults + deep merge |
| No tests | 27 passing |
| Screenshot/notify crashes | Error-handled |
| Unused imports/deps | Removed |
| Blocking calls froze the UI | Worker threads + event bus |
| UI could freeze on one exception | Tick is now fault-tolerant |
| Two instances competed for the mic | Single-instance lock |
| No clean shutdown | `shutdown()` / `_really_exit()` |

---

## 7. Verification performed

- `python -m compileall -q main.py core skills interfaces tests` → exit 0.
- `python -m unittest discover -s tests` → **Ran 27 tests … OK**.
- UI smoke test (real Tk window):
  - 1176 × 825 at 125 % scaling; stage 1176 × 523.
  - IRIS letters centred; halo fully inside the canvas; status/bars/hint positioned.
  - Resize to 760 × 520 → everything re-laid out, bubbles rebuilt.
  - Dialog confirmed via `Esc` and via timeout.
  - Result: **SMOKE OK**.
- Second-pass smoke test (`smoke2.py`):
  - Cyber HUD: 48 radial ticks, 16 data-rain columns, 142 grid/canvas items.
  - Permission HUD shows `PERMISSION REQUIRED` → `ACCESS GRANTED`.
  - Result cards render and click through to the browser opener.
  - Live JPEG view fills the intel panel, survives a resize to 780 × 540,
    hides again and clears on `view: closed`.
  - `ui.cyber: false` hides 77/77 decor items and skips all decor animation.
  - Result: **SMOKE2 OK**.
- End-to-end browser check: local Iris search page rendered 6 real results,
  `browser_click(1)` followed a result, `browser_click_text` followed a
  Wikipedia section link, `browser_close` cleared the view.
- Live `main.py --console` and UI runs: 9–10 s each, no stderr output.
- Screenshot of the rendered window saved to `data/ui_preview.png`.

---

## 8. Repo state

All changes are **uncommitted**. Staged already:

- deletion of `core/listen.py`
- deletion of tracked `core/__pycache__` and `skills/__pycache__` files

New, not yet added: `core/bus.py`, `core/net.py`, `core/text.py`,
`interfaces/`, `tests/`, `skills/laptop.py`, `skills/websearch.py`.

To commit everything:

```bat
git add -A
git commit -m "Add cyber UI, voice permissions, laptop control, in-interface web search, headless browser mirroring and free-model fallback"
```

---

## 9. Notes / limits

- Everything still runs on **free** services. Groq's free tier has rate limits; Iris waits and retries automatically, then falls back to the second model.
- The wake word is `iris`, spotted locally by Vosk's small English model; openWakeWord's bundled `hey_jarvis` model is only a fallback when Vosk is unavailable. Change `voice.wake_word` to use another bundled openWakeWord model.
- Browser tools need the one-time free `playwright install chromium`.
- `data/` and `logs/` are intentionally ignored by git; the UI preview lives at `data/ui_preview.png` on this machine.

---

## 10. Second pass — autonomy, voice permissions, in-interface web

**Goal:** Iris runs the laptop herself, asks permission out loud, searches and
controls the web while still talking, and shows every result inside her own
pink-red interface. Still free, still ordinary user rights — **no admin and no
UAC elevation** anywhere.

### 10.1 Voice-first permissions

- `core/text.py::answer()` turns a spoken phrase into `True` / `False` /
  `None`: yes (`yes, yeah, sure, go ahead, do it, confirm, granted…`),
  no (`no, nope, cancel, don't, never mind, stop…`) and unclear.
- `core/ears.py` now exposes `mic_ok`, so a confirmation can tell whether the
  microphone stream is actually live before trying to listen.
- `main.py::_voice_confirm()` is the primary path:
  1. publishes `auth` with the question,
  2. stops any narration, plays the `ask` chime,
  3. speaks the question, flushes the mic and listens for a spoken answer,
  4. retries once (`"Sorry. Yes, or no?"`) on silence or an unclear answer,
  5. publishes `granted`, `denied` or `timeout` and resets the state.
- `main.py::confirm()` waits up to 2.5 s for the mic to come up on a cold
  start, then uses the clickable modal only if the mic is unavailable and
  `permissions.click_fallback` is on. Console mode still asks in the terminal.
- Only genuinely risky actions ask: deleting files, powering off, running an
  arbitrary command, or a risky web click (buy/pay/send/delete/submit). Opening
  and closing apps, moving windows, opening settings and ordinary browsing run
  automatically.

### 10.2 Whole-laptop control (`skills/laptop.py`)

| Skill | What it does |
|---|---|
| `open_app` | Opens a known app or any Start-menu entry by name |
| `open_anything` | Opens an app, file, folder, URL or protocol with the shell |
| `close_app` | Terminates a user app (protected system processes are refused) |
| `close_window` | Sends a close request to a matching window |
| `window_action` | Focus, minimise, maximise or restore a window |
| `show_desktop` / `press_keys` | Desktop shortcut and global hotkeys |
| `list_windows` / `list_apps` | Read what is open and what is installed |
| `run_command` | PowerShell fallback (level 2: asks first, destructive disk/user/registry patterns blocked) |

- App lookup reads the Start-menu shortcuts, so anything the user can launch
  normally, Iris can launch too — no administrator rights involved.
- System-critical processes (`lsass`, `csrss`, `winlogon`, services…) and
  Iris' own window are explicitly protected.
- `skills/system.py` now carries a broad `ms-settings:` map with aliases and a
  fuzzy resolver, so `open_settings("touchpad")`, `"windows security"` or
  `"date and time"` deep-link straight to the right page.
- Duplicate `open_app` / `close_app` / `APPS` / `PROCS` definitions were
  removed from `skills/basic.py`; `skills/laptop.py` owns laptop control now.

### 10.3 Web search shown inside Iris (`skills/websearch.py`)

- `web_search` and `web_news` query DuckDuckGo's free HTML results through
  `ddgs`, normalise every hit (title, snippet, source, date, URL), remember the
  last set and publish them on the `results` bus topic.
- The interface draws them as clickable **INTEL // WEB SEARCH** cards while
  Iris keeps speaking. `last_results` redraws the last set; `open_result(n)`
  opens result *n* in her own browser.
- The model receives a short text summary only. URLs are never read aloud.
- The old weak `web_search` in `skills/hands.py` was deleted so there is one
  search path.
- Config: `web.max_results`, `web.show_in_ui`.

### 10.4 Headless browser mirrored into the interface

- `skills/browser_agent.py` was rewritten around **one dedicated worker
  thread** with a request queue (Playwright's sync API is not thread-safe).
  The assistant, the UI and speech never block on the page.
- Headless Chromium/Chrome runs with a **persistent profile**
  (`data/browser_profile`), so logins survive between runs. Headless is the
  default; `browser.headless` can flip it.
- After every action a JPEG snapshot is written to `data/browser_shots/`
  (old files pruned) and published as a `view` event. The UI shows it as a
  live **INTEL // LIVE VIEW** panel — no external Chrome window.
- Tools: `browser_open`, `browser_search`, `browser_look`, `browser_click`,
  `browser_click_text`, `browser_type`, `browser_select`, `browser_press`,
  `browser_scroll`, `browser_read`, `browser_back`, `browser_forward`,
  `browser_reload`, `browser_wait`, `browser_view`, `browser_close`.
- Clicking a search card calls `main.open_in_iris(url)`, which queues the
  navigation on the browser worker; the interface stays smooth and Iris can
  keep talking during the page load.
- Risky buttons (buy/pay/send/delete/submit…) still route through the same
  voice permission. Password fields are never typed into.

### 10.5 Cybernetic interface (same pink-red palette)

- Background grid, drifting scanlines, 48 radial telemetry ticks, 16 columns
  of data rain, corner brackets, and live readouts (`SYS`, `MIC`, `FRAME`,
  `AUTH`, `HUD`).
- New **INTEL** panel on the right opens for search cards or the live browser
  view. When it opens, IRIS shifts left and the halo/type/equaliser shrink to
  fit; when it closes they re-centre. Resizing keeps both layouts correct.
- Result cards are scrollable, show number/title/snippet/source and an
  `OPEN IN IRIS ›` action; the whole card is clickable.
- The permissions overlay sits at the bottom centre, becomes `ACCESS
  GRANTED` / `ACCESS DENIED` / `NO RESPONSE` / `MIC UNAVAILABLE`, and resizes
  around the intel panel.
- `ui.cyber: false` hides all decor items (verified 77/77) and skips the decor
  animation, leaving the clean pink interface.
- Everything stays on the Tk main thread; bus callbacks only enqueue.

### 10.6 Config additions

```yaml
permissions:      # voice-first confirmations
  voice_confirm: true
  timeout: 9
  attempts: 2
  click_fallback: true
browser:          # headless mirror
  headless: true
  show_in_ui: true
  capture_quality: 70
  capture_width: 1280
  keep_shots: 40
web:              # in-interface results
  max_results: 6
  show_in_ui: true
ui:
  cyber: true     # HUD animation on/off
```

### 10.7 Limits (honest ones)

- **No admin / no UAC.** Iris uses the user's own rights. Anything that
  requires elevation (installing services, changing protected system settings)
  cannot and will not be attempted.
- **Search engines block headless browsers.** DuckDuckGo, Brave, Startpage and
  Mojeek all serve a CAPTCHA, 403 or human-check to the headless session. So
  `browser_search` renders an Iris-styled results page from the free DuckDuckGo
  result API and puts real, clickable links on it. `web_search` never needs a
  page at all.
- **Cloudflare-protected sites** may still show "Just a moment…" in the live
  view; `browser_wait` gives them time, and `browser_read` reports what Iris
  can actually see.
- Playwright still needs the one-time free `python -m playwright install
  chromium`; a system Chrome is used automatically when available.
- If the mic is unavailable, confirmations fall back to the clickable modal or
  the console (configurable).

---

## 11. Free-model fallback chain

**Problem:** the primary could fail — Groq's free tier hit a per-minute or
daily limit, the service returned a 5xx, the connection timed out, or the model
sent an empty reply — and the only fallback was a second model on the *same*
Groq account and the *same* limit. One bad minute meant "I'm having trouble
reaching my brain right now."

**Now:** `core/brain.py` builds an ordered chain of free, tool-capable models
and walks it until one answers. A failing model costs seconds, not the reply.

### Order of fallback

1. `models.llm` then `models.fallback` — the configured free Groq models.
2. Any `models.free_fallbacks` entries (`{provider, model}`).
3. **OpenRouter free models**, discovered live from the public models endpoint,
   cached for an hour. Only IDs ending in `:free` that advertise tool support
   are kept, so a paid model can never be selected.
4. Google Gemini's free tier, Cerebras, Mistral and GitHub Models — each only
   when its key exists in `.env`.
5. A local **Ollama** model, only if listed in `models.ollama_models`
   (free and keyless, but opt-in so it is never probed unexpectedly).

Each model gets one extra attempt for a transient timeout/5xx. Rate limits,
quota errors, bad keys, missing models and empty replies move straight to the
next model. If a model has already spoken before failing, the chain stops
rather than restarting a reply mid-sentence. If *every* model is rate limited
with a short wait (≤ 15 s), Iris says "One moment.", waits and retries the
chain once. The whole walk is bounded by `brain.fallback_budget` (50 s).

### Free-only guarantee

- OpenRouter is filtered by `:free` both in live discovery and in the manual
  `models.openrouter_models` override.
- Gemini / Cerebras / Mistral / GitHub are used on their free tiers only, and
  only if the matching key is present.
- Ollama is local and free.
- The `openai` package is just the client library (Apache-2.0); using it does
  not add any paid service.

### Keys (all optional)

| Provider | Environment variable | Default free model |
|---|---|---|
| Groq | `GROQ_API_KEY` | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` |
| OpenRouter | `OPENROUTER_API_KEY` | discovered `:free` tool models |
| Google Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `gemini-2.5-flash` |
| Cerebras | `CEREBRAS_API_KEY` | `llama-3.3-70b` |
| Mistral | `MISTRAL_API_KEY` | `mistral-small-latest` |
| GitHub Models | `GITHUB_TOKEN` | `openai/gpt-4o-mini` |
| Ollama (local) | — | whatever is in `models.ollama_models` |

This machine currently has `GROQ_API_KEY` and `OPENROUTER_API_KEY`, so the
chain is Groq (two models) → four current OpenRouter `:free` models.

### Config

```yaml
models:
  llm: openai/gpt-oss-20b
  fallback: openai/gpt-oss-120b
  free_fallbacks: []            # extra {provider, model} pairs
  openrouter_free: true         # auto-discover free tool models
  openrouter_models: []         # optional fixed :free IDs
  openrouter_max: 4
  gemini_model: gemini-2.5-flash
  cerebras_model: llama-3.3-70b
  mistral_model: mistral-small-latest
  github_model: openai/gpt-4o-mini
  ollama_models: []
brain:
  request_timeout: 20           # per provider
  fallback_budget: 50           # total seconds for one reply
```

### Verification

- `python -m compileall …` → **COMPILE OK**.
- `python -m unittest discover -s tests` → **Ran 27 tests … OK** (5 new tests
  cover status/rate detection, provider kwargs, chain ordering, the free-only
  OpenRouter filter and the all-limited wait/retry path).
- Live chain on this machine: `groq/gpt-oss-20b` → `groq/gpt-oss-120b` →
  four OpenRouter `:free` models.
- Live normal reply: served by Groq, first words in 1–2 s.
- Forced failure test (both Groq models raising 429):
  `openrouter/inclusionai/ling-3.2-flash:free` answered
  **"Fallback works."** — total 8.4 s, no error spoken.
- A free fallback can be slower or a little less sharp than Groq; it is a
  safety net for limits and outages, not the everyday path.

---

## 12. Third pass - the name Iris, and a surface that holds everything

**Goal:** Iris should be fully autonomous on this laptop, but nothing she opens
may spill into a separate window. Chrome, apps, files, folders, pictures and
even the desktop itself have to appear *inside her own dark red interface*.
Her wake name also changes from Jarvis to **Iris**.

Still 100% free: Vosk, Playwright, Pillow and pywin32 are all open source or
already installed, and no paid API or hardware was added.

### 12.1 Wake word: "Iris"

`core/wake.py` was rewritten around one small interface (`load`, `predict`,
`reset`) so the microphone loop no longer cares which engine is running.

- **Primary engine - Vosk.** `vosk==0.3.45` is free and runs entirely offline.
  The 41 MB `vosk-model-small-en-us-0.15` model lives in
  `data/vosk/vosk-model-small-en-us-0.15` and is downloaded automatically on
  first run if it is missing (progress goes to a toast).
- **Grammar spotting.** The recogniser is restricted to a tiny grammar built
  from `voice.wake_phrases`, so "iris", "hey iris", "hi iris", "hello iris",
  "okay iris" and "ok iris" all wake her - and "Jarvis" still works as an
  alias. Anything unrelated ("irish stew") is ignored.
- **Fallback engine - openWakeWord.** If Vosk is not installed, Iris falls back
  to openWakeWord. There is no pretrained "Iris" model there, so it uses
  `voice.wake_fallback` (`hey_jarvis`) and logs that plainly. A missing
  "iris" model can no longer break the fallback path.
- `core/ears.py` keeps one stream open: `idle` waits for the wake phrase,
  `listen` captures one sentence, `busy` treats the wake phrase as a barge-in.
- `main.py` and `interfaces/ui.py` now greet and hint with "Say iris whenever
  you need me" / `say "iris" to wake`; `fix_voice_cfg.py` repairs the config.

Barge-in and voice-confirmed permissions are unchanged: Iris still asks out
loud and accepts spoken yes/no, with the clickable modal only as a mic
fallback.

### 12.2 The surface engine

New module `skills/surface.py` (about 490 lines). A *surface* is one panel
inside Iris' interface; skills publish a description and `interfaces/ui.py`
renders it. Supported kinds:

| Kind | What it shows |
|---|---|
| `browser` | live headless-browser mirror, with an address bar and click/scroll mapping |
| `image` | a picture scaled to fit the panel |
| `text` | documents, source code, logs, notes |
| `folder` | a scrollable, clickable directory listing with sizes |
| `app` | a real application window mirrored live, with input forwarding |
| `screen` | the whole desktop, streamed live or captured as a snapshot |
| `results` | the search-result cards from `web_search` |
| `dashboard` | CPU / RAM / disk / battery bars, network, uptime, top processes |
| `info` | a small status card |

Public skills: `show_text`, `show_image`, `show_file`, `show_folder`,
`show_dashboard`, `show_screen`, `show_window`, `open_app_inside`,
`open_anything_inside`, `show_settings`, `close_surface`. Bare site names are
recognised ("youtube", "gmail", "github", "spotify", "chatgpt", ...), as are
"browser", "settings" and "files".

### 12.3 Live frames for apps and the desktop

New module `skills/capture.py` (about 300 lines) owns exactly one daemon
worker thread. It grabs a window with `PrintWindow` first (works even when the
window is covered or behind), falls back to `BitBlt`, skips all-black frames,
resizes to `browser.capture_width`, writes a JPEG and publishes
`surface_frame` on the bus. The UI swaps the image in place, so the app looks
like it is running inside Iris.

- Frames are pruned to the newest 24 files in `data/surface_frames`.
- Frame rate is `surface.live_fps` (default 6), quality is
  `browser.capture_quality`.
- Minimised or destroyed windows publish `state: lost` instead of an error;
  the interface just keeps the last picture.
- Clicking, scrolling, key presses and typed text are posted to the real
  window with the client-area offset corrected. This is **best effort** -
  some apps ignore synthetic `PostMessage` input - but nothing needs admin
  rights and nothing can freeze the Tk thread, because capturing and input
  both run off it.

### 12.4 No more external Chrome

Web work never leaves the interface now:

- `skills/browser_agent.py` keeps Playwright headless (`browser.headless: true`,
  `browser.external_chrome: false`) and publishes every screenshot as a
  `browser` surface with the viewport size, so clicks and wheel events map
  back to the real page coordinates.
- `browser_home` renders Iris' own dark-red start page (search box plus cards
  for YouTube, Wikipedia, GitHub, Gmail, Maps, Reddit, Stack Overflow and
  Spotify) as a real HTML page inside the mirror.
- `browser_search` renders the free DuckDuckGo result set as an Iris-styled,
  clickable results page; clicking a card opens it in the same surface.
- `browser_open_file` shows local HTML/PDF/media in the mirror; local files
  are allowed by `browser.local_files`.
- The UI's toolbar has back / forward / reload / home plus an address bar;
  typing a URL opens it, typing words searches.
- `skills/laptop.py`, `skills/basic.py` and `skills/system.py` now route
  `open_app`, `open_anything`, `open_chrome`, `google_search`, `open_website`,
  `screenshot` and `open_settings` into the surface instead of `os.startfile`.

### 12.5 A darker, sharper red interface

The palette moved from pink-red to a near-black red:

```yaml
ui:
  bg:           '#060407'
  bg2:          '#12060A'
  surface:      '#1A070C'
  surface_hi:   '#2A0A12'
  accent:       '#FF1F3D'
  accent_soft:  '#FF5C73'
  text:         '#F7ECEF'
  muted:        '#A07A82'
  user_bubble:  '#4A0818'
  iris_bubble:  '#1A070C'
  width: 1020
  height: 700
```

The surface panel slides in from the right, takes 70% of the stage
(`surface.ratio`) and never covers the microphone orb. While a surface is
open the big IRIS wordmark dims so the content leads, and the orb re-centres
with it. The HUD, scanlines, rain and telemetry animation are unchanged.

### 12.6 Config additions

```yaml
voice:
  wake_word: iris
  wake_engine: auto            # auto | vosk | openwakeword
  wake_phrases: [iris, hey iris, hi iris, hello iris, okay iris, ok iris, jarvis, hey jarvis]
  wake_fallback: hey_jarvis    # openWakeWord model used only if Vosk is absent
  wake_threshold: 0.35
  vosk_model: data/vosk/vosk-model-small-en-us-0.15
  vosk_download: true
  vosk_url: https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
browser:
  local_files: true
  external_chrome: false
surface:
  enabled: true
  ratio: 0.70
  live_fps: 6
  embed_apps: true
  park_windows: true
ui:
  surface_ratio: 0.70
```

New dependency in `requirements.txt`: `vosk==0.3.45` (free, offline).

### 12.7 Verification

- `python -m compileall -q main.py core skills interfaces tests` -> no errors.
- `python -m unittest discover -s tests` -> **Ran 31 tests ... OK** (new tests
  cover wake-phrase construction, alias matching and the surface URL/entry
  helpers).
- Wake word: a synthetic "Hey Iris" clip spotted `partial 0.80`, then
  `final 0.98`; reset returns the engine to idle.
- Surface UI smoke: browser (toolbar, address bar, image, click and wheel
  mapping), text, folder, dashboard, app, screen, results and info all render;
  resize to 760x520 keeps the panel usable; the close button clears the panel.
- Live capture smoke: **12 screen frames in 2 s** (1920x1080) and **11
  Notepad frames in 2 s** (1152x592), with click, scroll, key and typed text
  forwarded without errors.
- Surface skills smoke: text, image, folder (714 items), dashboard, snapshot
  screen, live Notepad mirror and a clean "file not found" error.
- Browser smoke: the start page renders at about:blank with the right title,
  and a real search ("python programming language") produces a clickable
  results page inside the surface.
- `data/ui_preview.png` regenerated at 1276x875: **91.7% near-black pixels,
  2.5% red accent, mean RGB (23.6, 8.2, 13.1)**, 5,527 unique colours.

### 12.8 Limits, honestly

- **No admin / no UAC.** Unchanged: Iris uses the user's own rights.
- **Some apps ignore synthetic input.** The mirror always displays; typing
  into apps that require real focus may not always land. This is best effort
  by design - the alternative (re-parenting or exclusive focus stealing) is
  far more fragile.
- **DRM and hardware-accelerated video** (Netflix in the mirror, some games)
  may render black in a window capture; the desktop stream catches more.
- **Vosk is a small English model.** Very noisy rooms can still miss a quiet
  "Iris"; `voice.wake_debug: true` prints what it hears, and
  `voice.wake_threshold` tunes sensitivity.

---

## 13. Fourth pass - stop sounding like a helpdesk

**Goal:** she was replying correctly and sounding like a product manual. She
should be a person: warm, curious, a little cheeky, able to ask a question of
her own, poke fun at his 3 a.m. searches, disagree, and carry a conversation
instead of waiting for the next command. Still 100% free - this pass adds no
service, model or hardware, only wording, prosody and timing.

### 13.1 A personality with knobs, not adjectives

New pure module `core/persona.py` builds the system prompt and can be tested
without starting the mic, the voice or the model chain:

- `percent(value)` turns a `0..1` knob into a whole percentage, so the prompt
  reads "About 60% of your replies carry a jab" instead of a dangling number.
- `build_prompt(facts, config, now)` renders the persona, the current time and
  remembered facts into one prompt.

The `persona` block in `config.yaml` controls how human she is - `humour`,
`teasing`, `curiosity`, `opinions`, `proactive`, plus the idle timings. Nothing
important is hard-coded in the agent any more: `core/agent.py`'s
`system_prompt()` is now a one-line delegate to `build_prompt()`.

What the prompt actually tells her to do:

- talk, never write: contractions, fragments, one to three short sentences;
- react before answering ("Ha.", "Oof.", "Right.") without opening every reply
  the same way;
- tease like a friend - dry, specific, about his choices, never about his
  body, family, money or anything he is sore about;
- ask exactly one real follow-up question, because she is curious, not because
  silence is awkward;
- help first and joke after when he is already frustrated; drop the act
  entirely when he sounds tired or serious;
- disagree and say when he is wrong, then do what he asked;
- call him "boss" now and then, not every line.

`brain.temperature` moved from `0.5` to `0.7` so replies stop converging on the
same safe sentence.

### 13.2 A voice with a pulse

`core/text.py` gained `prosody(text, rate, pitch, ...)`: a deterministic,
hash-based tweak of up to **±4% rate** and **±3 Hz pitch** per phrase. The same
sentence always gets the same tweak, so the TTS cache still hits, but
neighbouring sentences drift a little instead of one flat monotone. The rate
and pitch are now part of `core/voice.py`'s cache key, and the default
`voice.rate` softened from `+10%` to `+8%`.

`core/text.py`'s `clean()` also strips pseudo function-call tags
(`<tool_code>`, `<function=...>` and friends) that free models occasionally
leak into spoken text.

### 13.3 Real conversation, not command/response

- A quick thank-you is no longer a goodbye. New `just_thanks()` recognises
  "thanks", "cheers boss", "thank you so much" and similar, answers with a warm
  `THANKS_ACKS` line, and keeps the microphone listening. "Thanks, open Chrome"
  is still a command.
- Goodbyes are varied and cheeky (`BYE_ACKS`), and typed input gets the same
  treatment as speech.
- Greetings depend on the hour: morning, afternoon, evening and a late-night
  line for the 1 a.m. crowd. The battery warning now sounds like her too.
- `_maybe_banter()` speaks one gentle unprompted line after a long silence
  (`persona.idle_minutes`, default 25) with a floor between lines
  (`idle_gap_minutes`, default 45). It stays quiet while she is listening,
  while she is speaking, and while a live surface is streaming - a movie is
  not silence. Custom lines can be supplied in `persona.idle_lines`.
- `main.py` touch-tracks real activity (spoken, typed and `surface_frame`
  traffic) so idle banter only fires when he has genuinely gone quiet.

### 13.4 Config additions

```yaml
persona:
  name: Iris
  call_me: boss
  vibe: warm, playful, quietly cheeky
  humour: 0.7          # how often a reply carries a joke or a jab
  teasing: 0.6         # how much she mocks you - never cruelly
  curiosity: 0.7       # how often she asks a real follow-up question
  opinions: true       # she may disagree and say what she actually thinks
  proactive: true      # one gentle line after a long silence
  idle_minutes: 25     # minutes of silence before that line
  idle_gap_minutes: 45 # minimum gap between two idle lines
  idle_lines: []       # optional custom lines, {call} is replaced
brain:
  temperature: 0.7
voice:
  rate: +8%
  pitch: +0Hz
  prosody: true        # subtle per-phrase pitch/rate variation
  rate_jitter: 4       # ± percent
  pitch_jitter: 3      # ± Hz
```

### 13.5 Verification

- `python -m compileall -q main.py core skills interfaces tests` -> no errors.
- `python -m unittest discover -s tests` -> **Ran 37 tests ... OK** (new tests
  cover `percent()`, the prompt text, config-driven prompt changes, the absence
  of template leftovers, `just_thanks()` and `prosody()`).
- Prompt smoke: 3,458 characters, reads naturally ("About 60% of your replies
  carry a jab like that, and about 70% carry a joke... Ask a real follow-up
  question in about 70% of replies...") and includes remembered facts.
- Prosody smoke: "On it." -> `+12% +0Hz`, "That's hilarious." -> `+4% -1Hz`,
  "I might be wrong here." -> `+9% -1Hz`, "Hm." -> `+12% -3Hz`; identical
  input always gives identical output.
- Live conversation smoke: asked about an all-night coding session, she replied
  with a jab, a follow-up question and one practical question; asked how she
  was, she answered, asked one about him, and remembered his Python lesson.
- Console safety: `main.py` now reconfigures stdout/stderr with
  `errors="replace"`, so a provider returning a thin space or curly quote can
  no longer abort a valid model attempt with `UnicodeEncodeError` on a cp1252
  console.
- Startup safety: `python main.py --help` from the bare system interpreter
  re-launches into the project venv and prints the CLI help instead of dying on
  `ModuleNotFoundError: openai`. `IRIS_REEXEC=1` prevents the hand-off, so a
  misconfiguration cannot cause a restart loop.

### 13.6 Limits, honestly

- **Personality is prompt-shaped.** A weaker free model can still slide back
  into helpdesk voice; the knobs make it likely, not guaranteed, and a better
  model follows them more closely.
- **Teasing is bounded by instruction, not by a filter.** She is told never to
  touch body, family, money or real sore spots, and to drop the act when he is
  low - but she is a language model, so the config cannot guarantee taste.
- **Idle banter is infrequent by design.** One line after 25 quiet minutes,
  then at most one every 45 minutes; it never fires while she is listening,
  speaking or mirroring a live surface. Set `persona.proactive: false` to turn
  it off entirely.
- **Prosody is deliberately small.** ±4% rate and ±3 Hz are enough to stop the
  flat monotone without making the cached voice sound unstable; pushing the
  jitter higher starts to sound drunk rather than human.
- **Temperature 0.7 costs a little predictability.** Occasionally a free model
  will now say something odder than the old 0.5 setting; that is the trade for
  sounding alive.

---

## 14. Doctor - one command to check the whole laptop

`python main.py --doctor` prints a plain-text health report and saves the same
text to `logs/doctor.txt`. It is read-only: no settings are changed, nothing is
downloaded and no admin rights are used. `--doctor` is dispatched before the
single-instance lock and before the third-party imports, so it still runs when
`config.yaml` or a package is broken. `python scripts/doctor.py` also works on
its own.

Every reported line starts with `OK`, `WARN` or `FAIL` and carries a one-line
`fix:` hint; the final line is a status-coded summary with the totals.

### 14.1 What it checks

- **Python and environment:** interpreter version (3.11+), active venv, and
  every pinned package in `requirements.txt` compared with the installed
  version (one line each).
- **Config:** `config.yaml` parses as a mapping, and every leaf key in
  `core.config.DEFAULTS` is present; missing keys are named as dotted paths.
- **Microphone:** input devices and the default device, plus a two-second
  `16 kHz` mono RMS test reporting min/avg/max, the noise floor, the peak-to-
  floor ratio and whether speech is clearly separable from noise.
- **Speaker:** one short 880 Hz beep.
- **Network:** DNS, TCP connect and TLS handshake times for Groq, OpenRouter,
  the Edge TTS host and both DuckDuckGo hosts, plus the size/time/speed of a
  small public file download.
- **Keys and models:** presence only (`yes`/`no`) for every provider key, then
  one `max_tokens=1` call per configured free provider. Key values, response
  bodies and raw exception messages are never printed.
- **Local pieces:** Vosk model files, Playwright package, installed Chrome or
  bundled Chromium, free RAM, free disk, and the last 20 real ERROR/WARNING
  log-level lines from `logs/iris.log` (an INFO message containing the word
  "ERROR" is not counted).

### 14.2 Staying under 30 seconds, even offline

New `scripts/doctor.py` uses one 22-second wall-clock budget, per-probe
timeouts and daemon worker threads. DNS, TCP, TLS, the mic scan, the two-second
level test, the beep, the download and each provider call are all bounded; a
stuck probe becomes a `WARN`/`FAIL` line reading `timeout` instead of blocking.
When the budget runs low, the remaining heavy groups become explicit
`skipped: time budget exhausted` lines rather than being attempted. Failures
are categorised only by type (for example `dns failed`, `rate limited (429)`,
`key rejected (401)`), never by raw error text, and the whole report is forced
to ASCII for cp1252 consoles.

### 14.3 Verification

- `python -m compileall -q main.py core skills interfaces scripts tests` -> no
  errors.
- `python -m unittest discover -s tests` -> **Ran 49 tests ... OK**. New
  `tests/test_doctor.py` covers report formatting (every line has a status and
  a fix hint), summary status, credential redaction, missing config keys,
  requirements version comparison, log-level filtering, probe values/errors,
  timeout escape hatches, the shared deadline and offline network failures.
- Online run: 9.6 s total, 67 lines, all hosts reachable, 1-token calls to
  Groq and OpenRouter succeeded, Vosk/Playwright/Chrome present, mic device
  list correct; the two-second test correctly reported `separable no` while
  the room was quiet and suggested saying "Iris Iris".
- Offline run (DNS forced to fail through a test-only `sitecustomize.py`):
  5.5 s total, exit code 0, no traceback, six network `FAIL` lines with fix
  hints, local checks still OK, and `logs/doctor.txt` rewritten successfully.

### 14.4 Limits

- The mic test cannot tell whether *you* spoke; it measures separation during
  two seconds and asks you to say the wake word if the room was quiet.
- DNS resolution inside the OS resolver cannot be cancelled mid-call; the
  probe stops waiting after its timeout and the daemon thread dies with the
  process. It cannot block the report or the mic loop.
- The 1-token calls consume a tiny amount of the free-tier quota. They are
  skipped entirely when no key is configured.

---

## 15. Bad internet - fallbacks instead of freezes

Iris now treats a bad connection as a normal condition rather than an error.
DNS has a free safety net, dead providers are skipped quickly, the interface
knows when the laptop is offline, and local skills keep working.

### 15.1 DNS fallback

New `core/dns.py` wraps `socket.getaddrinfo` once at startup. The normal system
lookup runs first, bounded by a short daemon-thread wait. If it fails, the name
is resolved through Cloudflare's free DNS-over-HTTPS JSON endpoint at
`https://1.1.1.1/dns-query`, using the IP literal and the
`cloudflare-dns.com` TLS name so the fallback never needs DNS itself.

- Positive answers are cached for five minutes; failures for 30 seconds, so a
  dead network does not trigger the same lookup on every tool call.
- Numeric hosts bypass DNS entirely, which also keeps the DoH path and the
  single-instance lock usable when the resolver is broken.
- The patch is on `socket` itself, so it covers the Groq/OpenAI SDKs (httpx),
  `requests`/urllib3 and `aiohttp`/edge-tts without changing those libraries.
- `main.py` installs it after `--doctor` is dispatched but before the
  third-party imports, so the doctor's offline simulation still works.

### 15.2 Provider circuit breaker

`core/brain.py` keeps a breaker per provider. Two failures inside 90 seconds
open that provider's circuit for 60 seconds; each later trip doubles the wait,
up to 15 minutes. `chain()` filters open providers before any client is
created, so a dead endpoint costs almost nothing while the other free models
still answer. A successful reply clears the provider's history. Rate-limit
waits happen at most once per `ask_stream` call, so Iris says "One moment."
once instead of repeating it.

### 15.3 Connectivity monitor

New `core/connectivity.py` makes short TCP probes against a few public hosts on
a daemon thread. It publishes `connectivity` only when the state actually
changes, so the interface and the spoken notice cannot spam. The UI shows
`offline - local mode` while offline; `main.py` speaks one calm notice on the
first drop and re-arms it after the connection returns.

### 15.4 Offline mode

When offline, `core/agent.py` first tries a small deterministic local router in
`core/offline.py` for commands such as time, battery, volume, screenshots,
opening local apps, notes, file reads and window lists. Online-only tools are
hidden from the model. If `models.ollama_models` is configured, the chain uses
only that local Ollama model; otherwise Iris says plainly that local things
still work. Speech falls back to the built-in Windows SAPI voice, and Edge TTS
network calls are bounded by `voice.tts_timeout`.

### 15.5 Verification

- `python -m compileall -q main.py core skills interfaces scripts tests` -> no
  errors.
- `python -m unittest discover -s tests` -> **Ran 63 tests ... OK**. New
  `tests/test_resilience.py` simulates DNS failure, DoH success and negative
  caching, system-resolver timeouts, `requests`/`httpx`/`aiohttp` through the
  patched resolver, HTTP 429, timeouts and recovery. It checks that a dead
  provider is skipped without another call, that "One moment." is said once,
  that connectivity publishes once per change, and that the monitor is a
  daemon and never blocks the UI.
- Offline doctor run (temporary `sitecustomize.py` forcing DNS failure):
  7.0 s total, exit code 0, no traceback, six network `FAIL` lines with fix
  hints and local checks still OK.

### 15.6 Limits

- DoH still needs a working route to `1.1.1.1:443`; if the network blocks that
  IP too, Iris treats the lookup as failed and uses the negative cache.
- The OS resolver call cannot be cancelled mid-call; the wrapper stops waiting
  after its timeout and the daemon thread dies with the process.
- The SAPI voice is lower quality than Edge TTS. It is a fallback, not a
  replacement.
- Without a configured Ollama model, offline replies are limited to the local
  command router.

### 15.7 Try it by hand in under a minute

Turn Wi-Fi off and say "Iris, volume up" - it should still work after one calm
offline notice. Ask "Iris, search the web for cats" - she should say she cannot
reach the web instead of freezing. Turn Wi-Fi back on; the status returns to
online and the next web request works.

## 16. Speech without gaps - merging, prefetch and a local voice

The old voice engine synthesised one sentence at a time, so every sentence paid
the full Edge TTS round trip and Iris audibly paused between them. `core/voice.py`
now runs a small `SpeechPipeline` with two daemon threads: one synthesises, one
plays. The public `speak()`, `wait_done()`, `is_speaking()`, `stop()` and
`prewarm()` functions are unchanged, so `main.py`, the agent and the skills did
not need to change.

### 16.1 Merging and prefetch

- `speak()` splits a reply at sentence boundaries and queues at most two chunks:
  the first sentence on its own, then the rest joined. That keeps the first
  sound fast while giving the synthesiser a large second request.
- Anything that arrives while the synthesiser is busy is merged. `_TextQueue`
  hands out the queued chunks of the same generation as one request, so three
  quick sentences become one Edge call instead of three.
- The very first chunk of an idle utterance is marked `solo` and is never merged
  with later text. It is synthesised immediately, which is the fast path for a
  short opener such as "Hi."
- While the player is busy, the synthesiser is already working on the next
  chunk, so the next clip is normally ready before the current one ends.

### 16.2 Measuring the gap

`GapMeter` records the time between the end of one chunk and the start of the
next within the same generation. The player logs `tts gap N ms` for every
consecutive pair and warns when a gap exceeds 300 ms. The meter takes an
injectable clock, so the tests can verify it without sleeping. `stop()` resets
it, and a new utterance resets it as well, so a gap is never reported across
two separate replies.

### 16.3 Edge budget and the Windows voice

- `voice.tts_timeout` now defaults to **3.0 s** (was 8.0 s). The Edge call is
  wrapped in `asyncio.wait_for`, so a stalled request cannot block speech.
- On a failure or timeout, the synthesiser calls `_edge_failed()`, which marks
  Edge as down, shows **one** toast, and sends the text to SAPI through
  `win32com.client` (`SAPI.SpVoice`). pywin32 is already a dependency, so this
  is free and built into Windows.
- SAPI is used asynchronously so `stop()` can purge it. If SAPI itself is
  unavailable, the text is skipped with a log line rather than crashing Iris.
- `voice.edge_retry` (default 30 s) controls how long Iris stays on SAPI. After
  that window the next chunk tries Edge again. A retry failure does **not**
  re-toast; only a real Edge success or the connectivity monitor reporting
  online clears the fallback state and re-arms the toast.
- `should_use_offline_voice()` also returns true whenever the connectivity
  monitor says Iris is offline, so the same path covers a pulled cable and a
  slow Edge server. `prewarm()` skips network synthesis in that state.

### 16.4 Barge-in and accounting

The old code used `queue.join()`, which made `stop()` wait for work it had just
cancelled. Each utterance now has a generation number and a per-generation
pending count:

- `stop()` increments the generation, drains the text and audio queues, marks
  their pending items done, removes temporary mp3 files, resets the gap meter,
  then stops pygame and purges SAPI.
- `wait_done()` waits only for the current generation, so a cancelled utterance
  cannot keep a caller blocked.
- `is_speaking()` reads the same counter, so the interface, wake word and
  barge-in checks stay correct after a stop.

### 16.5 Verification

- `python -m compileall -q main.py core skills interfaces scripts tests` -> no
  errors.
- `python -m unittest tests.test_voice -v` -> **Ran 9 tests ... OK**. The fake
  TTS tests check that queued sentences merge into one request, that the first
  short sentence stays on its own fast path, that the simulated gap stays under
  300 ms, that the second chunk is synthesised before the first finishes
  playing, that `GapMeter` honours an injected clock, that `stop()` returns in
  under 50 ms with zero pending work and announces idle, that a simulated Edge
  failure speaks through the offline voice, that the Edge budget is at most 3 s,
  that repeated failures toast once, and that connectivity recovery clears the
  fallback.
- `python -m unittest discover -s tests` -> **Ran 72 tests ... OK**.
- Offline doctor run (temporary `sitecustomize.py` forcing DNS failure):
  6.8 s total, exit code 0, no traceback, six network `FAIL` lines with fix
  hints and local checks still OK.

### 16.6 Limits

- The first sentence still needs one network round trip unless the phrase is
  already in the disk cache or Edge is down. Prefetch only helps from the second
  chunk onward.
- SAPI uses whatever Windows voice is installed. It is intelligible, not
  beautiful, and its timing is less precise than Edge.
- Merging joins sentences with a space, so per-sentence prosody is chosen for
  the merged text rather than for each sentence.
- If a machine has no audio device at all, playback is skipped and logged; the
  rest of Iris keeps running.

### 16.7 Try it by hand in under a minute

Ask something that makes Iris answer in several sentences. The first sentence
should start quickly, the rest should follow without a long pause, and
`logs/iris.log` should show `tts gap` lines below 300 ms. Then disconnect the
network and ask again: she should say one toast about the offline voice and
still speak. Reconnect and wait half a minute; the next reply should use Edge
again.


## 17. Real Chrome, a matching offline voice, stronger free models, initiative and a floating avatar

### 17.1 Offline voice now matches the online Iris

The Edge voice is `en-GB-SoniaNeural`. The Windows SAPI fallback used to keep
whatever voice happened to be first, which on this laptop is David (male). The
offline path now picks a voice deliberately:

- `core/voice.py` adds `OFFLINE_VOICE_NAME` (default `Hazel`),
  `_pick_sapi_voice()` and `_sapi_ssml()`.
- Selection scores the installed voices: the named voice first (Microsoft Hazel
  Desktop, en-GB female), then any British female, then any female, then
  anything else. A male voice is never chosen while a female exists.
- Speech is wrapped in SSML with the same `rate` and `pitch` values Edge uses,
  and falls back to plain text if a voice rejects XML.
- `voice.offline_voice: Hazel` is exposed in `config.yaml` and in
  `core/config.py` defaults.

### 17.2 Stronger free models, ranked instead of guessed

- The primary free model is now `qwen/qwen3.8-27b` on Groq, with
  `openai/gpt-oss-120b` as the fallback and `openai/gpt-oss-20b` as an extra
  free fallback. All three were checked against Groq's free API with tool calls.
- `core/brain.py` adds `_model_rank()`. OpenRouter `:free` models are filtered
  to tool-capable IDs with a usable context window and then ranked by model
  size, family, recency-friendly hints and context length instead of a short
  hard-coded preference list. `openrouter_max` is now 6.
- No paid model can enter the chain: OpenRouter IDs must still end in `:free`.

### 17.3 Real Chrome, controlled over DevTools

`skills/browser_agent.py` was rewritten. There is no bundled browser and no
headless page hidden from the user any more.

- Iris launches the installed Google Chrome (or attaches to a Chrome already
  listening on a loopback DevTools port) and drives the same visible window the
  user can see and touch.
- The default profile is Iris' own `data/chrome-profile`, because a Chrome that
  is already running with the system profile cannot enable the debugging port.
  `chrome.user_data_dir: system` is supported; if Chrome is already open Iris
  falls back to her own profile and shows one toast. `chrome.close_existing` is
  off by default, so Iris never kills a running Chrome unasked.
- Existing `browser_*` tool names are kept. New controls: tabs (list, new,
  switch, close), find by text, list links, hover, wait for text or a selector,
  JavaScript evaluation (level 2), file upload (level 2), viewport changes,
  keyboard shortcuts, local file pages, and an optional snapshot into the UI.
- `browser.show_in_ui` now defaults to false, so no browser panel is drawn.
  `browser.external_chrome` is true.
- `skills/basic.py` routes `open_chrome`, `google_search` and `open_website`
  through the real Chrome controller.

### 17.4 Apps open as normal windows again

- `skills/laptop.py` `open_app` and `open_anything` now launch ordinary,
  separate Windows windows. Websites in `open_anything` go to real Chrome.
- `surface.embed_apps` defaults to false. `show_window` still mirrors a real
  window into the optional surface panel when the user asks for it.

### 17.5 Iris can decide for herself

- New `core/initiative.py`: a small, rate-limited second loop. After a long
  silence the free model is asked for one JSON decision:
  `{"say": "...", "do": null}` or a safe tool call.
- `should_consider()` is pure and enforces `autonomy.enabled`, idle time,
  cooldown, an hourly cap and a busy flag. `parse_decision()` accepts clean
  JSON, fenced JSON or a short plain sentence. `safe_tool()` is an allow-list of
  ordinary, reversible actions; risky tools, JavaScript, uploads, typing,
  clicks and shell commands are not available to the autonomous path.
- `Initiative` also has a `check_minutes` gap, so a model that says "nothing"
  is not called every half second. The old gentle idle line remains as a
  fallback when the model is offline or silent.
- `main.py` wires the initiative into the idle branch of the assistant loop. It
  never speaks over listening, speaking or an active turn. Risky actions chosen
  by the model are refused with a toast; ordinary tools run through the normal
  registry and confirmation path.
- `core/persona.py` now describes the visible Chrome window, normal app
  windows, the floating avatar, and her freedom to decide and chain safe steps.

### 17.6 The floating avatar

- New `interfaces/avatar.py`: a frameless, always-on-top Toplevel with a dark
  red orb, glow rings, rotating arcs, blinking eyes, a speaking equaliser and a
  short caption of the last line.
- Drag to move; the position is saved to `data/avatar_pos.json`. Click to bring
  the main window back, double-click to start listening, right-click for Show
  Iris, Listen, Stop speaking and Hide avatar. The main title bar also has an
  avatar toggle, so hiding it is reversible.
- `interfaces/ui.py` creates it when `ui.avatar` is true (default), forwards
  state, microphone level, `say` and `reply` events, and closes it with the main
  window. `ui.avatar_size` sets its size.

### 17.7 Verification

- `python -m compileall -q main.py core skills interfaces scripts tests` -> no
  errors.
- `python -m unittest discover -s tests` -> **Ran 99 tests ... OK**. New tests
  cover SAPI voice selection and SSML, model ranking, Chrome profile/endpoint
  helpers, initiative rate limits, decision parsing and the safe-tool allow
  list.
- Real Chrome smoke test: launched Chrome 157 with a temporary profile and a
  loopback DevTools port, connected with Playwright `connect_over_cdp`, loaded
  a page, read its title and text, then disconnected and closed only that
  Chrome instance. It worked.
- Offline doctor smoke test (resolver forced to fail): 6.7 s, exit code 0, six
  network `FAIL` lines with fix hints, no traceback, local checks still OK.

### 17.8 Limits

- Chrome must have been started with `--remote-debugging-port`. If the user's
  normal Chrome is already open with the system profile, Iris uses her own
  profile instead of killing it; profile cookies and logins therefore differ.
- SAPI quality depends on the installed Windows voices. Hazel is present on
  this laptop; another machine falls back to the best British/female voice it
  has, then to whatever SAPI offers.
- Autonomy is intentionally conservative: safe reads, navigation, opening
  things and short spoken lines only. Anything destructive, private or costly
  still goes through the normal confirmation path.
- The avatar is plain tkinter, not a 3D character. It is light enough for an
  8 GB laptop and disappears with the main window.

## 18. Talking over her: loudness barge-in

Iris can now be interrupted simply by speaking over her, in addition to Esc and
the wake phrase.

- `core/ears.py` adds `BargeIn`, a pure state machine. While `voice` is actually
  playing it learns the echo baseline from the first ~1 s of each playback burst
  (median RMS of the mic frames, so a word or two from the user does not move
  it), then fires when the user is above `max(min_rms, baseline * ratio)` for
  `hold_s` (~300 ms). It fires once per burst and relearns on the next one.
- `KeepBuffer` holds the last ~0.6 s of microphone audio. On a trigger the
  frames are moved into the next listen capture, so the first word the user
  said is not lost. `Ears._seed_preroll()` restores them after the mode change and
  the seeded frames count towards the "real speech" threshold.
- On a trigger: `voice.stop()` cuts the audio, `agent.cancel` cancels the turn,
  `state` becomes `listening`, and the interface shows a short "Go ahead." cue.
- `voice.barge_in_mode` is `voice` (default), `wake` (wake phrase only) or `off`
  (Esc only). The legacy `voice.barge_in` flag still maps to `off` when no mode
  is set. New knobs: `barge_in_ratio`, `barge_in_min_rms`, `barge_in_learn`,
  `barge_in_hold`, `barge_in_keep`, `barge_in_debug`.
- `barge_in_debug: true` prints the learned baseline and threshold, both once
  per burst and on a trigger.
- `core/ears.py` now creates the microphone singleton lazily, so the detector
  can be imported and tested without opening the mic.
- `tests/test_bargein.py`: 14 tests replaying synthetic frames - steady echo
  with jitter never triggers, a single spike does not trigger, a user talking
  over her triggers within 0.4 s, a quiet room uses the absolute floor, modes
  map correctly, and the kept audio is seeded into the next capture.

## 19. Online voice no longer drops out on a slow line

Iris was speaking in the offline SAPI voice while the internet was fine. The
cause was the Edge TTS budget: `tts_timeout` was 3.0 s, but this line needs about
2.2-2.7 s just for the first audio and 3.3 s for a short sentence, so almost
every request timed out. A timeout then set a 30 s "Edge is down" window and
showed the "Voice switched to the offline voice" toast, so one slow sentence
kept her on Hazel.

- `tts_timeout` default is now 6.0 s (config and `core/config.py`), which leaves
  headroom for a slow line without letting the fallback hang.
- Timeouts and connection errors are now treated as transient: Edge is skipped
  for `tts_soft_retry` (12 s) so the rest of the reply keeps one voice, with no
  toast. Real service refusals still use `edge_retry` (30 s) and toast once.
- The warning now logs the exception type instead of an empty message, so a
  timeout is readable in `logs/iris.log`.
- Startup prewarm waits while a real reply is being synthesised or played. A
  second concurrent Edge stream roughly doubles the latency, and the greeting
  matters more than warming the cache.
- `tests/test_voice.py` checks the bounded budget, that a timeout neither
  benches Edge for 30 s nor toasts, and that a service refusal still toasts once
  and recovers when the network returns.

## 20. Persona eval: measure how human she sounds

Tuning the persona by vibes is guesswork. `scripts/persona_eval.py` runs about
thirty scripted turns through the real `core/persona.py` prompt and the real
free-model chain, then scores every reply on rules a machine can check. It is
read-only: it never edits `config.yaml` or `core/persona.py`.

- 31 cases across small talk, a bad day, jokes, errors, commands, 3 a.m. coding
  and disagreement. Ten are marked `upset`, so the softer-tone rule only applies
  where it should.
- Rules: at most three sentences; no markdown, lists, headings, code, links or
  tables; no helpdesk phrases (a built-in list plus `--helpdesk-file`); no
  banned opener ("Certainly", "Of course", "I'd be happy to"); the user's name
  at most once and never Iris in the third person; at most one question mark per
  reply; an opener repeated for the third time is flagged; and on an upset turn,
  a warm acknowledgement with no teasing.
- The scorecard shows per-rule pass/fail, the overall rate, the observed
  question rate against `persona.curiosity` (a +/-25 point band), repeated
  openers, and the three worst replies. Each worst reply names the failed
  checks and the `build_prompt()` sentences likely responsible.
- `--demo` scores canned replies with no network or model calls, so the
  scorecard works offline. The live runner uses `brain.ask_stream` with
  `tools=None`, one turn at a time, a configurable gap, and a rate-limit
  backoff before retrying a turn that got no reply. It only ever uses
  `brain.chain()`, which already refuses paid IDs and providers without a key.
- `--json PATH` writes the same result for comparing runs over time.
- `tests/test_persona_eval.py`: 23 tests on canned replies covering every
  scorer, the upset-tone pass/fail cases, repeated openers, prompt blame, the
  demo scorecard, JSON output and case-set coverage.

## 21. Skills she writes herself, a warmer voice, a human avatar

Iris can now save a multi-step job as a reusable skill, sounds less robotic,
and appears as a small animated person instead of a glowing orb. Everything is
still free: Edge TTS, the existing free models and plain tkinter. No new
service, download or paid tier was added.

### Skills she writes herself

- `core/recipe.py` stores a skill as JSON data, not Python. A recipe lists
  registered tools and arguments, with `{name}` placeholders for inputs.
- The new `create_skill` tool is level 2, so the user is asked before anything
  is saved. Saved recipes live in `data/skills/` and are registered as normal
  tools at startup.
- Safety is unchanged at the step level: every step goes through
  `core.skill.run_tool`, so a level-2 step still asks every time it runs.
  Recipes cannot call level-3 tools, skill management tools or another recipe,
  and a step can only use arguments that tool really accepts.
- `list_skills` shows what she has learned; `forget_skill` (level 2) deletes a
  saved recipe. A corrupt or invalid file is skipped with a log line instead of
  breaking startup.
- `skills.enabled`, `skills.folder`, `skills.max_steps` and `skills.max_recipes`
  are new settings with defaults in `core/config.py` and comments in
  `config.yaml`.
- The persona prompt now tells her when to save a skill and when not to.

### A warmer, more human voice

- The default Edge voice is now `en-US-AvaMultilingualNeural`, the free
  "Expressive, Caring, Pleasant, Friendly" Copilot voice. The old British
  `Sonia` voice stays available by changing `voice.tts_voice`.
- `core/text.py` adds punctuation-aware prosody: questions lift, exclamations
  brighten, trailing thoughts fall, long sentences slow down and very short
  ones keep their bounce. The per-phrase jitter is still deterministic, so the
  TTS cache keeps working.
- The offline SAPI fallback now prefers the matching American female voice
  (`Zira`) and uses `en-US` SSML, instead of the old British `Hazel`/`en-GB`.
- `tests/test_core.py` covers the contour; `tests/test_voice.py` covers the
  new offline voice preference and SSML language.

### A human avatar

- `interfaces/avatar.py` now draws a stylised woman on the same transparent,
  always-on-top Toplevel: hair, face, blinking eyes with a moving gaze, brows
  that react, a mouth that moves with speech, breathing shoulders and a soft
  halo. It keeps the existing API (`set_state`, `set_level`, `say`, `hide`,
  `show`, `visible`, `close`) and the same drag/click/double-click/right-click
  behaviour.
- States are still `idle`, `listening`, `thinking` and `speaking`. Thinking
  adds floating dots, listening widens the eyes and raises the brows, speaking
  opens the mouth and nods slightly.
- Motion is pure, deterministic and cheap: about 40 canvas items at the same
  40 ms tick, with no images or extra downloads. The pure helpers are tested in
  `tests/test_avatar.py` without opening a window.

### A free model chain that survives a limit

- OpenRouter's free tier has a 50-request-per-day cap per account, not per
  model, so adding more OpenRouter models cannot help once it is hit. When that
  error arrives the whole provider is now parked until the reset time in the
  `X-RateLimit-Reset` header, and the other free providers keep answering.
- Groq's free limits are per model. Every free Groq chat model is discovered
  from the models endpoint (cached for an hour) and tried in turn, so one
  model's tokens-per-minute running out no longer disables the rest. A static
  `models.groq_models` list can pin an exact order.
- A rate-limited model is cooled on its own (`cool_model`) instead of tripping
  the provider breaker. The breaker is only used for real outages and
  account-wide limits.
- The full tool list is about 9k tokens, which is larger than Groq's per-minute
  input budget, so a Groq request used to fail with HTTP 413 and no reply.
  `core.skill.select_tools` now keeps a small core set plus the tools that match
  the user's words inside `brain.groq_tool_budget` (4000 rough tokens). A 413
  still shrinks the list once and retries the same model before moving on.
- New settings with defaults in `core/config.py` and comments in `config.yaml`:
  `models.groq_free`, `models.groq_models`, `models.groq_max`,
  `models.openrouter_max` (now 10) and `brain.groq_tool_budget`.
- `tests/test_resilience.py` covers the daily cap, a per-model limit, the cooled
  model being skipped and the 413 shrink-and-retry; `tests/test_core.py` covers
  the tool budget selection.

## 22. The voice stopped dropping out, and she speaks like a person

### The real cause (measured, not guessed)

`scripts/voice_diag.py` synthesises five sentences with the configured voice and
prints the exact exception, the time to first audio, the prosody strings, the
voice-list check and the DNS/TCP timings. On this laptop it showed:

- the voice name was present, DNS took about 0.06 s, TCP connect about 0.6 s,
  and every rate/pitch string was valid;
- Edge itself was variable: warm sentences produced audio in 0.7-2.5 s, but a
  cold or busy one needed 5-7.7 s;
- the old code wrapped the whole `save()` in one 6 s `asyncio.wait_for`, so a
  normal cold sentence hit `TimeoutError` at exactly 6.01 s;
- one timeout immediately set the old `_EDGE_DOWN` flag, which sent the rest of
  the reply to the Windows voice for 12 s. That is the voice switch the user
  heard.

With `--timeout 15` all five sentences succeeded, confirming the service was
fine and the budget was not.

### Fallback that treats one hiccup as a hiccup

- `core.voice.EdgeHealth` is a pure state machine with an injectable clock. A
  failed sentence is recorded once (after its own retries); Edge is marked down
  only after `voice.edge_down_after` (3) failures inside
  `voice.edge_down_window` (60 s). One failure never switches the next sentence.
- Each sentence gets `voice.edge_retries` (2) extra attempts with 0.4 s/0.8 s
  backoff, inside `voice.edge_total_budget` (20 s). The websocket uses
  `edge_connect_timeout` (6 s) and a first-audio budget of 8 s cold / 5 s warm.
  Once audio is flowing the stream is allowed to finish, so a long merged reply
  is never cut off by the sentence budget.
- A failed sentence is spoken by the offline voice immediately, but the next
  sentence tries Edge again unless the state machine is really down.
- While down, a background thread probes a tiny "Okay." every
  `voice.edge_probe_interval` (20 s), never during a reply, and the first
  success clears the state. A connectivity recovery also brings the probe
  forward.
- Toasts fire only on the transition ("Voice switched to the offline voice." /
  "Voice back online."). Every attempt logs its exception type and message.
- New settings: `voice.engine` (`auto | edge | offline`), `edge_retries`,
  `edge_connect_timeout`, `edge_first_audio_cold`, `edge_first_audio_warm`,
  `edge_total_budget`, `edge_cold_after`, `edge_down_after`,
  `edge_down_window`, `edge_probe_interval`. The old `tts_timeout`, `edge_retry`
  and `tts_soft_retry` keys are gone.

### Spoken text, not written text

- `core.text.speakable()` is applied inside the speech pipeline, so every line
  is normalised while the UI keeps the original reply. It strips markdown,
  emoji/emoticons, URLs, code fences and long Windows/POSIX paths, expands
  abbreviations, and turns times, dates, percentages, currency, units and
  numbers into words ("9:30" -> "nine thirty", "20%" -> "twenty percent",
  "2026-11-18" -> "November eighteenth, twenty twenty-six").
- Ellipses become a short pause and a sentence longer than 25 words is split at
  its middle comma, repeatedly, so the voice can breathe.
- `voice.pronunciations` is a config map applied last (`SQL` -> `sequel`,
  `JSON` -> `jason`, and so on). `voice.speakable: false` turns the whole step
  off.

### A natural rhythm, with valid strings

- `core.text.prosody()` still returns the same deterministic variation for the
  same phrase (so the cache still hits), but now always emits `+N%` and `+NHz`.
  A garbage config value such as `+-1Hz` is parsed and clamped instead of being
  sent to Edge.
- Questions lift and slow slightly, exclamations brighten, trailing thoughts
  fall, long sentences slow down and very short reactions get a little quicker.
- `voice.style` is `calm`, `warm` (default) or `energetic`, each clamping rate
  and pitch to a sensible range; the base rate is now `+2%`, so the default
  result stays inside +0%..+5%.

### Audition the free voices

- `scripts/voice_audition.py` generates the same six lines (casual, joke,
  question, serious, long explanation, time and number) in all eight requested
  Edge voices, into `data/voice_audition/<voice>/`, then opens the folder.
  Missing voices are skipped, existing files are reused, `--force` redoes them.
  It does not choose for the user; Ava stays the default.
- `scripts/voice_diag.py` remains the first thing to run when the voice drops.

### Optional Kokoro offline voice (opt-in, never required)

- `core/kokoro.py` uses `kokoro-onnx` with `models/kokoro-v1.0.int8.onnx` and
  `models/voices-v1.0.bin`. It is imported lazily in the TTS worker thread and
  only when `voice.offline_engine: kokoro`.
- On first use it measures the real-time factor and refuses the model if it is
  slower than `voice.kokoro_max_rtf` (1.5x), logging why and using SAPI. Any
  missing package, missing file, bad sample rate or synthesis error also falls
  back to SAPI; nothing else breaks.
- `scripts/fetch_kokoro.py` downloads the two files once, resumably, into
  `models/`; `--check` reports what is present. Then install
  `kokoro-onnx` with pip. This is optional and needs no account or payment.
- Kokoro plays through pygame, so `stop()` still cuts it off within the 50 ms
  barge-in budget, and the Edge mp3 cache is unchanged.

### Tests

- `tests/test_voice.py` drives `EdgeHealth` with a fake clock (one failure does
  not switch, three do, old failures expire, recovery toasts once, probe
  timing), checks per-sentence fallback in the pipeline, the once-only toast,
  the forced engines, the Kokoro fallback and the spoken-text normalisation.
- `tests/test_core.py` covers `speakable()` (numbers, times, dates, units,
  currency, URLs, paths, markdown, emoji, abbreviations, ellipses, long
  sentences) and a property test over hundreds of random phrases asserting
  every generated rate/pitch matches `^[+-]\d+%$` and `^[+-]\d+Hz$`, including
  inside every style.

