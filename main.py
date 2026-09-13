import os
import time
import random
import threading
import logging
from datetime import datetime
import pytz
import telebot
import certifi
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from pymongo import MongoClient

# 📚 Try to import QUIZ_LIST from questions file, else use fallback
try:
    from questions import QUIZ_LIST
except ImportError:
    QUIZ_LIST = [
        {
            "question": "Python ka avishkar kisne kiya tha?",
            "options": ["Dennis Ritchie", "Guido van Rossum", "James Gosling", "Bjarne Stroustrup"],
            "correct_id": 1,
            "explanation": "Guido van Rossum ne 1991 me Python ko release kiya."
        }
    ]

# Load credentials from environment
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SUPPORT_GROUP_ID = os.getenv("SUPPORT_GROUP_ID")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 8443))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not API_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variables me nahi mila!")

# Enable Telebot Middleware
apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(API_TOKEN)
telebot.logger.setLevel(logging.CRITICAL)

# Global structures
active_ban_timers = {}
BOT_USERNAME = "Bot"
DAILY_MSG_LIMIT = 40  

try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    pass

if OWNER_ID:
    try: OWNER_ID = int(OWNER_ID)
    except ValueError: OWNER_ID = None

if SUPPORT_GROUP_ID:
    try: SUPPORT_GROUP_ID = int(SUPPORT_GROUP_ID)
    except ValueError: SUPPORT_GROUP_ID = None

# MongoDB Initialisation
# main.py ke MongoDB Setup block ko isse badlein:

if MONGO_URI:
    client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where() # Yeh line Python 3.14 ko valid secure certificate degi
    )
    db = client["quiz_bot_db"]
    groups_col = db["groups"]
    users_col = db["users"]
    poll_mapping_col = db["poll_mapping"]
    daily_scores_col = db["daily_scores"]
    bot_settings_col = db["bot_settings"]
    print("✅ MongoDB Database Connected Successfully!")
else:
    raise ValueError("MONGO_URI nahi mila! Check your variables.")
    

if bot_settings_col.count_documents({"key": "leaderboard_time"}) == 0:
    bot_settings_col.insert_one({"key": "leaderboard_time", "value": "22:00"})

# Helper utilities
def is_user_admin(chat_id, user_id):
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

def escape_html(text):
    if not text: return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
def truncate_explanation(explanation_text, max_length=100):
    if not explanation_text: return None
    explanation_text = str(explanation_text).strip()
    if len(explanation_text) <= max_length: return explanation_text
    truncated = explanation_text[:max_length].rsplit(' ', 1)[0] + "..."
    return truncated

# Midnight reset tracker
def auto_reset_midnight_loop():
    tz = pytz.timezone('Asia/Kolkata')
    while True:
        try:
            now = datetime.now(tz)
            if now.hour == 0 and now.minute == 0:
                users_col.update_many({}, {"$set": {"msg_count": 0}})
                print("⏰ Success: Daily message limit automatic reset ho gayi!")
                time.sleep(60)
        except Exception as e:
            print(f"Error in automatic reset thread: {e}")
        time.sleep(30)

threading.Thread(target=auto_reset_midnight_loop, daemon=True).start()

