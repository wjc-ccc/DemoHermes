"""
DeepSeek Provider - DeepSeek 模型接入与连通性测试
"""
import os
from pathlib import Path
from openai import OpenAI

from .baseStructure import BaseModel
from ..Provider import DEEPSEEK_API_KEY,DEEPSEEK_BASE_URL



class DeepSeekModel(BaseModel):
    def __init__(self):
        super().__init__()
        self.client = OpenAI(api_key=DEEPSEEK_API_KEY,base_url=DEEPSEEK_BASE_URL)

    def chat(self, messages:list[dict]):
        response = self.client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages)

        ## TODO:解析和处理每一个message -- ToolCall or Response

        return response.choices[0].message.content




##  ✅️测试通过
def test():
    """连通性测试：发送一条简单消息，验证 API 可用。"""
    client = OpenAI(api_key=DEEPSEEK_API_KEY,base_url=DEEPSEEK_BASE_URL)

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "你是一个测试版本的agent loop，用户问什么你简单回答即可"},
            {"role": "user", "content": "你好"},
        ],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}}
    )

    print(response.choices[0].message.content)


if __name__ == "__main__":
    test()
