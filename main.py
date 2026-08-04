import asyncio
import logging
import os
import re


from maxapi import Bot, Dispatcher, F
from maxapi.types import (
    BotStarted,
    MessageCreated,
    MessageCallback,
    CallbackButton,
)
from dotenv import load_dotenv

from maxapi.types.attachments.upload import AttachmentUpload, AttachmentPayload
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.upload_type import UploadType
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.filters.command import CommandStart

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("maxapi_bot")

load_dotenv()

TOKEN = os.getenv("MAX_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ Не задан MAX_BOT_TOKEN — заполни .env")

# Чат группы админов: сюда падают обращения, отсюда приходят ответы
ADMIN_USER_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
if ADMIN_USER_ID == 0:
    logger.warning("⚠️ ADMIN_CHAT_ID не задан — обращения не будут доставляться админам!")

bot = Bot(token=TOKEN)
dp = Dispatcher()
COUNTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "counter.txt")


def get_next_report_number() -> int:
    """Возвращает следующий номер обращения и сохраняет его в файл."""
    try:
        with open(COUNTER_FILE, "r") as f:
            number = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        number = 0
    number += 1
    with open(COUNTER_FILE, "w") as f:
        f.write(str(number))
    return number




class ReportStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo_decision = State()
    waiting_for_photos = State()


async def get_user_lang(context: MemoryContext) -> str:
    """Возвращает текущий язык пользователя из контекста (по умолчанию - 'ru')."""
    data = await context.get_data()
    return data.get("lang", "ru")


async def set_user_lang(context: MemoryContext, lang: str):
    """Сохраняет выбранный язык в контексте."""
    await context.update_data(lang=lang)


def get_text(key: str, lang: str) -> str:
    """Возвращает локализованный текст по ключу и языку (ru/ba)."""
    texts = {
        "welcome": {
            "ru": (
                "Добро пожаловать! Здесь вы можете оставить обращение по поводу ошибок на "
                "вывесках, дорожных указателях, в названиях улиц, остановок и других надписях "
                "в городах и селах Республики Башкортостан. Обращение отправится в работу в "
                "Центр управления Республикой Башкортостан.\n\nЧтобы начать работу, "
                "нажмите 'Оставить обращение'."
            ),
            "ba": (
                "Рәхим итегеҙ! Бында һеҙ Башҡортостан Республикаһының ҡала һәм ауылдарындағы "
                "алтаҡталарҙа, юл күрһәткестәрендә, урам, туҡталыш атамаларында һәм башҡа "
                "яҙыуҙарҙа күргән хаталар буйынса мөрәжәғәт ҡалдыра алаһығыҙ. Мөрәжәғәт "
                "Башҡортостан Республикаһы менән идара итеү үҙәгенә китә.\n\nЭште башлар өсөн "
                "'Мөрәжәғәт ҡалдырырға' төймәһенә баҫығыҙ"
            ),
        },
        "leave_report": {
            "ru": "Оставить обращение",
            "ba": "Мөрәжәғәт ҡалдырырға",
        },
        "change_language": {
            "ru": "Сменить язык",
            "ba": "Телде алмаштырыу",
        },
        "ask_text": {
            "ru": (
                "Обращение должно содержать название населенного пункта, точный адрес. "
                "Напишите, пожалуйста, текст обращения."
            ),
            "ba": (
                "Мөрәжәғәттә ҡала йәки ауылдың исеме, аныҡ адресы булырға тейеш. "
                "Зинһар, мөрәжәғәт тексын яҙығыҙ."
            ),
        },
        "not_text_error": {
            "ru": "Напишите, пожалуйста, текстовое обращение.",
            "ba": "Зинһар, мөрәжәғәт тексын яҙығыҙ.",
        },
        "ask_photo": {
            "ru": "Нужно ли прикрепить фотографии?",
            "ba": "Фото беркетергә кәрәкме?",
        },
        "yes": {"ru": "Да", "ba": "Эйе"},
        "no": {"ru": "Нет", "ba": "Юҡ"},
        "ask_photo_attach": {
            "ru": (
                "Прикрепите, пожалуйста, фотографии (до 3 штук). "
                "Когда закончите, нажмите 'Готово'."
            ),
            "ba": (
                "Зинһар, фотоһүрәттәрегеҙҙе (иң күбе 3 фото) беркетегеҙ. "
                "Беркетеп бөткәс, 'Әҙер' төймәһенә баҫығыҙ."
            ),
        },
        "done": {"ru": "Готово", "ba": "Әҙер"},
        "max_photos": {
            "ru": "Вы уже отправили 3 фотографии. Максимум 3 фото.",
            "ba": "(Вы уже отправили 3 фотографии. Максимум 3 фото.)",
        },
        "photo_added": {
            "ru": "Фотография добавлена. Сейчас прикреплено {count} фото.",
            "ba": "Фотоһүрәт өҫтәлде. Әле {count} фото беркетелгән.",
        },
        "not_photo_error": {
            "ru": (
                "Пожалуйста, прикрепите только фотографии или нажмите 'Готово', "
                "когда закончите."
            ),
            "ba": (
                "Зинһар, фотоларҙы ғына беркетегеҙ йәки, беркетеп бөткәс, 'Әҙер' төймәһенә баҫығыҙ."
            ),
        },
        "thanks_report": {
            "ru": (
                "Спасибо за обращение #{number}! Ответ поступит по мере решения вопроса."
            ),
            "ba": (
                "#{number} мөрәжәғәт өсөн рәхмәт! Яуап мәсьәлә хәл ителгәс киләсәк."
            ),
        },
        "return_main": {
            "ru": "Вернуться на главную",
            "ba": "Төп менюға сығырға",
        },
        "choose_language": {
            "ru": "Выберите язык:",
            "ba": "Тел һайлағыҙ:",
        },
        "lang_ru": {
            "ru": "Русский",
            "ba": "Рус теле",
        },
        "lang_ba": {
            "ru": "Башҡорт теле",
            "ba": "Башҡорт теле",
        },
    }
    return texts[key][lang]


