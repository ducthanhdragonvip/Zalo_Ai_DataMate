import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client_remote = OpenAI(
    base_url="https://ragingly-couptative-alaya.ngrok-free.dev/v1",
    api_key=os.environ["MODEL_API_KEY"],
)

response = client_remote.chat.completions.create(
    model="Qwen/Qwen3.5-9B",
    messages=[
        {"role": "user", "content": "Xin chào Qwen, hãy giới thiệu bản thân"}
    ],
    temperature=0,
    max_tokens=1000,
    extra_body={
        "chat_template_kwargs": {
            "enable_thinking": False
        }
    }
)

print("Content:", response.choices[0].message.content)
print("Finish reason:", response.choices[0].finish_reason)
