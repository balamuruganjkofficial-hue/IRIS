"""Repair helper: make sure the wake word is Iris in config.yaml.

Safe to run any time; it only touches the voice section.
"""
import yaml

p = "config.yaml"
with open(p, encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh) or {}
v = cfg.setdefault("voice", {})
v["wake_word"] = "iris"
v["wake_engine"] = "auto"
v.setdefault("wake_phrases", ["iris", "hey iris", "hi iris", "hello iris",
                              "okay iris", "ok iris", "jarvis", "hey jarvis"])
v.setdefault("wake_fallback", "hey_jarvis")
v.setdefault("wake_threshold", 0.35)
with open(p, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
print("wake word:", v["wake_word"], "engine:", v["wake_engine"])
