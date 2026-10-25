from core import brain
from skills import browser

def run(user_text, max_steps=5):
    # We upgraded her memory to include an "Available tools" manual
    messages = [
        {"role": "system", "content": """You are I.R.I.S., an autonomous AI. Address the user as Boss. 
You MUST respond in JSON. 
Format: {"say": "message"} OR {"tool": "tool_name", "args": {"arg1": "value"}}

Available tools:
- browser: Opens the default web browser to search Google. Args: {"query": "the search term"}"""},
        {"role": "user", "content": user_text}
    ]

    print(f"\nBoss: {user_text}")
    
    # The Decide-Act-Observe loop
    for step in range(max_steps):
        action = brain.decide(messages)
        
        if "say" in action:
            print(f"I.R.I.S.: {action['say']}")
            return action["say"]
            
        if "tool" in action:
            tool_name = action["tool"]
            args = action.get("args", {})
            print(f"⚙️  [SYSTEM: I.R.I.S. is running tool -> {tool_name} with args {args}]")
            
            # The execution junction: routing her request to the real Python scripts
            if tool_name == "browser":
                observation = browser.execute(args)
            else:
                observation = f"Error: Tool '{tool_name}' does not exist."
                
            print(f"👁️  [OBSERVATION: {observation}]")
            
            messages.append({"role": "assistant", "content": str(action)})
            messages.append({"role": "user", "content": f"Observation: {observation}. Now tell the user it is done."})
            continue

    return "I could not finish that in time."