import os

class Config:
    API_URL = os.getenv("AI_API_URL", "https://openai.uniin.cn/openapi/v2/chat/completions")
    # ⚠️ 安全：此处原为明文 API Key（已泄露，必须到平台作废重建）。
    # 密钥一律从环境变量读取（见 .env.example），绝不写回代码或提交到 git。
    API_KEY = os.getenv("AI_API_KEY", "")
    MODEL_NAME = os.getenv("AI_MODEL_NAME", "qwen-max-latest")
    KNOWLEDGE_DIR = "knowledge_base"
    MAX_RETRIES = 3
    TIMEOUT = 3000
    MAX_CONCURRENT_SPECIALISTS = 5
    SPECIALIST_TIMEOUT = 3000
    MIN_REQUIRED_SPECIALTIES = 2