# Scheduler for sending polls automatically
# =====================================================================
# 📊 GLOBAL POLL SCHEDULER MANAGER (Authorized Group & Admin Check Override)
# =====================================================================
def global_poll_manager():
    while True:
        try:
            # Database se sabhi active groups ki list nikalna
            all_groups = list(groups_col.find())
            current_now = time.time()

            for group in all_groups:
                chat_id = group["chat_id"]
                current_index = group.get("current_index", 0)
                last_poll_id = group.get("last_poll_id", None)
                last_sent_time = group.get("last_sent_time", 0)
                language = group.get("language", "hindi")
                interval = group.get("interval", 1800)
                auto_delete = group.get("auto_delete", 1)
                last_warning_time = group.get("last_warning_time", 0)

                # Time interval check (Kya naya poll bhejne ka samay ho gaya hai?)
                if current_now - last_sent_time >= interval:
                    
                    # 🔍 1. Live Telegram Admin Status Verification
                    is_bot_admin = False
                    try:
                        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
                        if bot_member.status in ['administrator', 'creator']:
                            is_bot_admin = True
                    except Exception:
                        is_bot_admin = False

                    # 🚫 2. Agar bot admin nahi hai, toh poll skip karega aur warning alert bhejega
                    if not is_bot_admin:
                        warning_interval = 43200  # 12 ghante me ek baar alert
                        if current_now - last_warning_time >= warning_interval:
                            try:
                                bot.send_message(
                                    chat_id=chat_id, 
                                    text="⚠️ **ALERT!**\n\nMain is group me automated quizzes nahi bhej sakta kyunki mere paas **Admin Rights** nahi hain. Kripya mujhe admin banayein aur permissions grant karein.", 
                                    parse_mode="Markdown"
                                )
                                groups_col.update_one({"chat_id": chat_id}, {"$set": {"last_warning_time": current_now}})
                            except Exception: pass
                        
                        # Timer ko update kar dete hain taaki har 5 second me retry na kare
                        groups_col.update_one({"chat_id": chat_id}, {"$set": {"last_sent_time": current_now}})
                        continue

                    # 🗑️ 3. Puraana poll automatically delete karna (agar settings me ON ho)
                    if last_poll_id is not None and auto_delete == 1:
                        try:
                            bot.delete_message(chat_id=chat_id, message_id=last_poll_id)
                        except Exception: pass

                    # 🌐 4. Language selection filter
                    filtered_quiz = [q for q in QUIZ_LIST if q.get("lang", "hindi") == language]
                    if not filtered_quiz: 
                        filtered_quiz = QUIZ_LIST
                    
                    if current_index >= len(filtered_quiz): 
                        current_index = 0

                    quiz = filtered_quiz[current_index]
                    explanation_text = truncate_explanation(quiz.get("explanation", None), max_length=100)
                    
                    # 🚀 5. Native Telegram Quiz Poll Dispatcher
                    try:
                        sent_message = bot.send_poll(
                            chat_id=chat_id, 
                            question=quiz["question"], 
                            options=quiz["options"],
                            type="quiz", 
                            correct_option_id=quiz["correct_id"], 
                            is_anonymous=False, 
                            explanation=explanation_text
                        )
                        
                        # Unique Poll ID ko database map me record karna live scoring ke liye
                        poll_mapping_col.insert_one({
                            "poll_id": str(sent_message.poll.id), 
                            "chat_id": chat_id,
                            "correct_id": quiz["correct_id"], 
                            "creation_time": time.time()
                        })

                        # Next index calculator aur timer update loop
                        new_index = (current_index + 1) % len(filtered_quiz)
                        groups_col.update_one({"chat_id": chat_id}, {
                            "$set": {
                                "current_index": new_index, 
                                "last_poll_id": sent_message.message_id, 
                                "last_sent_time": current_now
                            }
                        })
                        print(f"✅ Auto-Poll successfully sent to group ID: {chat_id}")

                    except Exception as e:
                        error_str = str(e).lower()
                        # Agar bot ko group se nikal diya gaya hai toh record DB se saaf karein
                        if "bot was kicked" in error_str or "chat not found" in error_str or "bot is not a member" in error_str:
                            groups_col.delete_one({"chat_id": chat_id})
                            print(f"🗑️ Removed group {chat_id} from database (Bot left/kicked)")
                        else:
                            groups_col.update_one({"chat_id": chat_id}, {"$set": {"last_sent_time": current_now}})
                            
        except Exception as e:
            print(f"❌ Database global loop error: {e}")
        
        time.sleep(5) # Har 5 second me database scan pipeline refresh hogi
        
# Config layout renderers
def get_settings_markup(chat_id):
    res = groups_col.find_one({"chat_id": chat_id})
    if not res: return None, None
    lang = res.get("language", "hindi")
    interval = res.get("interval", 1800)
    auto_delete = res.get("auto_delete", 1)
    
    interval_mins = interval // 60
    del_status = "ON ✅" if auto_delete == 1 else "OFF 📴"
    
    text = (
        "⚙️ *Settings Panel (Quiz Settings)*\n\n"
        f"🌐 *Current Language:* {lang.upper()}\n"
        f"⏱️ *Quiz Interval:* {interval_mins} min\n"
        f"🗑️ *Auto Delete Poll:* {del_status}\n\n"
        "*Click on the buttons below to change configurations:*"
    )
    markup = InlineKeyboardMarkup()
    lang_text = "🌐 भाषा: HINDI 🇮🇳" if lang == 'hindi' else "🌐 Lang: ENGLISH 🇬🇧"
    
    markup.row(InlineKeyboardButton(text=lang_text, callback_data=f"set_lang_{chat_id}"))
    markup.row(InlineKeyboardButton(text="🗑️ Auto-Delete Polls", callback_data=f"menu_autodel_{chat_id}"))
    markup.row(InlineKeyboardButton(text="⏱️ 05 Min", callback_data=f"set_time_300_{chat_id}"), InlineKeyboardButton(text="⏱️ 30 Min", callback_data=f"set_time_1800_{chat_id}"))
    markup.row(InlineKeyboardButton(text="⏱️ 45 Min", callback_data=f"set_time_2700_{chat_id}"), InlineKeyboardButton(text="⏱️ 60 Min", callback_data=f"set_time_3600_{chat_id}"))
    markup.row(InlineKeyboardButton(text="Close ❌", callback_data=f"panel_close_{chat_id}"))
    return text, markup

