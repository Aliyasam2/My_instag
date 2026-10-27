import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from instagrapi import Client

# توکن ربات تلگرام شما (تنظیم شده برای استفاده شخصی)
TELEGRAM_BOT_TOKEN = "8837565058:AAFDtM7wJRI2AOgV67dPDdK-206STorQ1Ew"

# کلاینت اینستاگرام
insta_client = Client()
SESSION_FILE = "instagram_session.json"

# تابع برای لاگین خودکار یا بارگذاری سشن قبلی
def login_instagram(username, password):
    if os.path.exists(SESSION_FILE):
        insta_client.load_settings(SESSION_FILE)
    
    try:
        insta_client.login(username, password)
        insta_client.dump_settings(SESSION_FILE)
        return True
    except Exception as e:
        print(f"Login error: {e}")
        return False

# دستور شروع در تلگرام
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! ربات پل ارتباطی اینستاگرام آماده است.\n\n"
        "برای اتصال پیج خود از دستور زیر استفاده کنید:\n"
        "`/connect username password`",
        parse_mode="Markdown"
    )

# دستور اتصال پیج
async def connect_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "لطفاً یوزرنیم و پسورد را به این صورت بفرستید:\n"
            "`/connect username password`", 
            parse_mode="Markdown"
        )
        return
    
    username = args[0]
    password = args[1]
    
    await update.message.reply_text("⏳ در حال اتصال به اینستاگرام و احراز هویت...")
    
    success = login_instagram(username, password)
    if success:
        account_info = insta_client.account_info()
        await update.message.reply_text(f"✅ پیج با موفقیت متصل شد!\nنام کاربری: @{account_info.username}")
    else:
        await update.message.reply_text("❌ خطا در اتصال! ممکن است اینستاگرام کد تایید (Challenge) خواسته باشد.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect_page))
    
    print("Bot is running...")
    app.run_polling()
