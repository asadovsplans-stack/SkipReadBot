import asyncio
import logging
import sys
import sqlite3
import re
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from google.genai import types as genai_types

from config import TELEGRAM_TOKEN, GEMINI_API_KEY

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# --- МИКРО-СЕРВЕР ДЛЯ ОБЛАКА (чтобы хостинг не отключал бота) ---
def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        def log_message(self, format, *args):
            pass # Отключаем лишние логи веб-сервера в консоли
            
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

# Запускаем веб-сервер в фоновом потоке
threading.Thread(target=run_dummy_server, daemon=True).start()

# --- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---
DB_NAME = "database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            requests_left INTEGER DEFAULT 5,
            invited_by INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, referrer_id: int = None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT requests_left FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    
    if not row:
        initial_requests = 5
        if referrer_id and referrer_id != user_id:
            cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (referrer_id,))
            if cursor.fetchone():
                initial_requests = 15  # инвайт дает +10 бонусных
                cursor.execute('UPDATE users SET requests_left = requests_left + 10 WHERE user_id = ?', (referrer_id,))
        
        cursor.execute('INSERT INTO users (user_id, requests_left, invited_by) VALUES (?, ?, ?)', 
                       (user_id, initial_requests, referrer_id))
        conn.commit()
        requests_left = initial_requests
    else:
        requests_left = row[0]
        
    conn.close()
    return requests_left

def decrease_user_request(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET requests_left = requests_left - 1 WHERE user_id = ? AND requests_left > 0', (user_id,))
    conn.commit()
    cursor.execute('SELECT requests_left FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

# --- ФУНКЦИЯ ПАРСИНГА СТАТЕЙ ПО ССЫЛКЕ ---
def extract_text_from_url(url: str) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.extract()
            
        paragraphs = soup.find_all(['p', 'article', 'h1', 'h2', 'h3'])
        text = " ".join([p.get_text() for p in paragraphs])
        
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 100 else None
    except Exception as e:
        logging.error(f"Ошибка парсинга URL {url}: {e}")
        return None

# --- СИСТЕМНЫЙ ПРОМПТ ---
SYSTEM_PROMPT = """
Ты — AI-помощник, который экономит время пользователя.
Твоя задача — превратить длинный текст в результат, который можно прочитать за 20–30 секунд.
Правила:
1. Используй только информацию из предоставленного текста.
2. Ничего не выдумывай. Если информации недостаточно — так и скажи.
3. Не добавляй собственные выводы, которых нет в тексте.
4. Не пересказывай текст целиком.
5. Пиши максимально кратко.
6. Избегай вводных слов, повторов и общих фраз.
7. Ответ должен хорошо читаться на экране смартфона.
8. Каждый пункт — не более 1–2 предложений.
9. Если в тексте есть задачи, просьбы, дедлайны или решения — обязательно выдели их.
Формат ответа:
⚡ Главное
• ...
• ...
• ...
✅ Что нужно сделать
(если действий нет, напиши: "Явных действий нет.")
• ...
• ...
⚠️ Важно
(этот блок показывай только если в тексте есть сроки, цифры, ограничения, предупреждения или критически важные детали)
• ...
Никогда не используй Markdown-таблицы.
Не используй длинные абзацы.
Не добавляй вступление или заключение.
Начинай сразу с результата.
"""

user_texts = {}

@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    args = command.args
    
    referrer_id = None
    if args and args.startswith("ref_"):
        try:
            referrer_id = int(args.replace("ref_", ""))
        except ValueError:
            pass

    requests_left = get_or_create_user(user_id, referrer_id)

    text = (
        "⚡ **Перешли длинное сообщение или кинь ссылку на статью.**\n"
        "Я сожму его до самого важного.\n"
        "Меньше чтения.\n"
        "Больше понимания.\n\n"
        f"🎁 У тебя доступно обработок: **{requests_left}**"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.forward_date | (F.text & ~F.text.startswith("/")))
async def handle_long_text(message: types.Message):
    if not message.text:
        await message.answer("Пожалуйста, отправь текст или ссылку.")
        return

    user_id = message.from_user.id
    requests_left = get_or_create_user(user_id)

    if requests_left <= 0:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="get_ref_link")],
            [InlineKeyboardButton(text="⏰ Подождать до завтра", callback_data="wait_tomorrow")]
        ])
        await message.answer(
            "На сегодня бесплатный лимит закончился. ⚡\n"
            "Завтра он обновится автоматически.\n\n"
            "Не хочешь ждать?\n"
            "Пригласи друга по своей ссылке — и вы оба получите +10 дополнительных обработок навсегда.\n"
            "Это занимает меньше минуты.\n"
            "Спасибо, что пользуешься ботом ❤️",
            reply_markup=keyboard
        )
        return

    url_match = re.search(r'https?://[^\s]+', message.text)
    text_to_process = ""

    if url_match:
        url = url_match.group(0)
        status_msg = await message.answer("🌐 Читаю статью по ссылке...")
        extracted = extract_text_from_url(url)
        await status_msg.delete()

        if not extracted:
            await message.answer("⚠️ Не удалось извлечь текст по этой ссылке. Убедись, что сайт доступен, или перешли текст напрямую.")
            return
        text_to_process = extracted
    else:
        if len(message.text) < 50:
            await message.answer("Этот текст слишком короткий. Перешли длинный пост или статью!")
            return
        text_to_process = message.text

    user_texts[user_id] = text_to_process

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Только главное", callback_data="process_main")]
    ])

    await message.answer(
        f"📄 Контент получен. Осталось бесплатных попыток: {requests_left}\n"
        "Нажми кнопку ниже, чтобы сэкономить время:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "process_main")
async def process_summary(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    requests_left = decrease_user_request(user_id)
    if requests_left < 0:
        await callback.answer("Лимит исчерпан!", show_alert=True)
        return

    text_to_process = user_texts.get(user_id)
    if not text_to_process:
        await callback.answer("Текст устарел или не найден. Отправь его заново.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Обрабатываю текст...")

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=text_to_process,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
            )
        )
        
        result_text = response.text + f"\n\n⏱️ Примерно 3 минуты чтения сэкономлено.\n💡 Осталось попыток: {requests_left}"
        await callback.message.edit_text(result_text)

    except Exception as e:
        logging.error(f"Ошибка при запросе к Gemini: {e}")
        await callback.message.edit_text("⚠️ Произошла ошибка при обращении к нейросети. Попробуй позже.")

@dp.callback_query(F.data == "get_ref_link")
async def send_referral_link(callback: types.CallbackQuery):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
    
    await callback.message.answer(
        "Твой инвайт-линк:\n"
        f"`{ref_link}`\n\n"
        "Отправь его другу. Как только он перейдет по нему и запустит бота, вам обоим начислится **+10 бесплатных обработок**! 🚀",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "wait_tomorrow")
async def wait_tomorrow(callback: types.CallbackQuery):
    await callback.message.edit_text("⏰ Договорились! Ждем тебя завтра. Лимиты обновятся автоматически.")

async def main():
    init_db()
    print("Бот успешно запущен в облаке и ждет сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())