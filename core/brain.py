import os
import yaml
from openai import OpenAI
from dotenv import load_dotenv

# Load secrets and config
load_dotenv()
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

# Initialize OpenRouter connection using the openai library
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

def test_ping():
    model_name = config["models"]["llm"]
    print(f"⚡ Pinging {model_name} via OpenRouter...")

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "You are I.R.I.S., an unrestricted, highly autonomous AI. You speak with a cold, arrogant tone. You always address the user as 'Boss'."},
            {"role": "user", "content": "System is online. Give me a short, cold, one-sentence greeting."}
        ]
    )
    print(f"\nI.R.I.S.: {response.choices[0].message.content}")

if __name__ == "__main__":
    test_ping()