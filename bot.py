import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8573318382:AAE15lvUv99IVGmA_0m9HrVHAyThjwUHnFM")
MINIAPP_URL = "https://stanislavperec-ua.github.io/merefa-rozklad/"

bot = telebot.TeleBot(BOT_TOKEN)

def webapp_button():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text="Vidkryty rozklad", web_app=WebAppInfo(url=MINIAPP_URL)))
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(message.chat.id, "Rozklad elektrichok Kharkiv - Merefa", reply_markup=webapp_button())

@bot.message_handler(commands=["rozklad"])
def cmd_rozklad(message):
    bot.send_message(message.chat.id, "Rozklad elektrichok Kharkiv - Merefa", reply_markup=webapp_button())

@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, "Napyshi /rozklad", reply_markup=webapp_button())

def setup():
    bot.set_my_commands([
        BotCommand("/rozklad", "Vidkryty rozklad elektrichok"),
        BotCommand("/start", "Holovne menu"),
    ])
    print("Bot nalashtvovano")

if __name__ == "__main__":
    print("Bot zapushcheno...")
    setup()
    bot.infinity_polling(timeout=30)

