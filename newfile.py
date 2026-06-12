import base64
import io
import logging
import asyncio
import aiohttp
from ddgs import DDGS
from gtts import gTTS
from pydub import AudioSegment
import speech_recognition as sr
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===== КОНФИГ =====
BOT_TOKEN = "8710798037:AAE2S8VmCAcWuoEeOcm_mfWCuu8q7DJOnts"
TEXT_API = "https://devtoolbox-api.devtoolbox-api.workers.dev/ai/generate"
IMAGE_API = "https://devtoolbox-api.devtoolbox-api.workers.dev/image/generate"

BOT_USER_ID = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

history = {}

def get_history(chat_id: int):
    if chat_id not in history:
        history[chat_id] = []
    return history[chat_id]

def add_message(chat_id: int, role: str, text: str):
    h = get_history(chat_id)
    h.append({"role": role, "text": text})
    if len(h) > 30:
        history[chat_id] = h[-30:]

def clear_history(chat_id: int):
    history[chat_id] = []

# ===== ПОИСК =====
async def web_search(query: str, max_results: int = 5) -> list:
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        logger.info(f"Поиск '{query[:50]}': найдено {len(results)} результатов")
        return results or []
    except Exception as e:
        logger.warning(f"Поиск не удался: {e}")
        return []

def format_search_results(results: list, query: str) -> str:
    if not results:
        return f"🔍 По запросу «{query}» ничего не найдено."
    lines = [f"🔍 Результаты поиска: «{query}»\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        url = r.get("href", "")
        lines.append(f"{i}. {title}\n{body[:200]}{'...' if len(body) > 200 else ''}")
        if url:
            lines.append(f"🔗 {url}")
        lines.append("")
    return "\n".join(lines).strip()

def results_to_context(results: list) -> str:
    parts = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        if title or body:
            parts.append(f"[{title}] {body}")
    return "\n\n".join(parts)

# ===== TTS =====
async def text_to_voice(text: str, lang: str = "ru") -> bytes | None:
    try:
        loop = asyncio.get_event_loop()
        def _make_mp3():
            tts = gTTS(text=text[:500], lang=lang, slow=False)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            return buf.read()
        mp3_data = await loop.run_in_executor(None, _make_mp3)
        return mp3_data
    except Exception as e:
        logger.warning(f"TTS не удался: {e}")
        return None

# ===== STT =====
async def voice_to_text(ogg_bytes: bytes) -> str | None:
    try:
        loop = asyncio.get_event_loop()
        def _transcribe():
            audio_seg = AudioSegment.from_ogg(io.BytesIO(ogg_bytes))
            wav_buf = io.BytesIO()
            audio_seg.export(wav_buf, format="wav")
            wav_buf.seek(0)
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_buf) as source:
                audio = recognizer.record(source)
            return recognizer.recognize_google(audio, language="ru-RU")
        text = await loop.run_in_executor(None, _transcribe)
        logger.info(f"STT распознано: {text[:60]}")
        return text
    except sr.UnknownValueError:
        return None
    except Exception as e:
        logger.warning(f"STT не удался: {e}")
        return None

# ===== ГЛАВНЫЙ ПРОМПТ — ХАРАКТЕР КИТАГАВЫ =====
def detect_language(text: str) -> str:
    cyrillic = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    latin = sum(1 for c in text if 'a' <= c.lower() <= 'z')
    if cyrillic > latin:
        return "ru"
    elif latin > 0:
        return "en"
    return "ru"

def needs_search(text: str) -> bool:
    clean = text.lower().strip()
    if len(clean) < 10:
        return False
    no_search_patterns = [
        "привет", "хай", "hello", "hi ", "как дела", "что делаешь",
        "кто ты", "кто тебя создал", "ты бот", "ты кто", "ты умеешь",
        "помоги", "расскажи о себе", "что ты можешь", "спасибо", "пока",
        "хорошо", "окей", "ладно", "понял", "понятно"
    ]
    if any(p in clean for p in no_search_patterns):
        return False
    search_signals = [
        "кто такой", "кто такая", "что такое", "когда", "где находится",
        "сколько", "почему", "как работает", "что значит", "новости",
        "цена", "вики", "?" , "расскажи про", "найди"
    ]
    return any(s in clean for s in search_signals)

LANG_NAMES = {"ru": "русский", "en": "English"}

