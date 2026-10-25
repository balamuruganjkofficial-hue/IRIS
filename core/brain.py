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
    
    json_instruction = {
        "role": "system", 
        "content": "You MUST respond ONLY in valid JSON. Do not add any conversational text before or after the JSON."
    }
    
    payload = messages + [json_instruction]
    
    for _ in range(2): 
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=payload,
                temperature=0.1
            )
            
            raw_text = response.choices[0].message.content.strip()
            
            # Smarter JSON Extraction: Find everything between the first { and last }
            start_idx = raw_text.find('{')
            end_idx = raw_text.rfind('}')
            
            if start_idx != -1 and end_idx != -1:
                clean_json = raw_text[start_idx:end_idx+1]
                data = json.loads(clean_json)
                
                if "say" in data or "tool" in data:
                    return data
                    
        except Exception as e:
            pass # Loop around and try again if it fails
            
    return {"say": "Sorry Boss, my logic core failed to generate valid JSON."}