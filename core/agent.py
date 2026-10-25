from core import brain
from skills import browser

# 🧠 MEMORY BANK: This stays alive as long as the terminal is running
memory = [
    {"role": "system", "content": """You are I.R.I.S., an autonomous AI. Address the user as Boss. 
You MUST respond in JSON. 
Format: {"say": "message"} OR {"tool": "tool_name", "args": {"arg1": "value"}}

Available tools:
- browser: Opens the default web browser to search Google. Args: {"query": "the search term"}"""}
]

def run(user_text, max_steps=5):
    # 1. Add your new command to her memory bank
    memory.append({"role": "user", "content": user_text})
    
    print(f"\nBoss: {user_text}")
    
    # 2. The Decide-Act-Observe loop
    for step in range(max_steps):
        action = brain.decide(memory)
        
        if "say" in action:
            print(f"I.R.I.S.: {action['say']}")
            # 3. Add her final spoken answer to the memory bank so she remembers what she said
            memory.append({"role": "assistant", "content": str(action)})
            return action["say"]
            
        if "tool" in action:
            tool_name = action["tool"]
            args = action.get("args", {})
            print(f"⚙️  [SYSTEM: I.R.I.S. is running tool -> {tool_name} with args {args}]")
            
            if tool_name == "browser":
                observation = browser.execute(args)
            else:
                observation = f"Error: Tool '{tool_name}' does not exist."
                
            print(f"👁️  [OBSERVATION: {observation}]")
            
            # 4. Add tool actions and system observations to her memory
            memory.append({"role": "assistant", "content": str(action)})
            memory.append({"role": "user", "content": f"Observation: {observation}. Now tell the user it is done."})
            continue

    return "I could not finish that in time."