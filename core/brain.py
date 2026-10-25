import os
import yaml
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

def decide(messages):
    model_name = config["models"]["llm"]
    
    try:
        # Temperature raised to 0.6 to give her actual personality and wit
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.6 
        )
        return response.choices[0].message.content.strip()
                
    except Exception as e:
        return f"Sorry Boss, my neural link experienced an error: {e}"