def main_menu_kb(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(
            text=get_text("leave_report", lang),
            payload="start_report",
        )
    )
    builder.row(
        CallbackButton(
            text=get_text("change_language", lang),
            payload="change_language",
        )
    )
    return builder.as_markup()


def photo_decision_kb(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text=get_text("yes", lang), payload="photo_yes"),
        CallbackButton(text=get_text("no", lang), payload="photo_no"),
    )
    return builder.as_markup()


def done_kb(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text=get_text("done", lang), payload="done_photos")
    )
    return builder.as_markup()


def return_to_main_kb(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(
            text=get_text("return_main", lang),
            payload="return_to_main",
        )
    )
    return builder.as_markup()


def language_choice_kb(lang="ru"):
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text=get_text("lang_ru", lang), payload="select_lang_ru")
    )
    builder.row(
        CallbackButton(text=get_text("lang_ba", lang), payload="select_lang_ba")
    )
    return builder.as_markup()


def extract_photo_tokens(message) -> list[str]:
    """Извлекает токены фото-вложений из сообщения."""
    tokens = []
    body = message.body
    if body is None or not body.attachments:
        return tokens
    for att in body.attachments:
        att_type = getattr(att, "type", None)
        if att_type == AttachmentType.IMAGE:
            payload = getattr(att, "payload", None)
            token = getattr(payload, "token", None)
            if token:
                tokens.append(token)
    return tokens


def photo_tokens_to_attachments(tokens: list[str]) -> list:
    """Превращает токены фото в список вложений для переотправки."""
    return [
        AttachmentUpload(
            type=UploadType.IMAGE,
            payload=AttachmentPayload(token=token),
        )
        for token in tokens
    ]



# Старт бота (нажатие "Запустить" в MAX)
@dp.bot_started()
async def on_bot_started(event: BotStarted, context: MemoryContext):
    await context.clear()
    await set_user_lang(context, "ru")
    current_lang = await get_user_lang(context)
    await bot.send_message(
        chat_id=event.chat_id,
        text=get_text("welcome", current_lang),
        attachments=[main_menu_kb(current_lang)],
    )


# Команда /start
@dp.message_created(CommandStart())
async def cmd_start(event: MessageCreated, context: MemoryContext):
    await context.clear()
    await set_user_lang(context, "ru")
    current_lang = await get_user_lang(context)
    await event.message.answer(
        get_text("welcome", current_lang),
        attachments=[main_menu_kb(current_lang)],
    )


