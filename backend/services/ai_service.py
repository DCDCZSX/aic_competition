"""LLM 调用服务（OpenAI 兼容网关）。

安全约定（务必遵守）：
- **绝不把 API Key 写进代码或提交到 git**；
- 密钥只从环境变量读取：`AI_API_KEY`（必填），可选 `AI_API_URL` / `AI_MODEL_NAME` /
  `AI_TIMEOUT` / `AI_MAX_RETRIES`；
- 本地开发在 `CHAIS/.env` 或 `backend/.env` 配置（`.env` 已被 .gitignore 忽略），
  模板见仓库根目录 `.env.example`。
"""

import json
import os
import re
import time
from typing import Optional

import requests

# 默认值：可被环境变量覆盖
DEFAULT_API_URL = "https://openai.uniin.cn/openapi/v2/chat/completions"
DEFAULT_MODEL_NAME = "qwen-max-latest"


class AIService:
    def __init__(self):
        self.api_url = os.getenv("AI_API_URL", DEFAULT_API_URL).strip()
        self.api_key = (os.getenv("AI_API_KEY") or "").strip()
        self.model_name = os.getenv("AI_MODEL_NAME", DEFAULT_MODEL_NAME).strip()
        self.timeout = int(os.getenv("AI_TIMEOUT", "3000"))
        self.max_retries = int(os.getenv("AI_MAX_RETRIES", "3"))

        if not self.api_key:
            print(
                "[AIService] 警告：未检测到环境变量 AI_API_KEY，LLM 调用将被跳过。\n"
                "           请在 CHAIS/.env 或 backend/.env 中配置（参考 .env.example）。\n"
                "           注意：不要把密钥写回代码或提交到 git。"
            )

    def call_ai_api(self, prompt: str, role_description: str = None) -> Optional[str]:
        """调用AI API"""
        if not self.api_key:
            print("[AIService] 错误：缺少 AI_API_KEY，已跳过本次调用。")
            return None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        messages = []
        if role_description:
            messages.append({"role": "system", "content": role_description})
        messages.append({"role": "user", "content": prompt})
        
        data = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 2000
        }
        
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=data,
                    timeout=self.timeout
                )
                response.raise_for_status()
                result = response.json()
                
                if 'choices' in result and len(result['choices']) > 0:
                    content = result['choices'][0]['message']['content']
                    return self.markdown_to_text(content)
                else:
                    print(f"API响应格式异常: {result}")
                    return None
                    
            except requests.exceptions.RequestException as e:
                print(f"API调用失败 (尝试 {attempt+1}/{self.max_retries}): {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                    continue
                return None
            except Exception as e:
                print(f"API处理错误: {str(e)}")
                return None

    def markdown_to_text(self, markdown_text: str) -> str:
        """将Markdown格式转换为整洁的文本格式"""
        if not markdown_text:
            return ""
        
        text = markdown_text
        
        # 移除代码块
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        
        # 处理标题
        text = re.sub(r'^#{1,6}\s*(.+)$', r'\1', text, flags=re.MULTILINE)
        
        # 处理粗体和斜体
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'__([^_]+)__', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)
        
        # 处理链接
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        
        # 处理列表项
        text = re.sub(r'^[\s]*[-*+]\s+', '• ', text, flags=re.MULTILINE)
        text = re.sub(r'^\d+\.\s+', '• ', text, flags=re.MULTILINE)
        
        # 清理多余的空行
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = text.strip()
        
        return text

    def extract_json_from_text(self, text: str) -> Optional[dict]:
        """从文本中提取JSON"""
        try:
            # 尝试直接解析
            return json.loads(text)
        except:
            try:
                # 查找JSON块
                start = text.find('{')
                end = text.rfind('}') + 1
                if start != -1 and end > start:
                    json_str = text[start:end]
                    return json.loads(json_str)
            except:
                pass
        return None