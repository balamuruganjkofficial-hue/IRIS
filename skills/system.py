import os
import re
import subprocess
import winreg

from core.skill import skill

# Every common Windows settings page, addressed by its official deep link.
# These open with normal user rights; no administrator prompt is involved.
_PAGES = {
    # system
    "display": "ms-settings:display",
    "advanced display": "ms-settings:display-advanced",
    "night light": "ms-settings:nightlight",
    "sound": "ms-settings:sound",
    "volume": "ms-settings:sound",
    "notifications": "ms-settings:notifications",
    "focus assist": "ms-settings:quiethours",
    "power": "ms-settings:powersleep",
    "sleep": "ms-settings:powersleep",
    "battery": "ms-settings:batterysaver",
    "battery usage": "ms-settings:batterysaver-usagedetails",
    "storage": "ms-settings:storagesense",
    "storage sense": "ms-settings:storagesense",
    "multitasking": "ms-settings:multitasking",
    "clipboard": "ms-settings:clipboard",
    "about": "ms-settings:about",
    "system info": "ms-settings:about",
    "system information": "ms-settings:about",
    "remote desktop": "ms-settings:remotedesktop",
    # network
    "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi",
    "wireless": "ms-settings:network-wifi",
    "ethernet": "ms-settings:network-ethernet",
    "vpn": "ms-settings:network-vpn",
    "airplane mode": "ms-settings:network-airplanemode",
    "aeroplane mode": "ms-settings:network-airplanemode",
    "mobile hotspot": "ms-settings:network-mobilehotspot",
    "hotspot": "ms-settings:network-mobilehotspot",
    "proxy": "ms-settings:network-proxy",
    "network": "ms-settings:network-status",
    "network status": "ms-settings:network-status",
    "bluetooth": "ms-settings:bluetooth",
    # apps and updates
    "apps": "ms-settings:appsfeatures",
    "installed apps": "ms-settings:appsfeatures",
    "default apps": "ms-settings:defaultapps",
    "startup apps": "ms-settings:startupapps",
    "startup": "ms-settings:startupapps",
    "optional features": "ms-settings:optionalfeatures",
    "windows update": "ms-settings:windowsupdate",
    "update history": "ms-settings:windowsupdate-history",
    "activation": "ms-settings:activation",
    "windows insider": "ms-settings:windowsinsider",
    "developer": "ms-settings:developers",
    "developers": "ms-settings:developers",
    # accounts
    "accounts": "ms-settings:yourinfo",
    "your info": "ms-settings:yourinfo",
    "email and accounts": "ms-settings:emailandaccounts",
    "email & accounts": "ms-settings:emailandaccounts",
    "sign in options": "ms-settings:signinoptions",
    "sign-in options": "ms-settings:signinoptions",
    "family": "ms-settings:otherusers",
    "other users": "ms-settings:otherusers",
    "work or school": "ms-settings:workplace",
    # time, language, input
    "date and time": "ms-settings:dateandtime",
    "date & time": "ms-settings:dateandtime",
    "time": "ms-settings:dateandtime",
    "language": "ms-settings:regionlanguage",
    "region": "ms-settings:regionformatting",
    "speech": "ms-settings:speech",
    "keyboard": "ms-settings:keyboard",
    "fonts": "ms-settings:fonts",
    "typing": "ms-settings:typing",
    # devices
    "mouse": "ms-settings:mousetouchpad",
    "touchpad": "ms-settings:devices-touchpad",
    "touch": "ms-settings:devices-touch",
    "pen": "ms-settings:pen",
    "pen and windows ink": "ms-settings:pen",
    "camera": "ms-settings:camera",
    "printers": "ms-settings:printers",
    "scanners": "ms-settings:printers",
    # personalisation
    "personalisation": "ms-settings:personalization",
    "personalization": "ms-settings:personalization",
    "background": "ms-settings:personalization-background",
    "colours": "ms-settings:personalization-colors",
    "colors": "ms-settings:personalization-colors",
    "lock screen": "ms-settings:lockscreen",
    "themes": "ms-settings:themes",
    "start": "ms-settings:personalization-start",
    "taskbar": "ms-settings:taskbar",
    # privacy and security
    "privacy": "ms-settings:privacy",
    "location": "ms-settings:privacy-location",
    "camera privacy": "ms-settings:privacy-webcam",
    "microphone privacy": "ms-settings:privacy-microphone",
    "background apps": "ms-settings:privacy-backgroundapps",
    "autoplay": "ms-settings:autoplay",
    "windows security": "ms-settings:windowsdefender",
    "windows defender": "ms-settings:windowsdefender",
    "firewall": "ms-settings:windowsdefender",
    "device encryption": "ms-settings:deviceencryption",
    "bitlocker": "ms-settings:bitlocker",
    "find my device": "ms-settings:findmydevice",
    # gaming and accessibility
    "game bar": "ms-settings:gaming-gamebar",
    "game mode": "ms-settings:gaming-gamemode",
    "captures": "ms-settings:gaming-gamedvr",
    "ease of access": "ms-settings:easeofaccess",
    "accessibility": "ms-settings:easeofaccess",
    "narrator": "ms-settings:easeofaccess-narrator",
    "magnifier": "ms-settings:easeofaccess-magnifier",
    "color filters": "ms-settings:easeofaccess-colorfilter",
    "high contrast": "ms-settings:easeofaccess-highcontrast",
    "closed captions": "ms-settings:easeofaccess-closedcaptioning",
    # maintenance
    "backup": "ms-settings:backup",
    "recovery": "ms-settings:recovery",
    "reset": "ms-settings:reset",
    "troubleshoot": "ms-settings:troubleshoot",
    "troubleshooting": "ms-settings:troubleshoot",
}