def get_autodelete_markup(chat_id):
    res = groups_col.find_one({"chat_id": chat_id})
    auto_delete = res.get("auto_delete", 1) if res else 1
    status_text = "ON" if auto_delete == 1 else "OFF"
    text = (
        "🗑️ *Auto-Delete Quiz Polls Settings*\n\n"
        f"📊 *Status:* \" {status_text} \"\n\n"
        "👇 *Toggle auto-delete setting:*"
    )
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(text="Turn On ✅", callback_data=f"autodel_on_{chat_id}"), InlineKeyboardButton(text="Turn Off 📴", callback_data=f"autodel_off_{chat_id}"))
    markup.row(InlineKeyboardButton(text="Back 🔙", callback_data=f"autodel_back_{chat_id}"))
    return text, markup

@bot.message_handler(commands=['settings'])
def group_settings(message):
    if message.chat.type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  

    if not is_user_admin(message.chat.id, message.from_user.id):
        try: bot.reply_to(message, "❌ Only group admin's can change the settings.")
        except Exception: pass
        return
        
    row = groups_col.find_one({"chat_id": message.chat.id})
    old_msg_id = row.get("settings_msg_id", 0) if row else 0

    if old_msg_id > 0:
        try: bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception: pass

    text, markup = get_settings_markup(message.chat.id)
    if text: 
        try: 
            new_msg = bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
            groups_col.update_one({"chat_id": message.chat.id}, {"$set": {"settings_msg_id": new_msg.message_id}}, upsert=True)
            try: bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception: pass
        except Exception: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith(('set_lang_', 'set_time_', 'menu_autodel_', 'autodel_', 'panel_close_')))
def handle_settings_callbacks(call):
    user_id = call.from_user.id
    data_parts = call.data.split('_')
    action = data_parts[0]       
    sub_action = data_parts[1]   
    chat_id = int(data_parts[-1]) 
    
    if not is_user_admin(chat_id, user_id):
        bot.answer_callback_query(call.id, "❌ You do not have admin permissions!", show_alert=True)
        return

    if action == "panel" and sub_action == "close":
        groups_col.update_one({"chat_id": chat_id}, {"$set": {"settings_msg_id": 0}})
        try: bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception: pass
        return

    show_main_menu = True
    if action == "set" and sub_action == "lang":
        res = groups_col.find_one({"chat_id": chat_id})
        current_lang = res.get("language", "hindi") if res else 'hindi'
        new_lang = 'english' if current_lang == 'hindi' else 'hindi'
        groups_col.update_one({"chat_id": chat_id}, {"$set": {"language": new_lang}})
        bot.answer_callback_query(call.id, f"Language changed to {new_lang.upper()}")
    elif action == "set" and sub_action == "time":
        new_interval = int(data_parts[2]) 
        groups_col.update_one({"chat_id": chat_id}, {"$set": {"interval": new_interval}})
        bot.answer_callback_query(call.id, f"Interval updated to {new_interval // 60} mins")
    elif action == "menu" and sub_action == "autodel":
        show_main_menu = False
        bot.answer_callback_query(call.id) 
    elif action == "autodel":
        if sub_action == "on":
            groups_col.update_one({"chat_id": chat_id}, {"$set": {"auto_delete": 1}})
            bot.answer_callback_query(call.id, "Auto-Delete ON")
            show_main_menu = False
        elif sub_action == "off":
            groups_col.update_one({"chat_id": chat_id}, {"$set": {"auto_delete": 0}})
            bot.answer_callback_query(call.id, "Auto-Delete OFF")
            show_main_menu = False
        elif sub_action == "back":
            bot.answer_callback_query(call.id, "Going Back Menu...")
            show_main_menu = True
        
    if show_main_menu: text, markup = get_settings_markup(chat_id)
    else: text, markup = get_autodelete_markup(chat_id)
        
    try: bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except Exception: pass

