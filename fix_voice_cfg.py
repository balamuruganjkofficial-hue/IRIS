import yaml
p = "config.yaml"
cfg = yaml.safe_load(open(p, encoding="utf-8"))
v = cfg.setdefault("voice", {})
v["wake_word"] = "hey_jarvis"
v["wake_threshold"] = 0.35
for k in ("wake_engine", "wake_phrases"):
    v.pop(k, None)
open(p, "w", encoding="utf-8").write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
print(open(p, encoding="utf-8").read())
