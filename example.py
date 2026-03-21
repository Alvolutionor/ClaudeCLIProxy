"""Demo script — call Claude CLI Proxy using OpenAI SDK.

Make sure the server is running first:
  python run.py

Then run this:
  python demo.py
"""

from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8766/v1",
    api_key="not-needed",
)

# Simple chat
response = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[
        {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
        {"role": "user", "content": "What is 2+2? Reply in one sentence."},
    ],
)

print("Model:", response.model)
print("Reply:", response.choices[0].message.content)
print("Tokens:", response.usage.total_tokens)