@bot.message_handler(commands=['settime'])
def set_global_leaderboard_time(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))):
        try: bot.send_message(message.chat.id, "❌ Restricted to owner or authorized group.")
        except Exception: pass
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "Format: `/settime HH:MM`", parse_mode="Markdown")
        return
    time_str = args[1].strip()
    try:
        datetime.strptime(time_str, "%H:%M")
        bot_settings_col.update_one({"key": "leaderboard_time"}, {"$set": {"value": time_str}}, upsert=True)
        bot.send_message(message.chat.id, f"✅ Time updated to **{time_str}**", parse_mode="Markdown")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid time format! Use HH:MM format.")

@bot.message_handler(commands=['broadcast'])
def handle_owner_broadcast(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))):
        return
    if not message.reply_to_message:
        bot.send_message(message.chat.id, "⚠️ *Reply to message* with `/broadcast` to proceed.", parse_mode="Markdown")
        return
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(text="YES (Pin)", callback_data=f"bcast_yes_{message.reply_to_message.message_id}"),
               InlineKeyboardButton(text="NO (Don't Pin)", callback_data=f"bcast_no_{message.reply_to_message.message_id}"))
    bot.send_message(chat_id=message.chat.id, text="🏵️ *Pin this broadcast message everywhere?*", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith(('bcast_yes_', 'bcast_no_')))
def execute_broadcast_callback(call):
    if OWNER_ID and call.from_user.id != OWNER_ID: return
    data_parts = call.data.split('_')
    should_pin = (data_parts[1] == 'yes')
    target_msg_id = int(data_parts[2])

    original_markup = None
    try:
        orig_msg = bot.forward_message(chat_id=call.message.chat.id, from_chat_id=call.message.chat.id, message_id=target_msg_id)
        if orig_msg and hasattr(orig_msg, 'reply_markup'): original_markup = orig_msg.reply_markup
        bot.delete_message(chat_id=call.message.chat.id, message_id=orig_msg.message_id)
    except Exception: pass

    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="📢 **Initializing broadcast...**")
    all_chats = list(groups_col.find())
    all_users = list(users_col.find())
    g_success, g_fail, u_success, u_fail = 0, 0, 0, 0

    for group in all_chats:
        try:
            sent_msg = bot.copy_message(chat_id=group["chat_id"], from_chat_id=call.message.chat.id, message_id=target_msg_id, reply_markup=original_markup)
            if should_pin and sent_msg: bot.pin_chat_message(chat_id=group["chat_id"], message_id=sent_msg.message_id)
            g_success += 1
            time.sleep(0.15)
        except Exception: g_fail += 1

    for user in all_users:
        try:
            bot.copy_message(chat_id=user["user_id"], from_chat_id=call.message.chat.id, message_id=target_msg_id, reply_markup=original_markup)
            u_success += 1
            time.sleep(0.15)
        except Exception: u_fail += 1

    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                          text=f"📊 *Global Broadcast Report:*\nGroups Done: {g_success} | Fails: {g_fail}\nUsers Done: {u_success} | Fails: {u_fail}", parse_mode="Markdown")

@bot.message_handler(commands=['sendresult'])
def manual_leaderboard_sender(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))):
        return
    status_msg = bot.send_message(message.chat.id, "⏳ Sending results immediately to all groups...")
    send_all_leaderboards(manual=True)
    bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text="✅ Manual results process successfully dispatched.")