_ALIASES = {
    "wireless network": "wifi", "wi fi": "wifi", "internet": "network",
    "net": "network", "sound settings": "sound", "audio": "sound",
    "brightness": "display", "screen": "display", "monitor": "display",
    "battery saver": "battery", "power and sleep": "power",
    "update": "windows update", "updates": "windows update",
    "uninstall apps": "apps", "app list": "apps",
    "user accounts": "accounts", "my account": "accounts",
    "password": "sign in options", "pin": "sign in options",
    "region and language": "language", "input": "keyboard",
    "dark mode": "colors", "theme": "themes", "wallpaper": "background",
    "lock": "lock screen", "security": "windows security",
    "antivirus": "windows security", "defender": "windows security",
    "access": "ease of access", "text size": "ease of access",
    "mouse and touchpad": "mouse", "touch pad": "touchpad",
    "printer": "printers", "restart": "recovery", "factory reset": "reset",
    "date": "date and time", "clock": "date and time",
}


def _page_uri(page):
    """Resolve a spoken settings name to a ms-settings deep link."""
    key = re.sub(r"\s+", " ", (page or "").lower().strip())
    if not key:
        return None
    if key.startswith("ms-settings:"):
        return key
    key = _ALIASES.get(key, key)
    if key in _PAGES:
        return _PAGES[key]
    # loose containment either way ("wifi settings" -> "wifi")
    hits = [k for k in _PAGES if key in k or k in key]
    if len(hits) == 1:
        return _PAGES[hits[0]]
    words = [w for w in re.findall(r"[a-z]+", key) if w not in ("settings", "setting",
                                                                "the", "my", "page")]
    if not words:
        return None
    scored = sorted(_PAGES, key=lambda k: sum(1 for w in words if w in k),
                    reverse=True)
    best = scored[0]
    return _PAGES[best] if sum(1 for w in words if w in best) else None


def _bright_now():
    import screen_brightness_control as sbc
    cur = sbc.get_brightness()
    return int(cur[0] if isinstance(cur, (list, tuple)) else cur)


@skill("Set the screen brightness to an exact level. Args: level (0 to 100)")
def set_brightness(level):
    import screen_brightness_control as sbc
    try:
        v = max(0, min(int(float(level)), 100))
        sbc.set_brightness(v)
        return f"Brightness set to {v} percent."
    except Exception as e:
        return f"ERROR: could not change brightness: {e}"


@skill("Make the screen brighter or dimmer by a step. Args: direction (up or down), "
       "amount (optional, default 15)")
def change_brightness(direction, amount="15"):
    import screen_brightness_control as sbc
    try:
        step = int(float(amount))
        now = _bright_now()
        new = now + step if direction.lower().strip() == "up" else now - step
        new = max(5, min(new, 100))
        sbc.set_brightness(new)
        return f"Brightness went from {now} to {new} percent."
    except Exception as e:
        return f"ERROR: could not change brightness: {e}"


@skill("Switch Windows between dark and light mode. Args: mode (dark or light)")
def set_theme(mode):
    value = 0 if mode.lower().strip() == "dark" else 1
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                             0, winreg.KEY_SET_VALUE)
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        return f"Switched to {'dark' if value == 0 else 'light'} mode."
    except OSError as e:
        return f"ERROR: could not change the theme: {e}"


@skill("Open any Windows settings page by name, for example bluetooth, wifi, "
       "display, sound, night light, battery, storage, apps, windows update, "
       "privacy, microphone privacy, camera, taskbar, personalisation, date and "
       "time, language, mouse, touchpad, game bar, accessibility, recovery or "
       "windows security. Args: page")
def open_settings(page):
    uri = _page_uri(page)
    if not uri:
        return ("ERROR: I could not find that settings page. Try wifi, bluetooth, "
                "display, sound, battery, storage, apps, privacy, taskbar or "
                "windows update.")
    from skills import surface
    return surface.show_settings(uri)


@skill("Put the computer to sleep. No args.", level=2)
def sleep_pc():
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return "Going to sleep."


@skill("Shut down the computer in 30 seconds. No args.", level=2)
def shutdown_pc():
    subprocess.run(["shutdown", "/s", "/t", "30"], check=False)
    return "Shutting down in 30 seconds. Say cancel shutdown to stop it."


@skill("Restart the computer in 30 seconds. No args.", level=2)
def restart_pc():
    subprocess.run(["shutdown", "/r", "/t", "30"], check=False)
    return "Restarting in 30 seconds. Say cancel shutdown to stop it."


@skill("Cancel a pending shutdown or restart. No args.")
def cancel_shutdown():
    subprocess.run(["shutdown", "/a"], check=False)
    return "Cancelled."