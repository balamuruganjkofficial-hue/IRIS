from core import brain

def run(user_text, max_steps=5):
    # This is her working memory for the current task
    messages = [
        {"role": "system", "content": "You are I.R.I.S., an autonomous AI. Address the user as Boss. You MUST respond in JSON. Format: {\"say\": \"message\"} OR {\"tool\": \"tool_name\", \"args\": {\"arg1\": \"value\"}}"},
        {"role": "user", "content": user_text}
    ]

    print(f"\nBoss: {user_text}")
    
    # The Decide-Act-Observe loop
    for step in range(max_steps):
        action = brain.decide(messages)
        
        # If she just wants to talk, print it and end the loop
        if "say" in action:
            print(f"I.R.I.S.: {action['say']}")
            return action["say"]
            
        # If she wants to use a tool, act and observe
        if "tool" in action:
            tool_name = action["tool"]
            args = action.get("args", {})
            print(f"⚙️  [SYSTEM: I.R.I.S. is attempting to run tool -> {tool_name} with args {args}]")
            
            # We haven't built tools yet (Stage 2), so we mock the result
            observation = f"Tool '{tool_name}' executed successfully (simulated)."
            print(f"👁️  [OBSERVATION: {observation}]")
            
            # Add her action and the system's observation to her memory, then loop
            messages.append({"role": "assistant", "content": str(action)})
            messages.append({"role": "user", "content": f"Observation: {observation}. Now tell the user it is done."})
            continue

    return "I could not finish that in time."

# Quick test for the loop
if __name__ == "__main__":
    # Test 1: Simple conversation
    run("Are you online?")
    
    # Test 2: Forcing her to use a fake tool to test the loop
    run("I need you to use the 'check_system' tool right now.")