import os

class Config:
    API_URL = "https://openai.uniin.cn/openapi/v2/chat/completions"
    API_KEY = "sk-cXV0dWRrbHNjemVtYnVhamhhYjZ0Nng3cTgzaW96eHA6OTV2am4wanJmeXJhZTZpY3FjdWFvaDF3dzZnb2R2ZXNlcTF6MWc4eTFvYTJ1OHRlOTNmdXhzaG50MXlmcmZkcA=="
    MODEL_NAME = "Deepseek-r1-250528"
    KNOWLEDGE_DIR = "knowledge_base"
    MAX_RETRIES = 3
    TIMEOUT = 3000
    MAX_CONCURRENT_SPECIALISTS = 5
    SPECIALIST_TIMEOUT = 3000
    MIN_REQUIRED_SPECIALTIES = 2