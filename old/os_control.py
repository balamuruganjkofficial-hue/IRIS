from plyer import notification

def execute(command_string):
    """Executes local Windows OS commands."""
    if command_string.startswith("notify:"):
        message = command_string.replace("notify:", "").strip()
        try:
            notification.notify(
                title="I.R.I.S. System",
                message=message,
                app_name="I.R.I.S.",
                timeout=5
            )
            return f"Successfully triggered Windows notification with message: {message}"
        except Exception as e:
            return f"Failed to send notification: {e}"
            
    return "Unknown OS command."