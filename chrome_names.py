import json, os, re
p = os.path.join(os.environ["LOCALAPPDATA"], "Google", "Chrome", "User Data", "Local State")
d = json.load(open(p, encoding="utf-8"))["profile"]["info_cache"]
seen = set()
for folder, v in d.items():
    n = (v.get("name") or folder).split("@")[0].lower()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip() or folder.lower()
    if n in seen:
        n = n + " " + folder.lower().replace(" ", "")
    seen.add(n)
    print(f'    {n}: "{folder}"')
