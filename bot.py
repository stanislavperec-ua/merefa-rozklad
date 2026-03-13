import os
import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
MINIAPP_URL = 'https://stanislavperec-ua.github.io/merefa-rozklad/'
PORT = int(os.environ.get('PORT', 10000))

bot = telebot.TeleBot(BOT_TOKEN)

WARNING = (
    '\u26a0\ufe0f УВАГА \u26a0\ufe0f\n\n'
    '\u2757 Цей бот НЕ є офіційним інструментом Укрзалізниці. '
    'Створений на ентузіазмі для зручності пасажирів.\n\n'
    '\u2757 Може не відображати скасування чи зміну маршруту через відсутність '
    'інформації на офіційному сайті УЗ, хоча намагається знаходити її з різних джерел.\n\n'
    '\U0001f50d Завжди перевіряйте наявність поїздів на офіційних ресурсах перед поїздкою!'
)

def webapp_button():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        text='Vidkryty rozklad',
        web_app=WebAppInfo(url=MINIAPP_URL)
    ))
    return kb

@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(message.chat.id, WARNING)
    bot.send_message(
        message.chat.id,
        'Rozklad elektrichok Kharkiv - Merefa',
        reply_markup=webapp_button()
    )

@bot.message_handler(commands=['rozklad'])
def cmd_rozklad(message):
    bot.send_message(message.chat.id, WARNING)
    bot.send_message(
        message.chat.id,
        'Rozklad elektrichok Kharkiv - Merefa',
        reply_markup=webapp_button()
    )

@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, WARNING)
    bot.send_message(
        message.chat.id,
        'Rozklad elektrichok Kharkiv - Merefa',
        reply_markup=webapp_button()
    )

def setup():
    bot.set_my_commands([
        BotCommand('/rozklad', 'Vidkryty rozklad elektrichok'),
        BotCommand('/start', 'Holovne menu'),
    ])
    print('Bot nalashtvovano')

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        pass

def run_web():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    print(f'Web server on port {PORT}')
    server.serve_forever()

if __name__ == '__main__':
    print('Bot zapushcheno, ochikuvannya 30 sekund...')
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    time.sleep(30)
    setup()
    bot.infinity_polling(timeout=30)
