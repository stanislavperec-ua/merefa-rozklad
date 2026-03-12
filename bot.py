import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, BotCommand

BOT_TOKEN = "8573318382:AAE15lvUv99IVGmA_0m9HrVHAyThjwUHnFM"
MINIAPP_URL = "https://stanislavperec-ua.github.io/merefa-rozklad/"

bot = telebot.TeleBot(BOT_TOKEN)

def webapp_button():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text="Відкрити розклад", web_app=WebAppInfo(url=MINIAPP_URL)))
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(message.chat.id, "Розклад електричок Харків - Мерефа", reply_markup=webapp_button())

@bot.message_handler(commands=["rozklad"])
def cmd_rozklad(message):
    bot.send_message(message.chat.id, "Розклад електричок Харків - Мерефа", reply_markup=webapp_button())

@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, "Напиши /rozklad", reply_markup=webapp_button())

def setup():
    bot.set_my_commands([
        BotCommand("/rozklad", "Відкрити розклад електричок"),
        BotCommand("/start", "Головне меню"),
    ])
    bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Розклад", web_app=WebAppInfo(url=MINIAPP_URL)))
    print("Бот налаштовано")

if __name__ == "__main__":
    print("Бот запущено...")
    setup()
    bot.infinity_polling(timeout=30)
```

**4.** Натисни **Commit changes** → **Commit changes**

**5.** Завантаж ZIP знову з GitHub → розпакуй (замінивши стару папку)

**6.** У командному рядку:
```
cd Downloads\merefa-rozklad-main\merefa-rozklad-main
python bot.py
