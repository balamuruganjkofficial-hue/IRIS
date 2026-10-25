import speech_recognition as sr
r = sr.Recognizer()
with sr.Microphone() as s:
    r.adjust_for_ambient_noise(s, duration=1)
    print("energy threshold:", r.energy_threshold)
    print("Say something now...")
    a = r.listen(s, timeout=8, phrase_time_limit=6)
print("audio bytes captured:", len(a.frame_data))
try:
    print("heard:", r.recognize_google(a, language="en-US"))
except Exception as e:
    print("recognition error:", repr(e))
