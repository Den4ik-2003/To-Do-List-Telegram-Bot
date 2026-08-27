import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from keyboards.main_menu import CATEGORY_CREATIVE, kb_category, kb_main, BACK_TO_MAIN
from keyboards.creative_kb import (
    FORMATS, STYLE_PROMPTS, TEMPLATES,
    kb_formats, kb_styles, kb_templates, kb_regenerate,
)
from services.image_service import generate_image, edit_image, ImageServiceError
from services.sticker_service import prepare_sticker, prepare_emoji, crop_to_aspect
from database.creative import save_generation, get_user_generations, check_daily_limit

logger = logging.getLogger("tasks_bot")
router = Router(name="creative_studio")


class CreativeStates(StatesGroup):
    waiting_prompt = State()
    waiting_format = State()
    waiting_style = State()
    waiting_edit_photo = State()
    waiting_edit_instruction = State()
    waiting_template_prompt = State()


TEMPLATE_PROMPT_PREFIX = {
    "avatar": "Profile avatar portrait, centered, clean background:",
    "cover": "Wide banner/cover image:",
    "ig_post": "Instagram square post, eye-catching composition:",
    "ig_story": "Instagram story, vertical 9:16 composition:",
    "thumbnail": "YouTube thumbnail, bold, high contrast, readable at small size:",
    "meme": "Meme image, funny, exaggerated expressions:",
    "product": "Professional product photography, studio lighting, clean background:",
    "sticker": "Sticker illustration, bold outline, simple background:",
    "emoji": "Small expressive emoji icon, simple shape, single subject:",
    "holiday": "Festive holiday-themed image, warm colors:",
}


# ---------- Головне меню розділу ----------

@router.message(F.text == "🖼️ Згенерувати картинку")
async def start_generate(message: Message, state: FSMContext):
    if not await check_daily_limit(message.from_user.id):
        await message.answer("⛔ Денний ліміт генерацій вичерпано. Спробуй завтра.")
        return
    await state.set_state(CreativeStates.waiting_format)
    await state.update_data(mode="generate")
    await message.answer("Обери формат картинки:", reply_markup=kb_formats())


@router.message(F.text == "😀 Стікер")
async def start_sticker(message: Message, state: FSMContext):
    if not await check_daily_limit(message.from_user.id):
        await message.answer("⛔ Денний ліміт генерацій вичерпано. Спробуй завтра.")
        return
    await state.set_state(CreativeStates.waiting_prompt)
    await state.update_data(mode="sticker")
    await message.answer("Опиши стікер, наприклад: «кіт каже Давай!»")


@router.message(F.text == "😎 AI Emoji")
async def start_emoji(message: Message, state: FSMContext):
    if not await check_daily_limit(message.from_user.id):
        await message.answer("⛔ Денний ліміт генерацій вичерпано. Спробуй завтра.")
        return
    await state.set_state(CreativeStates.waiting_prompt)
    await state.update_data(mode="emoji")
    await message.answer("Опиши emoji, наприклад: «здивований смайл 🤯»")


@router.message(F.text == "🤖 Редагувати фото")
@router.message(F.text == "🎨 З мого фото")
async def start_edit(message: Message, state: FSMContext):
    if not await check_daily_limit(message.from_user.id):
        await message.answer("⛔ Денний ліміт генерацій вичерпано. Спробуй завтра.")
        return
    await state.set_state(CreativeStates.waiting_edit_photo)
    await message.answer("Надішли фото, яке треба відредагувати/використати як референс.")


@router.message(F.text == "📱 Шаблони")
async def start_templates(message: Message):
    await message.answer("Обери шаблон:", reply_markup=kb_templates())


@router.message(F.text == "🗂️ Мої генерації")
async def show_history(message: Message):
    items = await get_user_generations(message.from_user.id, limit=10)
    if not items:
        await message.answer("Поки що немає збережених генерацій.")
        return
    for item in items:
        caption = f"{item['kind']} · {item['prompt'][:150]}"
        await message.answer_photo(photo=item["file_id"], caption=caption)


# ---------- Формат / стиль (для generate) ----------

@router.callback_query(CreativeStates.waiting_format, F.data.startswith("cs_fmt:"))
async def choose_format(callback: CallbackQuery, state: FSMContext):
    fmt = callback.data.split(":", 1)[1]
    await state.update_data(fmt=fmt)
    await state.set_state(CreativeStates.waiting_style)
    await callback.message.edit_text("Обери стиль:", reply_markup=kb_styles())
    await callback.answer()


@router.callback_query(CreativeStates.waiting_style, F.data.startswith("cs_style:"))
async def choose_style(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":", 1)[1]
    await state.update_data(style=style)
    await state.set_state(CreativeStates.waiting_prompt)
    await callback.message.edit_text("Тепер опиши, що згенерувати:")
    await callback.answer()


# ---------- Шаблони ----------

