import json, os, re, yaml
lp = os.path.join(os.environ["LOCALAPPDATA"], "Google", "Chrome", "User Data", "Local State")
info = json.load(open(lp, encoding="utf-8"))["profile"]["info_cache"]
profiles, seen = {}, set()
for folder, v in info.items():
    n = (v.get("name") or folder).split("@")[0].lower()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip() or folder.lower()
    if n in seen:
        n = n + " " + folder.lower().replace(" ", "")
    seen.add(n)
    profiles[n] = folder
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
cfg["chrome"] = {"default_profile": "", "profiles": profiles}
text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
open("config.yaml", "w", encoding="utf-8").write(text)
for k, v in profiles.items():
    print(k, "->", v)
