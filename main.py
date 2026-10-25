from core import agent

print("⚡ I.R.I.S. Core Online. Type 'exit' to shut down.")

while True:
    # Get user input
    user_input = input("\n> ")
    
    # Check for the kill word
    if user_input.lower() in ['exit', 'quit', 'stop']:
        print("I.R.I.S.: Shutting down systems. Goodbye, Boss.")
        break
        
    # Send the input to the agent loop
    agent.run(user_input)