def send_all_leaderboards(manual=False):
    IST = pytz.timezone('Asia/Kolkata')
    now = datetime.now(IST)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=f"https://t.me{BOT_USERNAME}?startgroup=true"))
    
    res_time = bot_settings_col.find_one({"key": "leaderboard_time"})
    db_time = res_time["value"] if res_time else "22:00"
    time_label = "(Manual)" if manual else f"| Time: {db_time}"

    all_chats = list(groups_col.find())
    for group in all_chats:
        chat_id = group["chat_id"]
        all_users = list(daily_scores_col.find({"chat_id": chat_id}))
        calculated_leaderboard = []
        for doc in all_users:
            correct = doc.get("correct_count", 0)
            wrong = doc.get("wrong_count", 0)
            final_score = (correct * 2) - (wrong * 0.5)
            if (correct + wrong) > 0:
                calculated_leaderboard.append((final_score, doc.get("user_name", "User"), correct, wrong))
        
        calculated_leaderboard.sort(key=lambda x: x, reverse=True)
        top_20 = calculated_leaderboard[:20]
        
        lb_text = "🏆 *Result [Top 20 user's Leaderboard]*\n---------------------------------------\n"
        lb_text += f"📅 *Date:* {now.strftime('%d-%m-%Y')} {time_label}\n📊 Marking: Right (+2) | Wrong (-0.5)\n---------------------------------------\n\n"
        
        if top_20:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for idx, (score, name, correct, wrong) in enumerate(top_20, 1):
                medal = medals.get(idx, f"{idx}.")
                display_score = f"{score:.1f}" if score % 0.5 != 0 else f"{int(score)}"
                lb_text += f"{medal} *{name}*\nRight: **{correct}** ✅ | Wrong: **{wrong}** ❌\nScore: **{display_score}** Marks\n---------------------------------------\n"
        else:
            lb_text += "⚠️ No users participated in the quiz today.\n---------------------------------------\n"
            
        lb_text += "\n🎯 Amazing effort! Use `/myscore` command anytime."
        try:
            bot.send_message(chat_id=chat_id, text=lb_text, reply_markup=markup, parse_mode="Markdown")
            time.sleep(0.15)
        except Exception: pass
        
    daily_scores_col.delete_many({})
    poll_mapping_col.delete_many({})
    

def daily_leaderboard_scheduler():
    has_sent_today = False
    last_checked_date = ""
    while True:
        try:
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime.now(IST)
            current_date_str = now.strftime("%Y-%m-%d")
            
            if current_date_str != last_checked_date:
                has_sent_today = False
                last_checked_date = current_date_str

            res = bot_settings_col.find_one({"key": "leaderboard_time"})
            db_time = res["value"] if res else "22:00"
            try: target_hour, target_minute = map(int, db_time.split(':'))
            except Exception: target_hour, target_minute = 22, 0
            
            if now.hour == target_hour and now.minute == target_minute and not has_sent_today:
                send_all_leaderboards(manual=False)
                has_sent_today = True
                time.sleep(60) 
        except Exception as e: print(f"Scheduler Error: {e}")
        time.sleep(20)

threading.Thread(target=daily_leaderboard_scheduler, daemon=True).start()

# 🎯 Track user score updates from poll submission
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    poll_id = str(poll_answer.poll_id)
    user_id = poll_answer.user.id
    if not poll_answer.option_ids: return

    mapping = poll_mapping_col.find_one({"poll_id": poll_id})
    if not mapping: return  

    chat_id = mapping["chat_id"]
    correct_id = mapping["correct_id"]
    creation_time = mapping.get("creation_time", time.time())
    
    if time.time() - creation_time > 86400: return  
    user_name = f"{poll_answer.user.first_name or ''} {poll_answer.user.last_name or ''}".strip() or f"User_{user_id}"
    is_correct = (poll_answer.option_ids[0] == correct_id)

    inc_field = "correct_count" if is_correct else "wrong_count"
    daily_scores_col.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$inc": {inc_field: 1}, "$set": {"user_name": user_name}},
        upsert=True
    )

@bot.message_handler(commands=['myscore'])
def check_user_score(message):
    if message.chat.type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  
    try: bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception: pass

    res = daily_scores_col.find_one({"chat_id": message.chat.id, "user_id": message.from_user.id})
    correct, wrong, old_score_msg_id = (res.get("correct_count", 0), res.get("wrong_count", 0), res.get("last_score_msg_id", 0)) if res else (0, 0, 0)
    final_score = (correct * 2) - (wrong * 0.5)
    display_score = str(int(final_score)) if final_score.is_integer() else f"{final_score:.1f}"

    if old_score_msg_id > 0:
        try: bot.delete_message(chat_id=message.chat.id, message_id=old_score_msg_id)
        except Exception: pass

    score_text = (
        f"🏆 *Congratulations {message.from_user.first_name}, your score!*\n-------------------------------------\n"
        f"Right: {correct} ✅ | Wrong: {wrong} ❌\n*Final Score: {display_score} Marks*\n-------------------------------------\n"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="ᴄʟᴏꜱᴇ ᴄᴀʀᴅ", callback_data=f"close_score_{message.from_user.id}"))
    new_card = bot.send_message(chat_id=message.chat.id, text=score_text, parse_mode="Markdown", reply_markup=markup)
    daily_scores_col.update_one({"chat_id": message.chat.id, "user_id": message.from_user.id}, {"$set": {"last_score_msg_id": new_card.message_id}}, upsert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("close_score_"))
