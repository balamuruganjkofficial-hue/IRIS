import re
p = "config.yaml"
s = open(p, encoding="utf-8").read()
pal = {"bg": "#0C0509", "bg2": "#160710", "surface": "#220A14", "surface_hi": "#34101E",
       "accent": "#FF4D7D", "accent_soft": "#FF8FAB", "text": "#FFEFF3", "muted": "#B48A96",
       "user_bubble": "#5A1030", "iris_bubble": "#220A14"}
changed = 0
for key, val in pal.items():
    pat = r"(?m)^(\s*" + re.escape(key) + r":\s*[^#\n]*)#[0-9A-Fa-f]{6}"
    s, n = re.subn(pat, lambda m: m.group(1) + val, s)
    changed += n
open(p, "w", encoding="utf-8").write(s)
print("colour lines updated:", changed, "(expected 10)")
