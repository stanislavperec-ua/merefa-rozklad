import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand
from flask import Flask, request, abort
 
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
RENDER_URL = os.environ.get('RENDER_URL', 'https://merefa-rozklad.onrender.com')
PORT = int(os.environ.get('PORT', 10000))
 
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
 
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
        web_app=WebAppInfo(url='https://stanislavperec-ua.github.io/merefa-rozklad/')
    ))
    return kb
 
def send_rozklad(chat_id):
    bot.send_message(chat_id, WARNING)
    bot.send_message(
        chat_id,
        'Rozklad elektrichok Kharkiv - Merefa',
        reply_markup=webapp_button()
    )
 
@bot.message_handler(commands=['start', 'rozklad'])
def cmd_handler(message):
    send_rozklad(message.chat.id)
 
@bot.message_handler(func=lambda m: True)
def fallback(message):
    send_rozklad(message.chat.id)
 
@app.route('/')
def index():
    return 'OK', 200
 
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
        return '', 200
    abort(403)
 
def setup():
    bot.remove_webhook()
    bot.set_webhook(url=f'{RENDER_URL}/webhook')
    bot.set_my_commands([
        BotCommand('/rozklad', 'Vidkryty rozklad elektrichok'),
        BotCommand('/start', 'Holovne menu'),
    ])
    print(f'Webhook vstanovleno: {RENDER_URL}/webhook')
 
if __name__ == '__main__':
    setup()
    app.run(host='0.0.0.0', port=PORT)