def close_score_card(call):
    card_owner_id = int(call.data.split("_")[2])
    if call.from_user.id == card_owner_id:
        try: bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception: pass
    else:
        try: bot.answer_callback_query(callback_query_id=call.id, text="⚠️ You can only close your own card!", show_alert=True)
        except Exception: pass

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_type = message.chat.type
    
    if chat_type in ['group', 'supergroup']:
        if f"@{BOT_USERNAME}" in message.text and not message.text.startswith(f"/start@{BOT_USERNAME}"): return
        row = groups_col.find_one({"chat_id": message.chat.id})
        old_start_id = row.get("start_msg_id", 0) if row else 0
        if old_start_id > 0:
            try: bot.delete_message(chat_id=message.chat.id, message_id=old_start_id)
            except Exception: pass

        g_text = "🎉 *Bot activated successfully!*\nUse `/settings` command inside group."
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=f"https://t.me/{BOT_USERNAME}?startgroup=true"))
        new_msg = bot.send_message(chat_id=message.chat.id, text=g_text, reply_markup=markup, parse_mode="Markdown")
        
        groups_col.update_one({"chat_id": message.chat.id}, {"$set": {"start_msg_id": new_msg.message_id}}, upsert=True)
        try: bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception: pass
        return

    users_col.update_one({"user_id": user_id}, {"$set": {"user_name": message.from_user.first_name, "username": message.from_user.username, "join_time": time.time()}}, upsert=True)
    w_text = "👑 *Welcome Chief!*" if OWNER_ID and user_id == OWNER_ID else f"👋 *Hey {message.from_user.first_name}! Add me to your group to start quizzes.*"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=f"https://t.me/{BOT_USERNAME}?startgroup=true"))
    bot.send_message(chat_id=message.chat.id, text=w_text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def send_help(message):
    chat_type = message.chat.type
    if chat_type in ['group', 'supergroup']:
        row = groups_col.find_one({"chat_id": message.chat.id})
        old_help_id = row.get("help_msg_id", 0) if row else 0
        if old_help_id > 0:
            try: bot.delete_message(chat_id=message.chat.id, message_id=old_help_id)
            except Exception: pass

    help_text = "⚡ *Help & Guide:*\nUse `/settings` to config quiz variables. Automatic Leaderboard is sent daily."
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="Contact Support", url=f"tg://user?id={OWNER_ID or 0}"))
    new_help = bot.send_message(chat_id=message.chat.id, text=help_text, reply_markup=markup, parse_mode="Markdown")
    
    if chat_type in ['group', 'supergroup']:
        groups_col.update_one({"chat_id": message.chat.id}, {"$set": {"help_msg_id": new_help.message_id}})
        try: bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception: pass

@bot.message_handler(commands=['status'])
def send_stats(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))):
        return
    g_count = groups_col.count_documents({})
    u_count = users_col.count_documents({})
    text = f"📊 *Bot Live Status:*\nActive Groups: {g_count}\nActive Users: {u_count}"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# 🤖 BAN SYSTEM & AUTOMATED TIMER
def ban_countdown_thread(target_id, target_mention, message_id_to_edit):
    rem = 5
    while rem > 0:
        time.sleep(60)
        rem -= 1
        if target_id not in active_ban_timers or active_ban_timers[target_id]["status"] != "active": return
        try:
            bot.edit_message_text(chat_id=SUPPORT_GROUP_ID, message_id=message_id_to_edit, 
                                  text=f"⏳ *Ban countdown active...* Time left: {rem} mins. Speak sorry to Owner!", parse_mode="HTML")
        except Exception: pass
    
    if target_id in active_ban_timers and active_ban_timers[target_id]["status"] == "active":
        try:
            bot.promote_chat_member(chat_id=SUPPORT_GROUP_ID, user_id=target_id, can_manage_chat=False)
            bot.ban_chat_member(SUPPORT_GROUP_ID, target_id)
            bot.edit_message_text(chat_id=SUPPORT_GROUP_ID, message_id=message_id_to_edit, text="🎯 **Time Over! User has been banned.**")
        except Exception: pass
        active_ban_timers.pop(target_id, None)