# Смена языка
@dp.message_callback(F.callback.payload == "change_language")
async def change_language(event: MessageCallback, context: MemoryContext):
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("choose_language", current_lang),
        attachments=[language_choice_kb(current_lang)],
    )


@dp.message_callback(F.callback.payload == "select_lang_ru")
async def select_lang_ru(event: MessageCallback, context: MemoryContext):
    await set_user_lang(context, "ru")
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("welcome", current_lang),
        attachments=[main_menu_kb(current_lang)],
    )


@dp.message_callback(F.callback.payload == "select_lang_ba")
async def select_lang_ba(event: MessageCallback, context: MemoryContext):
    await set_user_lang(context, "ba")
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("welcome", current_lang),
        attachments=[main_menu_kb(current_lang)],
    )


# Начало обращения
@dp.message_callback(F.callback.payload == "start_report")
async def start_report(event: MessageCallback, context: MemoryContext):
    current_lang = await get_user_lang(context)
    user = event.callback.user

    # Сохраняем данные пользователя
    report_number = get_next_report_number()
    await context.update_data(
        user_id=user.user_id,
        user_chat_id=event.message.recipient.chat_id,
        user_full_name=user.full_name,
        user_username=user.username,
        report_number=report_number,
        photos=[],
    )

    # Убираем клавиатуру у предыдущего сообщения и просим текст
    await event.edit(
        text=get_text("welcome", current_lang),
        attachments=[],
    )
    await event.send(get_text("ask_text", current_lang))
    await context.set_state(ReportStates.waiting_for_text)


# ------ Шаг 1: Ожидаем текст обращения ------
@dp.message_created()
async def handle_states_message(event: MessageCreated, context: MemoryContext):
    """Единый обработчик текстовых сообщений в зависимости от состояния."""
    message = event.message
    message_chat_id = message.recipient.chat_id if message.recipient else None

    # Если сообщение пришло из группы админов — проверяем, не является ли оно ответом на обращение
    if message_chat_id == ADMIN_USER_ID:
        body = message.body
        if not body:
            return

        raw_text = body.text or ""

        # Срабатываем только если бот упомянут в тексте
        bot_username = bot.me.username if bot.me else None
        if not bot_username or f"@{bot_username}" not in raw_text:
            return

        # Сообщение должно быть ответом на обращение
        if not message.link or message.link.type.value != "reply":
            logger.warning("Упоминание бота в группе без цитирования обращения — игнорируем")
            return

        linked_body = message.link.message
        replied_text = linked_body.text if linked_body else ""
        match_chat = re.search(r"Chat ID:\s*(-?\d+)", replied_text)
        match_number = re.search(r"Обращение #(\d+)", replied_text)
        if not match_chat:
            logger.warning("Ответ от админа: Chat ID не найден в тексте цитаты: %r", replied_text)
            return
        user_chat_id = int(match_chat.group(1))
        report_number = match_number.group(1) if match_number else "?"

        # Убираем упоминание бота из текста ответа
        reply_text = raw_text.replace(f"@{bot_username}", "").strip() or None

        photo_tokens = extract_photo_tokens(message)
        attachments = photo_tokens_to_attachments(photo_tokens)

        if reply_text or attachments:
            full_reply = f"Ответ по обращению #{report_number}:\n{reply_text}" if reply_text else f"Ответ по обращению #{report_number}"
            try:
                await bot.send_message(
                    chat_id=user_chat_id,
                    text=full_reply,
                    attachments=attachments or None,
                )
                logger.info("Ответ администратора отправлен пользователю (chat_id=%s)", user_chat_id)
            except Exception as e:
                logger.error(
                    "Не удалось отправить ответ администратора пользователю (chat_id=%s): %s",
                    user_chat_id,
                    e,
                )
        return

    state = await context.get_state()
    current_lang = await get_user_lang(context)

    body = message.body
    text = body.text if body else None
    photo_tokens = extract_photo_tokens(message)

    # --- Состояние: ожидание текста ---
    if state == ReportStates.waiting_for_text:
        if not text:
            await event.message.answer(get_text("not_text_error", current_lang))
            return

        await context.update_data(report_text=text)
        await event.message.answer(
            get_text("ask_photo", current_lang),
            attachments=[photo_decision_kb(current_lang)],
        )
        await context.set_state(ReportStates.waiting_for_photo_decision)
        return

    # --- Состояние: решение о фото (ждём нажатия кнопки) ---
    if state == ReportStates.waiting_for_photo_decision:
        await event.message.answer(
            get_text("ask_photo", current_lang),
            attachments=[photo_decision_kb(current_lang)],
        )
        return

    # --- Состояние: прикрепление фото ---
    if state == ReportStates.waiting_for_photos:
        if not photo_tokens:
            await event.message.answer(get_text("not_photo_error", current_lang))
            return

        data = await context.get_data()
        photos = data.get("photos", [])

        for token in photo_tokens:
            if len(photos) >= 3:
                await event.message.answer(get_text("max_photos", current_lang))
                break
            photos.append(token)

        await context.update_data(photos=photos)
        await event.message.answer(
            get_text("photo_added", current_lang).format(count=len(photos))
        )
        return


