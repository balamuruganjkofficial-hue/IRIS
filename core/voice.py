import pyttsx3

def speak(text):
    print(f"I.R.I.S.: {text}")
    try:
        # BUGFIX: We initialize the engine inside the function.
        # This creates a fresh audio session every single time she speaks, preventing the loop lock-up.
        engine = pyttsx3.init('sapi5')
        
        voices = engine.getProperty('voices')
        if len(voices) > 1:
            engine.setProperty('voice', voices[1].id)
        elif len(voices) > 0:
            engine.setProperty('voice', voices[0].id)
            
        engine.setProperty('rate', 170)
        
        engine.say(text)
        engine.runAndWait()
        
    except Exception as e:
        print(f"❌ [SYSTEM ERROR: Audio playback failed -> {e}]")