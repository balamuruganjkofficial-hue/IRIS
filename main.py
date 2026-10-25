from core import agent
from core import listen

print("⚡ I.R.I.S. Neural Core Online.")

while True:
    print("\n" + "═"*50)
    # Give the user the choice: Type, or hit Enter to speak
    user_input = input("⌨️  Type your message (or press ENTER to use Microphone): ").strip()
    
    # If the user just pressed Enter, fire up the microphone
    if user_input == "":
        user_input = listen.get_audio()
        
    if not user_input:
        continue
        
    if user_input.lower() in ['shut down', 'exit', 'quit', 'stop']:
        print("I.R.I.S.: Shutting down systems. Goodbye, Boss.")
        break
        
    agent.run(user_input)