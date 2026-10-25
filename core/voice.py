import os
import asyncio
import edge_tts
import pygame

# Initialize the audio mixer invisibly
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"
pygame.mixer.init()

def play_audio(file_path):
    """Plays the audio file without blocking the rest of the system."""
    pygame.mixer.music.load(file_path)
    pygame.mixer.music.play()
    
    # Wait for the audio to finish playing before deleting the temp file
    while pygame.mixer.music.get_busy():
        pygame.time.Clock().tick(10)
        
    # Clean up the file so it doesn't clutter your folder
    try:
        pygame.mixer.music.unload()
        os.remove(file_path)
    except:
        pass

def speak(text):
    """Generates natural cloud speech and plays it."""
    print(f"I.R.I.S.: {text}")
    
    # Microsoft's free Neural Voice (Aria)
    voice_model = "en-US-AriaNeural"
    output_file = "temp_voice.mp3"
    
    async def generate_speech():
        communicate = edge_tts.Communicate(text, voice_model)
        await communicate.save(output_file)
        
    # Generate the audio file, then play it
    try:
        asyncio.run(generate_speech())
        play_audio(output_file)
    except Exception as e:
        print(f"❌ [SYSTEM: Audio generation failed -> {e}]")