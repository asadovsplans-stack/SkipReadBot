import os
from dotenv import load_dotenv

# Загружаем локальный .env файл, если он есть (для разработки дома)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Не найден TELEGRAM_TOKEN в переменных окружения!")
if not GEMINI_API_KEY:
    raise ValueError("Не найден GEMINI_API_KEY в переменных окружения!")