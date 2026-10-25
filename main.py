from core import agent
from core import listen

print("⚡ I.R.I.S. Voice Core Online. Say 'shut down' to exit.")

while True:
    # We replaced input() with our new microphone module!
    user_input = listen.get_audio()
    
    # If the mic didn't catch anything, loop back and listen again
    if not user_input:
        continue
        
    if user_input.lower() in ['shut down', 'exit', 'quit', 'stop']:
        print("I.R.I.S.: Shutting down systems. Goodbye, Boss.")
        break
        
    agent.run(user_input)