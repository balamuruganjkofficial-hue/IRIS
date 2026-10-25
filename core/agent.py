import re
import datetime
import sqlite3
import threading  # <-- NEW: Allows simultaneous actions
from core import brain
from core import voice
from skills import browser
from skills import os_control

DB_PATH = "data/memory.db"

def get_vitals():
    now = datetime.datetime.now()
    return f"Time: {now.strftime('%I:%M %p')}. Date: {now.strftime('%A, %B %d, %Y')}. OS: Windows."

def get_memory():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS conversation (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
    cursor.execute("SELECT role, content FROM conversation ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    
    history = [{"role": row[0], "content": row[1]} for row in rows]
    
    system_prompt = f"""You are I.R.I.S., a highly intelligent, slightly witty autonomous AI. 
Your Boss is Bala. You are running locally on his Windows machine. 
Speak naturally, confidently, and with personality.

SYSTEM VITALS: {get_vitals()}

TOOL PROTOCOL:
If you need to use a tool, embed this exact tag in your response: <TOOL>tool_name|arguments</TOOL>
- To search the web: <TOOL>browser|search query</TOOL>
- To send a Windows push notification: <TOOL>os|notify: your reminder message</TOOL>"""
    
    return [{"role": "system", "content": system_prompt}] + history

def add_to_memory(role, content):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS conversation (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
    cursor.execute("INSERT INTO conversation (role, content) VALUES (?, ?)", (role, content))
    conn.commit()
    conn.close()

def run(user_text, max_steps=3):
    active_memory = get_memory()
    
    active_memory.append({"role": "user", "content": user_text})
    add_to_memory("user", user_text)
    
    for step in range(max_steps):
        raw_response = brain.decide(active_memory)
        tool_match = re.search(r'<TOOL>(.*?)\|(.*?)</TOOL>', raw_response)
        
        if tool_match:
            tool_name = tool_match.group(1).strip()
            args = tool_match.group(2).strip()
            
            spoken_text = re.sub(r'<TOOL>.*?</TOOL>', '', raw_response).strip()
            
            if spoken_text:
                # MULTITHREADING: Start speaking in the background and immediately move to the next line of code
                speaker_thread = threading.Thread(target=voice.speak, args=(spoken_text,))
                speaker_thread.start()
                
            print(f"⚙️  [SYSTEM: Executing -> {tool_name} with arg: '{args}']")
            
            # The tool executes instantly WHILE she is still talking out loud!
            if tool_name == "browser":
                observation = browser.execute({"query": args})
            elif tool_name == "os":
                observation = os_control.execute(args)
            else:
                observation = f"Error: Tool {tool_name} not recognized."
                
            print(f"👁️  [OBSERVATION: {observation}]")
            
            active_memory.append({"role": "assistant", "content": raw_response})
            add_to_memory("assistant", raw_response)
            
            obs_text = f"System Observation: {observation}. Analyze this and reply briefly."
            active_memory.append({"role": "user", "content": obs_text})
            add_to_memory("user", obs_text)
            continue
            
        else:
            voice.speak(raw_response)
            active_memory.append({"role": "assistant", "content": raw_response})
            add_to_memory("assistant", raw_response)
            return raw_response
            
    return "I timed out processing that."