@router.callback_query(F.data.startswith("cs_tpl:"))
async def choose_template(callback: CallbackQuery, state: FSMContext):
    tpl = callback.data.split(":", 1)[1]
    await state.update_data(mode="template", template=tpl)
    await state.set_state(CreativeStates.waiting_template_prompt)
    await callback.message.edit_text(
        f"{TEMPLATES[tpl]}. Опиши деталі (кого/що зобразити):"
    )
    await callback.answer()


@router.message(CreativeStates.waiting_template_prompt)
async def handle_template_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    tpl = data["template"]
    prefix = TEMPLATE_PROMPT_PREFIX.get(tpl, "")
    full_prompt = f"{prefix} {message.text}".strip()

    is_sticker = tpl in ("sticker", "emoji")
    size = "1024x1024"
    await _run_generation(
        message, state,
        prompt=full_prompt,
        size=size,
        transparent=is_sticker,
        kind="template",
        post_process="sticker" if tpl == "sticker" else ("emoji" if tpl == "emoji" else None),
    )
    await state.clear()


# ---------- Основний текстовий промпт (generate / sticker / emoji) ----------

@router.message(CreativeStates.waiting_prompt)
async def handle_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", "generate")

    if mode == "generate":
        fmt = data.get("fmt", "1:1")
        style = data.get("style")
        w, h = FORMATS[fmt]
        style_suffix = f", {STYLE_PROMPTS[style]}" if style and style != "none" else ""
        prompt = f"{message.text}{style_suffix}"
        size = f"{w}x{h}" if fmt != "4:5" else "1024x1536"
        await _run_generation(
            message, state, prompt=prompt, size=size,
            transparent=False, kind="generate",
            post_process="crop_4_5" if fmt == "4:5" else None,
        )
    elif mode in ("sticker", "emoji"):
        prompt = f"{message.text}, simple background, bold clean shapes, sticker/emoji style"
        await _run_generation(
            message, state, prompt=prompt, size="1024x1024",
            transparent=True, kind=mode, post_process=mode,
        )

    await state.clear()


# ---------- Редагування фото ----------

@router.message(CreativeStates.waiting_edit_photo, F.photo)
async def receive_edit_photo(message: Message, state: FSMContext, bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    await state.update_data(source_image=buf.read())
    await state.set_state(CreativeStates.waiting_edit_instruction)
    await message.answer("Що зробити з фото? Наприклад: «прибери фон» або «додай напис SALE».")


@router.message(CreativeStates.waiting_edit_photo)
async def receive_edit_photo_wrong(message: Message):
    await message.answer("Надішли саме фото 🙂")


@router.message(CreativeStates.waiting_edit_instruction)
async def handle_edit_instruction(message: Message, state: FSMContext):
    data = await state.get_data()
    image_bytes = data["source_image"]

    await message.answer("🎨 Редагую зображення, це займе трохи часу...")
    try:
        result = await edit_image(image_bytes, message.text, size="1024x1024")
    except ImageServiceError as e:
        await message.answer(f"❌ Не вдалося відредагувати: {e}")
        await state.clear()
        return

    sent = await message.answer_photo(
        photo=BufferedInputFile(result, filename="edited.png"),
        caption="Готово! Результат редагування.",
    )
    file_id = sent.photo[-1].file_id
    gen_id = await save_generation(message.from_user.id, "edit", message.text, file_id)
    await sent.reply("Зберегти чи перегенерувати?", reply_markup=kb_regenerate(gen_id))
    await state.clear()


# ---------- Спільна логіка генерації + постобробки ----------

async def _run_generation(
    message: Message,
    state: FSMContext,
    prompt: str,
    size: str,
    transparent: bool,
    kind: str,
    post_process: str | None = None,
):
    await message.answer("🎨 Генерую, зачекай ~10-30 секунд...")
    try:
        result = await generate_image(prompt, size=size, transparent=transparent)
    except ImageServiceError as e:
        await message.answer(f"❌ Не вдалося згенерувати: {e}")
        return

    if post_process == "sticker":
        result = prepare_sticker(result)
    elif post_process == "emoji":
        result = prepare_emoji(result)
    elif post_process == "crop_4_5":
        result = crop_to_aspect(result, 4, 5)

    sent = await message.answer_photo(
        photo=BufferedInputFile(result, filename="result.png"),
        caption="Готово! Тисни 🔄, якщо хочеш інший варіант.",
    )
    file_id = sent.photo[-1].file_id
    gen_id = await save_generation(message.from_user.id, kind, prompt, file_id, meta={"size": size})
    await sent.reply("", reply_markup=kb_regenerate(gen_id))


@router.callback_query(F.data.startswith("cs_regen:"))
async def regenerate(callback: CallbackQuery):
    await callback.message.answer("Опиши, що згенерувати цього разу:")
    await callback.answer()


@router.callback_query(F.data.startswith("cs_save:"))
async def save_confirm(callback: CallbackQuery):
    await callback.answer("Вже в 🗂️ Мої генерації", show_alert=False)