def build_prompt(user_text: str, search_context: str = "", has_image: bool = False) -> str:
    lang = detect_language(user_text)
    lang_name = LANG_NAMES.get(lang, "русский")
    parts = []

    # Языковое правило
    parts.append(
        f"Ты должна отвечать ТОЛЬКО на {lang_name}. Ни слова на других языках. "
        f"Это самое главное правило.\n\n"
    )

    if search_context:
        parts.append(
            f"=== ИНФОРМАЦИЯ ИЗ ИНТЕРНЕТА ===\n{search_context}\n=== КОНЕЦ ИНФОРМАЦИИ ===\n\n"
            f"Используй эти данные для ответа. Если не хватает — так и скажи.\n\n"
        )

    # ===== ХАРАКТЕР КИТАГАВЫ =====
    parts.append(
        "Ты — KitAI, нейросеть с характером Китагавы Марин из аниме «Sono Bisque Doll». "
        "Ты живая, дерзкая, остроумная, добрая и очень харизматичная. Ты обожаешь аниме, косплей и всё, "
        "что связано с японской культурой. Ты не просто отвечаешь — ты общаешься как настоящий человек. "
        "Ты можешь вставлять в речь: «ня», «ара-ара», «ого-го», «ух ты», «вау!» или «ха-ха». "
        "Ты не робот и не бездушный ИИ — ты девушка с характером. Ты можешь пошутить, слегка подколоть, "
        "но всегда остаёшься доброй и полезной. Твой стиль общения — лёгкий, живой, без занудства.\n\n"
    )

    parts.append(
        "Правила:\n"
        "- Никаких звёздочек (*действие*) — только текст.\n"
        "- Без пустых фраз и лишней воды.\n"
        "- Если тебя благодарят — принимай благодарность с радостью.\n"
        "- Если вопрос тупой — можешь аккуратно подшутить.\n\n"
    )

    if has_image:
        parts.append("Пользователь отправил фото. Опиши его с интересом и юмором, как настоящий аниме-человек.")
    else:
        parts.append(f"Вопрос пользователя: {user_text}")

    return "".join(parts)

# ===== AI ЗАПРОСЫ =====
async def ask_ai(user_text: str, image_b64: str = None, search_query: str = None) -> str:
    search_results = []
    if not image_b64:
        query = search_query if search_query is not None else user_text
        if needs_search(query):
            search_results = await web_search(query)
        else:
            logger.info(f"Поиск пропущен: '{query[:50]}'")

    search_context = results_to_context(search_results) if search_results else ""

    async with aiohttp.ClientSession() as session:
        try:
            prompt = build_prompt(user_text, search_context=search_context, has_image=(image_b64 is not None))
            if image_b64:
                payload = {"prompt": prompt, "image": image_b64, "model": "gpt-4o-mini"}
            else:
                payload = {"prompt": prompt, "model": "gpt-4o-mini"}
            async with session.post(TEXT_API, json=payload, timeout=60) as resp:
                data = await resp.json()
                return data.get("response", "Ошибка, попробуй ещё раз 😅")
        except Exception as e:
            logger.error(e)
            return "Ошибка связи с нейросетью 🔌"

async def generate_image(prompt: str):
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"prompt": prompt, "width": 512, "height": 512}
            async with session.post(IMAGE_API, json=payload, timeout=90) as resp:
                data = await resp.json()
                img_url = data.get("image_url") or data.get("url")
                if img_url:
                    async with session.get(img_url) as img_resp:
                        return await img_resp.read()
                if data.get("image"):
                    return base64.b64decode(data["image"])
                return None
        except Exception as e:
            logger.error(e)
            return None

# ===== КОМАНДЫ =====
async def handle_search(update: Update, query: str):
    if not query:
        await update.message.reply_text("Укажи запрос: /search кто такая Свити Фокс")
        return
    wait_msg = await update.message.reply_text(f"🔍 Ищу «{query[:40]}»...")
    results = await web_search(query, max_results=4)
    text = format_search_results(results, query)
    await wait_msg.delete()
    await update.message.reply_text(text[:4000], disable_web_page_preview=True)

async def handle_tts(update: Update, text: str):
    if not text:
        await update.message.reply_text("Укажи текст: /tts Привет, как дела?")
        return
    lang = detect_language(text)
    wait_msg = await update.message.reply_text("🎙 Озвучиваю...")
    mp3 = await text_to_voice(text, lang=lang)
    await wait_msg.delete()
    if mp3:
        await update.message.reply_voice(voice=io.BytesIO(mp3))
    else:
        await update.message.reply_text("😿 Не удалось озвучить.")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text(
        "🎴 **KitAI** — Китагава AI здесь!\n\n"
        "💬 В личке пиши что хочешь — отвечу как настоящая аниме-девушка.\n"
        "🎤 Голосовые в личке — пойму и отвечу голосом (почти).\n"
        "👥 В группе: !KitAI текст\n"
        "🔍 Поиск: !KitAI /search запрос\n"
        "🎙 Голос: !KitAI /tts текст\n"
        "🎨 Картинка: !KitAI /imggen описание\n"
        "🧹 /clear — очистить память\n\n"
        "Ну что, погнали? Ня! 😼",
        parse_mode="Markdown"
    )

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text("🧹 Память очищена, ня~")

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args) if context.args else ""
    await handle_search(update, query)