@bot.message_handler(commands=['ban'])
def handle_ban_command(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID): return
    user_id_to_ban = message.reply_to_message.from_user.id if message.reply_to_message else None
    if not user_id_to_ban:
        args = message.text.split()
        if len(args) > 1:
            try: user_id_to_ban = int(args[1])
            except ValueError: pass
            
    if not user_id_to_ban or user_id_to_ban == OWNER_ID: return
    mention = f'<a href="tg://user?id={user_id_to_ban}">User</a>'
    warn = bot.reply_to(message, f"⏳ *Ban countdown started for {mention} (5 Mins)!*", parse_mode="HTML")
    active_ban_timers[user_id_to_ban] = {"status": "active", "msg_id": warn.message_id}
    threading.Thread(target=ban_countdown_thread, args=(user_id_to_ban, mention, warn.message_id), daemon=True).start()

@bot.message_handler(commands=['unban'])
def handle_unban_command(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID): return
    user_id_to_unban = message.reply_to_message.from_user.id if message.reply_to_message else None
    if not user_id_to_unban:
        args = message.text.split()
        if len(args) > 1: user_id_to_unban = int(args[1])
        
    if user_id_to_unban:
        bot.unban_chat_member(SUPPORT_GROUP_ID, user_id_to_unban, only_if_banned=True)
        active_ban_timers.pop(user_id_to_unban, None)
        bot.reply_to(message, "✅ User successfully unbanned / pardon granted.")

@bot.message_handler(func=lambda m: m.chat.id == SUPPORT_GROUP_ID and m.text and m.text.lower() == 'cancel')
def handle_cancel_ban(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and message.reply_to_message): return
    t_id = message.reply_to_message.message_id
    for uid, data in list(active_ban_timers.items()):
        if data["msg_id"] == t_id and data["status"] == "active":
            active_ban_timers.pop(uid, None)
            bot.edit_message_text(chat_id=SUPPORT_GROUP_ID, message_id=t_id, text="✅ Ban execution cancelled by Owner.")

@bot.message_handler(commands=['promote'])
def handle_promote_command(message):
    if not (OWNER_ID and message.from_user.id == OWNER_ID and SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID): return
    target_id = message.reply_to_message.from_user.id if message.reply_to_message else None
    if target_id:
        try:
            bot.promote_chat_member(chat_id=SUPPORT_GROUP_ID, user_id=target_id, can_restrict_members=True, can_delete_messages=True, can_pin_messages=True)
            users_col.update_one({"user_id": target_id}, {"$set": {"is_bot_promoted": 1}}, upsert=True)
            bot.reply_to(message, "👑 User Promoted with automated restriction filters.")
        except Exception as e: bot.reply_to(message, f"Error: {e}")

# 💾 MESSAGE RATE LIMITER MIDDLEWARE 
# =====================================================================
# 💾 🤖 AUTOMATIC USER TRACKER & DAILY TEXT LIMITER (Fixed Private Chat Bypass)
# =====================================================================
@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'video', 'sticker', 'document', 'voice', 'audio', 'animation'])
def track_save_and_limit_users(message):
    # Private chat ko block nahi karega, commands chalne dega
    if message.chat.type == 'private':
        return

    # Sirf aur sirf .env wale SUPPORT_GROUP_ID ke andar hi limit check karega
    if SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID:
        if message.from_user and not message.from_user.is_bot:
            u_id = message.from_user.id
            is_core_owner = (OWNER_ID and u_id == OWNER_ID)
            
            row = users_col.find_one({"user_id": u_id})
            current_count = row.get("msg_count", 0) if row else 0
            is_bot_promoted_admin = row.get("is_bot_promoted", 0) if row else 0
            
            apply_limit = False
            if not is_core_owner:
                if is_bot_promoted_admin == 1: 
                    apply_limit = True
                else:
                    try:
                        member = bot.get_chat_member(SUPPORT_GROUP_ID, u_id)
                        if member.status not in ['creator', 'administrator']: 
                            apply_limit = True
                    except Exception: 
                        apply_limit = True

            if apply_limit and current_count >= DAILY_MSG_LIMIT:
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    if current_count == DAILY_MSG_LIMIT:
                        bot.send_message(SUPPORT_GROUP_ID, f"⚠️ User limit of {DAILY_MSG_LIMIT} messages reached. Texts restricted until midnight.")
                except Exception: pass
                users_col.update_one({"user_id": u_id}, {"$inc": {"msg_count": 1}})
                return

            users_col.update_one(
                {"user_id": u_id},
                {"$inc": {"msg_count": 1}, "$set": {"user_name": message.from_user.first_name, "username": message.from_user.username}},
                upsert=True
            )

