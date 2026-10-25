import speech_recognition as sr
import time

def get_audio():
    """Listens to the microphone and converts spoken words to text."""
    r = sr.Recognizer()
    
    # SPEED HACK: Reduces the silence she waits for before processing (default is 0.8s)
    r.pause_threshold = 1.5
    
    # CRASH FIX: We wrapped the *entire* microphone connection in the try block
    try:
        with sr.Microphone() as source:
            print("\n🎙️ [Listening... Speak now]")
            
            # Calibrate faster
            r.adjust_for_ambient_noise(source, duration=0.2) 
            
            audio = r.listen(source, timeout=5, phrase_time_limit=15)
            
            # Added a visual cue so you know she isn't frozen, just thinking
            print("⏳ [Translating to cloud...]") 
            text = r.recognize_google(audio)
            print(f"🗣️ You said: {text}")
            return text
            
    except sr.WaitTimeoutError:
        return "" 
    except sr.UnknownValueError:
        print("❌ [SYSTEM: Could not understand audio.]")
        return ""
    except sr.RequestError:
        print("❌ [SYSTEM: Network error with Google.]")
        return ""
    except Exception as e:
        # This catches that nasty -9999 Windows host error and prevents a crash
        print(f"❌ [SYSTEM: Microphone glitch, resetting... -> {e}]")
        time.sleep(1) # Give the Windows audio driver a second to recover
        return ""