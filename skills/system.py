import os
import subprocess
import winreg

from core.skill import skill

_PAGES = {"bluetooth": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi",
          "display": "ms-settings:display", "sound": "ms-settings:sound",
          "night light": "ms-settings:nightlight", "battery": "ms-settings:batterysaver",
          "apps": "ms-settings:appsfeatures", "windows update": "ms-settings:windowsupdate",
          "microphone privacy": "ms-settings:privacy-microphone"}


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


@skill("Open a Windows settings page. Args: page (bluetooth, wifi, display, sound, "
       "night light, battery, apps, windows update, microphone privacy)")
def open_settings(page):
    uri = _PAGES.get(page.lower().strip())
    if not uri:
        return "ERROR: I know these pages: " + ", ".join(_PAGES)
    os.startfile(uri)
    return f"Opened {page} settings."


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