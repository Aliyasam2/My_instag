import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from instagrapi import Client

# توکن ربات تلگرام شما
TELEGRAM_BOT_TOKEN = "8837565058:AAFDtM7wJRI2AOgV67dPDdK-206STorQ1Ew"

# تنظیمات کلاینت اینستاگرام و دیتابیس
insta_client = Client()
SESSION_FILE = "instagram_session.json"
DB_FILE = "bot_database.db"

# راه‌اندازی دیتابیس محلی برای ذخیره اطلاعات شخصی شما
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# تابع لاگین اینستاگرام
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

# منوی اصلی (مشابه ظاهر Regrambot)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔗 اتصال پیج اینستاگرام", callback_data="help_connect")],
        [InlineKeyboardButton("📥 راهنمای دانلود لینک", callback_data="help_download")],
        [InlineKeyboardButton("👁️ پیج‌های تحت نظر (Watchlist)", callback_data="view_watchlist")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🤖 **سلام! خوش آمدید به ربات اختصاصی پل ارتباطی اینستاگرام**\n\n"
        "این ربات به صورت اختصاصی برای شخص شما تنظیم شده است.\n"
        "امکانات فعال:\n"
        "• دانلود مستقیم پست، ریلز، اسلاید و استوری با ارسال لینک\n"
        "• اتصال به اکانت شخصی برای دسترسی به پیج‌های پرایوت\n"
        "• سیستم پایش پیج‌ها (Watchlist)\n\n"
        "از دکمه‌های زیر استفاده کنید یا لینک مد نظرتان را بفرستید:"
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

# مدیریت دکمه‌های شیشه‌ای
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "help_connect":
        await query.message.reply_text(
            "برای اتصال پیج اینستاگرام خود، دستور زیر را در چت بفرستید:\n"
            "`/connect username password`",
            parse_mode="Markdown"
        )
    elif query.data == "help_download":
        await query.message.reply_text(
            " کافیست لینک هر پست، ریلز یا اسلایدی از اینستاگرام را کپی کرده و مستقیماً همینجا بفرستید تا ربات آن را دانلود کند!"
        )
    elif query.data == "view_watchlist":
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM watchlist")
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            list_text = "📋 **پیج‌های تحت نظر شما:**\n" + "\n".join([f"• @{row[0]}" for row in rows])
        else:
            list_text = "📭 هیچ پیجی در لیست Watchlist نیست.\nبرای اضافه کردن از دستور `/watch username` استفاده کنید."
        
        await query.message.reply_text(list_text, parse_mode="Markdown")

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
    
    username, password = args[0], args[1]
    await update.message.reply_text("⏳ در حال احراز هویت و اتصال به اینستاگرام...")
    
    if login_instagram(username, password):
        account_info = insta_client.account_info()
        await update.message.reply_text(f"✅ پیج با موفقیت متصل شد!\nنام کاربری: @{account_info.username}")
    else:
        await update.message.reply_text("❌ خطا در اتصال! ممکن است اینستاگرام چالش امنیتی (کد تایید) خواسته باشد.")

# دستور اضافه کردن به Watchlist
async def add_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("لطفاً یوزرنیم پیج مورد نظر را وارد کنید:\n`/watch target_username`", parse_mode="Markdown")
        return
    
    target = args[0].replace("@", "")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO watchlist (username) VALUES (?)", (target,))
        conn.commit()
        await update.message.reply_text(f"✅ پیج @{target} به لیست Watchlist اضافه شد.")
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"⚠️ پیج @{target} از قبل در لیست وجود دارد.")
    finally:
        conn.close()

# موتور هوشمند دانلود لینک‌های اینستاگرام (پست، ریلز، اسلاید)
async def handle_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "instagram.com" in text:
        await update.message.reply_text("📥 در حال دانلود فایل از اینستاگرام...")
        try:
            media_pk = insta_client.media_pk_from_url(text)
            media = insta_client.media_info(media_pk)
            
            if media.media_type == 1:  # عکس
                path = insta_client.photo_download(media_pk)
                await update.message.reply_photo(photo=open(path, 'rb'))
                os.remove(path)
            elif media.media_type == 2:  # ویدیو / ریلز
                path = insta_client.video_download(media_pk)
                await update.message.reply_video(video=open(path, 'rb'))
                os.remove(path)
            elif media.media_type == 8:  # آلبوم اسلایدی
                paths = insta_client.album_download(media_pk)
                for path in paths:
                    if path.endswith('.mp4'):
                        await update.message.reply_video(video=open(path, 'rb'))
                    else:
                        await update.message.reply_photo(photo=open(path, 'rb'))
                    os.remove(path)
            else:
                await update.message.reply_text("⚠️ فرمت این مدیا پشتیبانی نمی‌شود.")
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در دانلود. مطمئن شوید ابتدا با `/connect` لاگین کرده‌اید.\nجزئیات خطا: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect_page))
    app.add_handler(CommandHandler("watch", add_watchlist))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_instagram_link))
    
    print("Bot is running...")
    app.run_polling()
