import re
import datetime
from core import brain
from core import voice
from skills import browser

def get_vitals():
    """Fetches real-time system context so she isn't blind."""
    now = datetime.datetime.now()
    return f"Time: {now.strftime('%I:%M %p')}. Date: {now.strftime('%A, %B %d, %Y')}. OS: Windows."

memory = []

def boot_sequence():
    """Initializes her personality and environmental awareness."""
    if not memory:
        memory.append({
            "role": "system", 
            "content": f"""You are I.R.I.S., a highly intelligent, slightly witty autonomous AI. 
Your Boss is Bala. You are running locally on his Windows machine. 
Do NOT act like a robotic language model. Speak naturally, confidently, and with personality.

SYSTEM VITALS: {get_vitals()}

TOOL PROTOCOL:
If you need to search the web, embed this exact tag anywhere in your response: <TOOL>browser|search query</TOOL>
Example: "I'll look that up for you right now, Boss. <TOOL>browser|Python automation</TOOL>"
Otherwise, just speak naturally."""
        })

def run(user_text, max_steps=3):
    boot_sequence()
    memory.append({"role": "user", "content": user_text})
    print(f"\nBoss: {user_text}")
    
    for step in range(max_steps):
        raw_response = brain.decide(memory)
        
        # Hunt for the hidden tool tags in her natural speech
        tool_match = re.search(r'<TOOL>(.*?)\|(.*?)</TOOL>', raw_response)
        
        if tool_match:
            tool_name = tool_match.group(1)
            args = tool_match.group(2)
            
            # Strip the tag from the text so she doesn't speak the raw code
            spoken_text = re.sub(r'<TOOL>.*?</TOOL>', '', raw_response).strip()
            
            if spoken_text:
                voice.speak(spoken_text)
                
            print(f"⚙️  [SYSTEM: Executing -> {tool_name} with arg: '{args}']")
            
            if tool_name == "browser":
                # We format the regex arg back into a dictionary for the browser script
                observation = browser.execute({"query": args})
            else:
                observation = f"Error: Tool {tool_name} not recognized."
                
            print(f"👁️  [OBSERVATION: {observation}]")
            
            # Feed the observation back into her memory so she can analyze it
            memory.append({"role": "assistant", "content": raw_response})
            memory.append({"role": "user", "content": f"System Observation: {observation}. Analyze this and reply briefly."})
            continue
            
        else:
            # Pure conversation, no tools required
            voice.speak(raw_response)
            memory.append({"role": "assistant", "content": raw_response})
            return raw_response
            
    return "I timed out processing that."