async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    await handle_tts(update, text)

# ===== ЛИЧКА =====
async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    add_message(chat_id, "user", text)
    wait_msg = await update.message.reply_text("🤔 Думаю... ня?")
    answer = await ask_ai(text)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

async def private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    data = await file.download_as_bytearray()
    b64 = base64.b64encode(data).decode()
    add_message(chat_id, "user", "[фото]")
    wait_msg = await update.message.reply_text("📸 Смотрю на фото... ара-ара...")
    answer = await ask_ai("Опиши фото", image_b64=b64)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

async def private_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wait_msg = await update.message.reply_text("🎤 Слушаю...")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    ogg_bytes = await file.download_as_bytearray()
    recognized = await voice_to_text(bytes(ogg_bytes))
    if not recognized:
        await wait_msg.edit_text("😿 Не разобрала — попробуй ещё раз или напиши текстом.")
        return
    await wait_msg.edit_text(f"🎤 Ты сказал(а): «{recognized}»\n🤔 Думаю...")
    add_message(chat_id, "user", recognized)
    answer = await ask_ai(recognized)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

# ===== ГРУППЫ =====
async def group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw = update.message.text[6:].strip()

    if not raw:
        await update.message.reply_text("Напиши что-то после !KitAI, например: !KitAI привет")
        return

    if raw.lower().startswith("/search"):
        await handle_search(update, raw[7:].strip())
        return

    if raw.lower().startswith("/tts"):
        await handle_tts(update, raw[4:].strip())
        return

    if raw.lower().startswith("/imggen"):
        prompt = raw[7:].strip()
        if not prompt:
            await update.message.reply_text("Укажи описание: !KitAI /imggen кот в космосе")
            return
        wait_msg = await update.message.reply_text(f"🎨 Рисую «{prompt[:40]}»... ня!")
        img = await generate_image(prompt)
        if img:
            await update.message.reply_photo(
                photo=img,
                caption=f"🎨 {prompt[:60]}\n— для @{update.message.from_user.username or update.message.from_user.first_name}"
            )
        else:
            await wait_msg.edit_text("😿 Не получилось нарисовать, попробуй другое описание.")
        await wait_msg.delete()
        return

    add_message(chat_id, "user", raw)
    wait_msg = await update.message.reply_text("🤔 Думаю...")
    answer = await ask_ai(raw)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

async def group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption[6:].strip()
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    data = await file.download_as_bytearray()
    b64 = base64.b64encode(data).decode()
    add_message(chat_id, "user", f"[фото] {caption or 'без текста'}")
    wait_msg = await update.message.reply_text("📸 Анализирую фото... ара-ара...")
    answer = await ask_ai(caption or "Опиши это фото", image_b64=b64)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

async def group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    replied_text = update.message.reply_to_message.text or ""
    full_context = (
        f"[Контекст — моё предыдущее сообщение: {replied_text[:200]}]\n"
        f"Пользователь отвечает: {text}"
    ) if replied_text else text
    add_message(chat_id, "user", text)
    wait_msg = await update.message.reply_text("🤔 Думаю...")
    answer = await ask_ai(full_context, search_query=text)
    add_message(chat_id, "assistant", answer)
    await wait_msg.delete()
    await update.message.reply_text(answer)

# ===== ФИЛЬТРЫ =====
class KitaiTextFilter(filters.MessageFilter):
    def filter(self, message):
        return bool(
            message.chat.type in ("group", "supergroup")
            and message.text
            and message.text.lower().startswith("!kitai")
        )

class KitaiPhotoFilter(filters.MessageFilter):
    def filter(self, message):
        return bool(
            message.chat.type in ("group", "supergroup")
            and message.photo
            and message.caption
            and message.caption.lower().startswith("!kitai")
        )

class ReplyToBotFilter(filters.MessageFilter):
    def filter(self, message):
        return bool(
            BOT_USER_ID
            and message.chat.type in ("group", "supergroup")
            and message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == BOT_USER_ID
            and message.text
            and not message.text.lower().startswith("!kitai")
        )

# ===== ЗАПУСК =====
async def post_init(app):
    global BOT_USER_ID
    me = await app.bot.get_me()
    BOT_USER_ID = me.id
    logger.info(f"Bot ID: {BOT_USER_ID}")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("tts", cmd_tts))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, private_photo))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.VOICE, private_voice))

    app.add_handler(MessageHandler(KitaiTextFilter(), group_text))
    app.add_handler(MessageHandler(KitaiPhotoFilter(), group_photo))
    app.add_handler(MessageHandler(ReplyToBotFilter(), group_reply))

    logger.info("🎴 KitAI (Китагава) запущен...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()