# ------ Шаг 2: Решение — прикреплять фото или нет ------
@dp.message_callback(F.callback.payload == "photo_yes")
async def photo_decision_yes(event: MessageCallback, context: MemoryContext):
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("ask_photo", current_lang),
        attachments=[],
    )
    await event.send(
        get_text("ask_photo_attach", current_lang),
        attachments=[done_kb(current_lang)],
    )
    await context.set_state(ReportStates.waiting_for_photos)


@dp.message_callback(F.callback.payload == "photo_no")
async def photo_decision_no(event: MessageCallback, context: MemoryContext):
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("ask_photo", current_lang),
        attachments=[],
    )
    await send_report(context)


# ------ Шаг 3: Завершение прикрепления фото ------
@dp.message_callback(F.callback.payload == "done_photos")
async def done_photos(event: MessageCallback, context: MemoryContext):
    current_lang = await get_user_lang(context)
    await event.edit(
        text=get_text("ask_photo_attach", current_lang),
        attachments=[],
    )
    await send_report(context)


# Возврат в главное меню
@dp.message_callback(F.callback.payload == "return_to_main")
async def return_to_main(event: MessageCallback, context: MemoryContext):
    data = await context.get_data()
    current_lang = data.get("lang", "ru")  # восстанавливаем язык
    await context.clear()
    await context.update_data(lang=current_lang)

    await event.edit(
        text=get_text("welcome", current_lang),
        attachments=[main_menu_kb(current_lang)],
    )


# ---------------------------
#   ОТПРАВКА ОБРАЩЕНИЯ (БОТ -> АДМИН-ЧАТ)
# ---------------------------
async def send_report(context: MemoryContext):
    data = await context.get_data()
    current_lang = data.get("lang", "ru")
    report_text = data.get("report_text", "Нет текста")
    photos = data.get("photos", [])
    user_id = data.get("user_id")
    user_chat_id = data.get("user_chat_id")
    user_full_name = data.get("user_full_name")
    user_username = data.get("user_username")

    report_number = data.get("report_number", "?")

    # Текст для админ-чата
    text_to_admin = (
        f"Обращение #{report_number}\n"
        f"Имя пользователя: {user_full_name}\n"
        f"Логин MAX: @{user_username or 'Нет'}\n"
        f"ID аккаунта MAX: {user_id}\n"
        f"Chat ID: {user_chat_id}\n\n"
        f"Текст обращения: {report_text}"
    )

    # Формируем вложения с фото (переиспользуем токены)
    attachments = photo_tokens_to_attachments(photos)

    # Отправляем в группу админов
    try:
        await bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=text_to_admin,
            attachments=attachments or None,
        )
    except Exception as e:
        logger.error(
            "Не удалось отправить обращение в группу админов (chat_id=%s): %s",
            ADMIN_USER_ID,
            e,
        )

    # Сообщаем пользователю, что обращение принято
    if user_chat_id is not None:
        try:
            await bot.send_message(
                chat_id=user_chat_id,
                text=get_text("thanks_report", current_lang).format(number=report_number),
                attachments=[return_to_main_kb(current_lang)],
            )
        except Exception as e:
            logger.error(
                "Не удалось отправить подтверждение пользователю (chat_id=%s): %s",
                user_chat_id,
                e,
            )

    # Очищаем данные, но сохраняем язык
    await context.clear()
    await context.update_data(lang=current_lang)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())