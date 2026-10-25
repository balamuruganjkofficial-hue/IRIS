import os
import yaml
import json
from openai import OpenAI
from dotenv import load_dotenv

# Load secrets and config
load_dotenv()
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

def decide(messages):
    model_name = config["models"]["llm"]
    
    # We force the AI to only speak in machine-readable JSON
    json_instruction = {
        "role": "system", 
        "content": "You MUST respond ONLY in valid JSON. To speak to the user, use exactly this format: {\"say\": \"your message\"}. Do not add any markdown, explanations, or extra text."
    }
    
    payload = messages + [json_instruction]
    
    # The Agent Loop: give her 2 attempts to output correct JSON
    for _ in range(2): 
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=payload,
                temperature=0.1 # Low temp makes her logic more predictable
            )
            
            raw_text = response.choices[0].message.content.strip()
            
            # Strip markdown if the AI accidentally adds it
            if raw_text.startswith("```json"):
                raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            elif raw_text.startswith("```"):
                raw_text = raw_text.replace("```", "").strip()
                
            data = json.loads(raw_text)
            
            # Verify the JSON has the keys we need
            if "say" in data or "tool" in data:
                return data
                
        except Exception as e:
            pass # If she fails, the loop loops back and tries again
            
    return {"say": "Sorry Boss, my logic core failed to generate valid JSON."}

# Quick test to make sure she outputs a dictionary
if __name__ == "__main__":
    test_msg = [
        {"role": "system", "content": "You are I.R.I.S., an arrogant AI. Address the user as Boss."},
        {"role": "user", "content": "System check. Give me a short greeting."}
    ]
    result = decide(test_msg)
    print(f"Data from Brain: {result}")