# =====================================================================
# 💾 🤖 GLOBAL USER DB TRACKER MIDDLEWARE (Crash & Bypass Proof)
# =====================================================================
@bot.middleware_handler(update_types=['message'])
def track_and_save_users(bot_instance, message):
    # Agar user valid hai aur bot nahi hai, toh database me save karega (chahhe chat private ho ya group)
    if message.from_user and not message.from_user.is_bot:
        users_col.update_one(
            {"user_id": message.from_user.id},
            {"$set": {"user_name": message.from_user.first_name, "username": message.from_user.username}},
            upsert=True
        )

# =====================================================================
# 🤖 GROUP JOIN/LEAVE TRACKER (Instant 1st Poll Trigger Enabled)
# =====================================================================
@bot.my_chat_member_handler()
def handle_left_or_joined(my_chat_member):
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status
    chat_id = my_chat_member.chat.id
    chat_title = my_chat_member.chat.title
    
    # 🎯 Jab bot ko group me add kiya jaye ya promote karke ADMIN banaya jaye
    if new_status in ["administrator", "member"]:
        group_exists = groups_col.find_one({"chat_id": chat_id})
        
        # Agar group DB me nahi hai ya bot pehle left karke dubara aaya hai
        if not group_exists or old_status in ["left", "kicked"]:
            # `last_sent_time` ko 0 set kar rahe hain taaki scheduler ise turant pick kare
            groups_col.update_one(
                {"chat_id": chat_id},
                {"$set": {
                    "chat_id": chat_id,
                    "current_index": 0,
                    "last_poll_id": None,
                    "last_sent_time": 0,  # 👈 0 hone se timer instantly trigger hoga
                    "language": "hindi",
                    "interval": 1800,
                    "auto_delete": 1,
                    "last_warning_time": 0
                }},
                upsert=True
            )
            
            # Welcome Message sending logic
            group_text = (
                f"🌟 *Hey everyone,* I'm poll bot, Thanks for the invite 💖\n\n"
                f"🎉 *Joined Group Successfully!*\n"
                f"📢 Automated quizzes have been activated for this group.\n\n"
                f"🚀 *How to Start Quizzes Instantly:*\n"
                f"1. Mujhe is group ka **Admin** banayein.\n"
                f"2. Muje *Manage Polls* aur *Delete Messages* ki permission dein.\n"
                f"3. Jaise hi main Admin banunga, pehla quiz **turant** bhej diya jayega!"
            )
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=f"https://t.me{BOT_USERNAME}?startgroup=true"))
            
            try:
                bot.send_message(chat_id=chat_id, text=group_text, reply_markup=markup, parse_mode="Markdown")
            except Exception: pass

    # 🎯 Agar bot ko pehle se add group me ab ADMIN bana diya gaya hai
    if new_status == "administrator" and old_status != "administrator":
        # `last_sent_time` ko fir se reset karenge taaki Admin bante hi instantly poll chala jaye
        groups_col.update_one(
            {"chat_id": chat_id},
            {"$set": {"last_sent_time": 0}} # 🚀 Instant Poll Trigger!
        )
        try:
            bot.send_message(chat_id=chat_id, text="✅ **Admin Rights Detected!** Aapka pehla automated quiz agle 5-10 seconds me aa raha hai... 🚀")
        except Exception: pass

    elif new_status in ["left", "kicked"]:
        # Bot ko group se nikalne par database clean karein
        groups_col.delete_one({"chat_id": chat_id})
        daily_scores_col.delete_many({"chat_id": chat_id})
        

# 🚀 Webhook server using Flask
from flask import Flask, request
server = Flask(__name__)

@server.route('/' + API_TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{API_TOKEN}")
    return "Webhook Re-configured!", 200

if __name__ == "__main__":
    if RENDER_EXTERNAL_URL:
        print(f"Starting Production Webhook on port {PORT}...")
        bot.remove_webhook()
        bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{API_TOKEN}")
        server.run(host="0.0.0.0", port=PORT)
    else:
        print("Starting Local Development Polling...")
        bot.remove_webhook()
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
