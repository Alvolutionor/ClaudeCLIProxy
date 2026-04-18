"""示例脚本 — 使用 OpenAI SDK 调用 Claude CLI 代理。

请先启动代理服务器:
  python run.py

然后运行本脚本:
  python example.py
"""

from openai import OpenAI

# 创建 OpenAI 客户端，指向本地代理服务器
client = OpenAI(
    base_url="http://localhost:8766/v1",
    api_key="not-needed",  # 代理服务器不需要 API 密钥
)

# ===== 示例 1: 标准请求 =====
print("--- 标准请求 ---")
response = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[
        {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
        {"role": "user", "content": "What is 2+2? Reply in one sentence."},
    ],
)
print("模型:", response.model)
print("回复:", response.choices[0].message.content)
print("Token数:", response.usage.total_tokens)

# ===== 示例 2: 流式请求 =====
print("\n--- 流式请求 ---")
stream = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[
        {"role": "user", "content": "Explain recursion in one sentence."},
    ],
    stream=True,
)
print("回复: ", end="", flush=True)
# 逐块读取流式响应并打印
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
print()
