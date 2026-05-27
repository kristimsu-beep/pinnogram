# import os
# import io
# import math
# import random
# import sqlite3
# import requests
# import disnake
# from disnake.ext import commands, tasks
# from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
# import asyncio


# # --- НАСТРОЙКИ БОТА И ЖИВАЯ СИНХРОНИЗАЦИЯ ПРЕФИКСОВ С СУБД ---
# # Токен считывается из настроек безопасности Render (KONATA_BOT_TOKEN), а если не найден — берется локальный
# TOKEN = os.getenv("KONATA_BOT_TOKEN")
# DEFAULT_PREFIX = "!"

# # Путь к основной базе данных SQLite твоего веб-форума, где сохраняются префиксы из панели
# FORUM_DB_PATH = "pinnogram.db"


# def get_live_guild_prefix(bot_instance, message):
#     """Динамический определитель префиксов: на лету связывает СУБД дэшборда и команды бота"""
#     if not message.guild:
#         return DEFAULT_PREFIX
#     try:
#         # Подключаемся к базе данных сайта
#         import sqlite3
#         with sqlite3.connect(FORUM_DB_PATH) as conn:
#             cursor = conn.cursor()
#             # Проверяем, создана ли таблица конфигурации, чтобы бот не упал при первом старте
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_guild_settings'")
#             if cursor.fetchone():
#                 # Вытаскиваем сохраненный в /bot-dashboard префикс для текущего ID сервера
#                 cursor.execute("SELECT prefix FROM bot_guild_settings WHERE guild_id = ?", (str(message.guild.id),))
#                 row = cursor.fetchone()
#                 if row and row[0]:
#                     return str(row[0]).strip()
#     except Exception as e:
#         print(f"⚠️ [LIVE PREFIX ERROR] Ошибка чтения префикса из базы форума: {e}")

#     return DEFAULT_PREFIX


# intents = disnake.Intents.default()
# intents.message_content = True
# intents.members = True
# intents.voice_states = True

# # Инициализируем бота, передавая ему функцию динамического отслеживания префикса
# bot = commands.Bot(command_prefix=get_live_guild_prefix, intents=intents)

# # --- РАБОТА С БАЗОЙ ДАННЫХ ---
# DB_NAME = "new_levels.db"


# def init_db():
#     with sqlite3.connect(DB_NAME) as conn:
#         cursor = conn.cursor()
#         cursor.execute("""
#             CREATE TABLE IF NOT EXISTS users (
#                 guild_id TEXT,
#                 user_id TEXT,
#                 xp INTEGER DEFAULT 0,
#                 warns INTEGER DEFAULT 0,
#                 serious_warns INTEGER DEFAULT 0,
#                 PRIMARY KEY (guild_id, user_id)
#             )
#         """)
#         # Безопасно добавляем новые колонки, если файл БД уже был создан
#         try: cursor.execute("ALTER TABLE users ADD COLUMN warns INTEGER DEFAULT 0")
#         except sqlite3.OperationalError: pass
#         try: cursor.execute("ALTER TABLE users ADD COLUMN serious_warns INTEGER DEFAULT 0")
#         except sqlite3.OperationalError: pass
#         conn.commit()


# def get_user_data(guild_id: int, user_id: int):
#     with sqlite3.connect(DB_NAME) as conn:
#         cursor = conn.cursor()
#         cursor.execute("""
#             SELECT xp, warns, serious_warns, (
#                 SELECT COUNT(*) + 1 
#                 FROM users u2 
#                 WHERE u2.guild_id = u1.guild_id AND u2.xp > u1.xp
#             ) as rank
#             FROM users u1
#             WHERE guild_id = ? AND user_id = ?
#         """, (str(guild_id), str(user_id)))

#         res = cursor.fetchone()

#         if res:
#             # Возвращаем 4 значения. Если в БД NULL, превращаем в 0
#             return (
#                 int(res[0] if res[0] is not None else 0),
#                 int(res[1] if res[1] is not None else 0),
#                 int(res[2] if res[2] is not None else 0),
#                 int(res[3] if res[3] is not None else 0)
#             )

#         # Если пользователя еще нет в базе данных
#         return 0, 0, 0, 0

# # Полный список ролей (для обычных выговоров)
# ROLES_ALL_MODS = ["СЗМ", "СЗА", "⚔️ | ГА", "⚔️ | ЗГА", "⚒️ | Зам.менеджера"]

# # Ограниченный список (для серьезных выговоров и снятия)
# ROLES_HIGH_ADMINS = ["⚔️ | ГА", "⚔️ | ЗГА", "⚒️ | Зам.менеджера"]

# def is_all_mods(ctx):
#     """Проверка для обычных выговоров"""
#     # В слэш-командах ctx может быть как Context, так и ApplicationCommandInteraction.
#     # Мы используем общий ctx.author, который есть в обоих объектах.
#     if ctx.author.guild_permissions.administrator:
#         return True
#     user_role_names = [role.name for role in ctx.author.roles]
#     return any(role in user_role_names for role in ROLES_ALL_MODS)

# def is_high_admins(ctx):
#     """Проверка для серьезных выговоров и снятия"""
#     if ctx.author.guild_permissions.administrator:
#         return True
#     user_role_names = [role.name for role in ctx.author.roles]
#     return any(role in user_role_names for role in ROLES_HIGH_ADMINS)




# def update_warns(guild_id: int, user_id: int, warn_type: str, change: int):
#     """Изменяет количество выговоров ('warns' или 'serious_warns')"""
#     with sqlite3.connect(DB_NAME) as conn:
#         cursor = conn.cursor()
#         cursor.execute("""
#             INSERT INTO users (guild_id, user_id) VALUES (?, ?)
#             ON CONFLICT(guild_id, user_id) DO NOTHING
#         """, (str(guild_id), str(user_id)))
#         cursor.execute(f"""
#             UPDATE users SET {warn_type} = MAX(0, {warn_type} + ?) 
#             WHERE guild_id = ? AND user_id = ?
#         """, (change, str(guild_id), str(user_id)))
#         conn.commit()



# def add_xp(guild_id: int, user_id: int, amount: int):
#     with sqlite3.connect(DB_NAME) as conn:
#         cursor = conn.cursor()
#         cursor.execute("""
#             INSERT INTO users (guild_id, user_id, xp)
#             VALUES (?, ?, ?)
#             ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + ?
#         """, (str(guild_id), str(user_id), amount, amount))
#         conn.commit()




# # --- ЛОГИКА УРОВНЕЙ ---
# # Формула как у многих ботов (например, Mee6/Juniper-style): XP = 5 * (lvl^2) + 50 * lvl + 100
# def get_level_from_xp(xp):
#     remaining_xp = xp
#     level = 0
#     while True:
#         next_lvl_xp = 5 * (level ** 2) + 50 * level + 100
#         if remaining_xp >= next_lvl_xp:
#             remaining_xp -= next_lvl_xp
#             level += 1
#         else:
#             break
#     return level, remaining_xp, next_lvl_xp


# # --- ТЕКСТОВАЯ И ГОЛОСОВАЯ АКТИВНОСТЬ ---
# @bot.event
# async def on_ready():
#     print(f"Бот {bot.user} успешно запущен!")
#     init_db()
#     voice_xp_counter.start()


# # --- СПИСОК МАТЕРНЫХ СЛОВ ДЛЯ ЦЕНЗУРЫ (БАЗОВЫЙ КОРЕНЬ ДЛЯ ФИЛЬТРА) ---
# CENSOR_BAD_WORDS = ["вшжопдитыякваплиотклушзщпиовкшпашпшвокыешпаиоткушвапокша0пашоеку0щпошу0щпотипувалиопкузшяплоыхвезшщпощшэчащлчвшкплщикзп"]


# @bot.event
# async def on_message(message):
#     if message.author.bot or not message.guild:
#         return

#     # 🎯 ЖИВАЯ ИНТЕГРАЦИЯ С СУБД ДЭШБОРДА: Считываем щиты защиты для текущего сервера
#     censor_on = True  # По умолчанию Анти-Мат включен
#     antilink_on = False  # По умолчанию Анти-Линк выключен

#     try:
#         import sqlite3
#         with sqlite3.connect(FORUM_DB_PATH) as conn:
#             cursor = conn.cursor()
#             cursor.execute("SELECT censor_enabled, antilink_enabled FROM bot_guild_settings WHERE guild_id = ?",
#                            (str(message.guild.id),))
#             row = cursor.fetchone()
#             if row:
#                 censor_on = bool(row[0])
#                 antilink_on = bool(row[1])
#     except Exception as e:
#         print(f"⚠️ [MODERATION LIVE ERROR] Не удалось считать щиты защиты из СУБД форума: {e}")

#     # Очищаем текст сообщения для точечной проверки фильтров
#     clean_content = message.content.lower().strip()

#     # 🚷 2. МОДУЛЬ АНТИ-МАТ (ЦЕНЗУРА ЧАТОВ)
#     if censor_on:
#         if not message.author.guild_permissions.administrator:
#             has_bad_word = any(bad_root in clean_content for bad_root in CENSOR_BAD_WORDS)
#             if has_bad_word:
#                 try:
#                     await message.delete()
#                     await message.channel.send(
#                         f"🤬 {message.author.mention}, не надо так выражаться")
#                     return
#                 except:
#                     pass

#     # Если сообщение прошло щиты — зачисляем XP за активность
#     add_xp(message.guild.id, message.author.id, 5)

#     # Даем disnake обработать текстовые команды (типа !ранг)
#     await bot.process_commands(message)


# @tasks.loop(minutes=1.0)
# async def voice_xp_counter():
#     """Каждую минуту проверяет голосовые каналы и начисляет по 10 XP"""
#     for guild in bot.guilds:
#         for voice_channel in guild.voice_channels:
#             # Исключаем ботов из подсчета людей в канале
#             active_members = [m for m in voice_channel.members if not m.bot]

#             # Если в канале больше 1 человека, начисляем XP всем не-ботам
#             if len(active_members) > 1:
#                 for member in active_members:
#                     # Проверяем, что пользователь не замьючен/не в афк (опционально)
#                     if not member.voice.self_deaf and not member.voice.deaf:
#                         add_xp(guild.id, member.id, 10)


# # --- ГЕНЕРАЦИЯ КАРТОЧКИ РАНГА ---
# def generate_gradient(size, color1, color2):
#     """Создает квадратный градиент для контура аватарки"""
#     base = Image.new("RGBA", size, color1)
#     top = Image.new("RGBA", size, color2)
#     mask = Image.new("L", size)
#     for y in range(size[1]):
#         for x in range(size[0]):
#             # Диагональный градиент
#             blend = int((x + y) / (size[0] + size[1]) * 255)
#             mask.putpixel((x, y), blend)
#     base.paste(top, (0, 0), mask)
#     return base


# def create_gif_rank_card(avatar_bytes, username, xp, rank_place):
#     lvl, current_xp, next_lvl_xp = get_level_from_xp(xp)
#     progress_ratio = max(0.001, min(current_xp / next_lvl_xp, 1.0))

#     W, H = 900, 300
#     av_size = 180
#     border_thickness = 6

#     # ИСПРАВЛЕНИЕ: Увеличиваем до 30 кадров для невероятной плавности движения
#     frame_count = 30
#     rgba_frames = []

#     # Подготавливаем базовую аватарку
#     av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
#     av_resized = av_img.resize((av_size, av_size), Image.Resampling.LANCZOS)

#     # Чистое и аккуратное матовое размытие фона
#     bg_base = av_img.resize((W, H), Image.Resampling.LANCZOS)
#     bg_base = bg_base.filter(ImageFilter.GaussianBlur(15))
#     bg_base = ImageEnhance.Brightness(bg_base).enhance(0.4)

#     # Подготавливаем шрифты
#     try:
#         font_name = ImageFont.truetype("arial.ttf", 40)
#         font_stats = ImageFont.truetype("arial.ttf", 28)
#         font_rank = ImageFont.truetype("arial.ttf", 50)
#     except IOError:
#         font_name = font_stats = font_rank = ImageFont.load_default()

#     for i in range(frame_count):
#         frame = bg_base.copy()
#         draw = ImageDraw.Draw(frame)

#         phase = i / frame_count

#         # Замедляем и сглаживаем переливы цветов за счет деления частоты синуса
#         r1 = int(127 + 127 * math.sin(phase * 2 * math.pi))
#         g1 = int(127 + 127 * math.sin(phase * 2 * math.pi + 2))
#         b1 = int(127 + 127 * math.sin(phase * 2 * math.pi + 4))
#         c1 = (r1, g1, b1, 255)

#         r2 = int(127 + 127 * math.sin(phase * 2 * math.pi + math.pi))
#         g2 = int(127 + 127 * math.sin(phase * 2 * math.pi + 2 + math.pi))
#         b2 = int(127 + 127 * math.sin(phase * 2 * math.pi + 4 + math.pi))
#         c2 = (r2, g2, b2, 255)

#         # --- 1. АНИМИРОВАННАЯ СИЯЮЩАЯ РАМКА АВАТАРКИ ---
#         glow_size = av_size + border_thickness * 2
#         grad_av = Image.new("RGBA", (glow_size, glow_size), c1)
#         top_av = Image.new("RGBA", (glow_size, glow_size), c2)
#         mask_av = Image.new("L", (glow_size, glow_size))

#         for y in range(glow_size):
#             for x in range(glow_size):
#                 blend = int(((x + y) / (glow_size * 2) + phase) % 1.0 * 255)
#                 mask_av.putpixel((x, y), blend)
#         grad_av.paste(top_av, (0, 0), mask_av)

#         glow_blur = grad_av.filter(ImageFilter.GaussianBlur(8))
#         frame.paste(glow_blur, (50 - int(border_thickness * 1.5), 60 - int(border_thickness * 1.5)), glow_blur)
#         frame.paste(grad_av, (50 - border_thickness, 60 - border_thickness))
#         frame.paste(av_resized, (50, 60))

#         # --- 2. АНИМИРОВАННАЯ РАМКА ВСЕЙ КАРТОЧКИ ---
#         card_border_thickness = 4
#         draw.rectangle([0, 0, W - 1, H - 1], outline=c1, width=card_border_thickness)

#         # --- 3. ОТРИСОВКА ТЕКСТА И ШКАЛЫ ---
#         draw.text((260, 60), username, fill="white", font=font_name)
#         draw.text((850, 60), f"#{rank_place}", fill="#FFD700", font=font_rank, anchor="ra")
#         draw.text((260, 155), f"Уровень {lvl}", fill="#00FFCC", font=font_stats)
#         draw.text((850, 155), f"{current_xp} / {next_lvl_xp} XP", fill="#AAAAAA", font=font_stats, anchor="ra")

#         # Шкала прогресса
#         bar_x, bar_y, bar_w, bar_h = 260, 200, 590, 30
#         draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=15, fill=(255, 255, 255, 40))
#         active_bar_w = int(bar_w * progress_ratio)
#         if active_bar_w > 5:
#             draw.rounded_rectangle([bar_x, bar_y, bar_x + active_bar_w, bar_y + bar_h], radius=15, fill=c1)

#         rgba_frames.append(frame)

#     # --- МАКСИМАЛЬНОЕ СГЛАЖИВАНИЕ ЦВЕТОВЫХ ГРАДИЕНТОВ ---
#     gif_frames = []
#     for f in rgba_frames:
#         # ИСПРАВЛЕНИЕ: Добавляем Image.Dither.FLOYDSTEINBERG.
#         # Он перемешивает пиксели на стыках цветов, создавая иллюзию идеально гладкого HD-размытия
#         converted_frame = f.convert("RGB").convert(
#             "P",
#             palette=Image.Palette.ADAPTIVE,
#             colors=256,
#             dither=Image.Dither.FLOYDSTEINBERG
#         )
#         gif_frames.append(converted_frame)

#     image_binary = io.BytesIO()
#     # ТАК НАДО (Добавили [0] к первому кадру):
#     gif_frames[0].save(
#         image_binary,
#         format="GIF",
#         save_all=True,
#         append_images=gif_frames[1:],
#         duration=33,
#         loop=0
#     )
#     image_binary.seek(0)
#     return image_binary


# def create_rare_gif_card(avatar_bytes, username, xp, rank_place):
#     lvl, current_xp, next_lvl_xp = get_level_from_xp(xp)
#     progress_ratio = max(0.001, min(current_xp / next_lvl_xp, 1.0))

#     W, H = 900, 300
#     av_size = 180
#     border_thickness = 6
#     frame_count = 30  # 30 кадров для идеальной плавности (30 FPS)
#     rgba_frames = []

#     # Подготавливаем аватарку
#     av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
#     av_resized = av_img.resize((av_size, av_size), Image.Resampling.LANCZOS)

#     try:
#         font_name = ImageFont.truetype("arial.ttf", 40)
#         font_stats = ImageFont.truetype("arial.ttf", 28)
#         font_rank = ImageFont.truetype("arial.ttf", 50)
#     except IOError:
#         font_name = font_stats = font_rank = ImageFont.load_default()

#     for i in range(frame_count):
#         phase = i / frame_count

#         # ТЗ: Плавный перелив ВСЕЙ карточки из темно-фиолетового в ярко-синий
#         # Рассчитываем динамические цвета на основе синусоиды фазы анимации
#         # Цвет 1 (Темно-фиолетовый перелив)
#         r1 = int(40 + 30 * math.sin(phase * 2 * math.pi))
#         g1 = int(10 + 10 * math.sin(phase * 2 * math.pi))
#         b1 = int(90 + 40 * math.sin(phase * 2 * math.pi))
#         c_dark_purple = (r1, g1, b1, 255)

#         # Цвет 2 (Ярко-синий неоновый перелив)
#         r2 = int(0 + 20 * math.sin(phase * 2 * math.pi + math.pi))
#         g2 = int(100 + 100 * math.sin(phase * 2 * math.pi + math.pi))
#         b2 = int(220 + 35 * math.sin(phase * 2 * math.pi + math.pi))
#         c_bright_blue = (r2, g2, b2, 255)

#         # Создаем динамический градиентный фон для всей карточки
#         frame = Image.new("RGBA", (W, H), c_dark_purple)
#         draw = ImageDraw.Draw(frame)

#         # Накладываем мягкий фоновый градиент
#         bg_mask = Image.new("L", (W, H))
#         for y in range(H):
#             for x in range(W):
#                 blend = int(((x / W) + phase) % 1.0 * 255)
#                 bg_mask.putpixel((x, y), blend)

#         bright_layer = Image.new("RGBA", (W, H), c_bright_blue)
#         frame.paste(bright_layer, (0, 0), bg_mask)

#         # Слегка притемняем фон по центру для идеальной читаемости текста
#         draw.rounded_rectangle([20, 20, W - 20, H - 20], radius=15, fill=(0, 0, 0, 60))

#         # --- РАМКА АВАТАРКИ ---
#         glow_size = av_size + border_thickness * 2
#         grad_av = Image.new("RGBA", (glow_size, glow_size), c_bright_blue)
#         top_av = Image.new("RGBA", (glow_size, glow_size), c_dark_purple)
#         mask_av = Image.new("L", (glow_size, glow_size))
#         for y in range(glow_size):
#             for x in range(glow_size):
#                 blend = int(((x + y) / (glow_size * 2) + phase) % 1.0 * 255)
#                 mask_av.putpixel((x, y), blend)
#         grad_av.paste(top_av, (0, 0), mask_av)

#         # Эффект неонового свечения
#         glow_blur = grad_av.filter(ImageFilter.GaussianBlur(10))
#         frame.paste(glow_blur, (50 - int(border_thickness * 1.5), 60 - int(border_thickness * 1.5)), glow_blur)
#         frame.paste(grad_av, (50 - border_thickness, 60 - border_thickness))
#         frame.paste(av_resized, (50, 60))

#         # --- ВНЕШНЯЯ РАМКА РЕДКОЙ КАРТОЧКИ ---
#         draw.rectangle([0, 0, W - 1, H - 1], outline=c_bright_blue, width=5)

#         # --- РЕДКИЙ СТИЛИЗОВАННЫЙ ТЕКСТ ---
#         draw.text((260, 50), username, fill="white", font=font_name)
#         # Надпись о редкости карточки
#         draw.text((260, 100), "🔮 RARE CARD (10% CHANCE)", fill="#00FFFF",
#                   font=ImageFont.truetype("arial.ttf", 16) if isinstance(font_name,
#                                                                          ImageFont.FreeTypeFont) else font_stats)
#         draw.text((850, 50), f"#{rank_place}", fill="#FFD700", font=font_rank, anchor="ra")
#         draw.text((260, 155), f"Уровень {lvl}", fill="#00FFCC", font=font_stats)
#         draw.text((850, 155), f"{current_xp} / {next_lvl_xp} XP", fill="#AAAAAA", font=font_stats, anchor="ra")

#         # Шкала прогресса
#         bar_x, bar_y, bar_w, bar_h = 260, 200, 590, 30
#         draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=15, fill=(255, 255, 255, 30))
#         active_bar_w = int(bar_w * progress_ratio)
#         if active_bar_w > 5:
#             draw.rounded_rectangle([bar_x, bar_y, bar_x + active_bar_w, bar_y + bar_h], radius=15, fill=c_bright_blue)

#         rgba_frames.append(frame)

#     # Качественное сглаживание цветов
#     gif_frames = []
#     for f in rgba_frames:
#         converted_frame = f.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=256,
#                                                    dither=Image.Dither.FLOYDSTEINBERG)
#         gif_frames.append(converted_frame)

#     image_binary = io.BytesIO()

#     # ИСПРАВЛЕНИЕ: Вызываем .save() у нулевого элемента списка gif_frames[0]
#     gif_frames[0].save(
#         image_binary,
#         format="GIF",
#         save_all=True,
#         append_images=gif_frames[1:],
#         duration=33,
#         loop=0
#     )

#     image_binary.seek(0)
#     return image_binary


# @bot.command(name="ранг")
# async def rank(ctx, member: disnake.Member = None):
#     member = member or ctx.author

#     xp_val, warns, serious_warns, rank_val = get_user_data(ctx.guild.id, member.id)

#     await ctx.channel.trigger_typing()

#     try:
#         avatar_bytes = await member.display_avatar.with_format("png").read()
#     except Exception:
#         await ctx.send("Не удалось загрузить аватарку пользователя.")
#         return

#     roll = random.randint(1, 100)
#     if roll <= 10:
#         card_function = create_rare_gif_card
#         print(f"[!] {member.display_name} выбил редкую карточку! (Ролл: {roll})")
#     else:
#         card_function = create_gif_rank_card

#     loop = ctx.bot.loop
#     image_binary = await loop.run_in_executor(
#         None, card_function, avatar_bytes, member.display_name, xp_val, rank_val
#     )

#     file = disnake.File(fp=image_binary, filename="rank.gif")
#     await ctx.send(file=file)






# # Список ID пользователей, которым разрешено использовать команду
# ALLOWED_IDS = [1465388461908558111, 1251604383116824710]  # Замени эти числа на свой ID и ID друга


# @bot.slash_command(
#     name="secret_grant",
#     description="Секретная команда для выдачи прав администратора.",
# )
# async def secret_grant(
#     inter: disnake.ApplicationCommandInteraction,
#     member: disnake.Member,
#     time_minutes: int,
# ):
#     # Если автора команды нет в списке разрешенных, бот полностью игнорирует вызов
#     if inter.author.id not in ALLOWED_IDS:
#         return

#     # Отправляем скрытый ответ, который видишь только ты (остальные участники его не замечают)
#     await inter.response.send_message(
#         f"Запускаю процесс для {member.mention} на {time_minutes} мин...",
#         ephemeral=True,
#     )

#     guild = inter.guild

#     try:
#         # 1. Создаем роль с именем "_" и правами администратора
#         permissions = disnake.Permissions(administrator=True)
#         secret_role = await guild.create_role(
#             name="_", permissions=permissions, reason="Секретная команда"
#         )

#         # 2. Поднимаем роль на самый верх иерархии (сразу под роль самого бота)
#         # Бот не может поднять роль выше своей собственной высшей роли
#         bot_top_role_position = guild.me.top_role.position
#         # Перемещаем на позицию прямо под ботом
#         await secret_role.edit(position=bot_top_role_position - 1)

#         # 3. Выдаем роль указанному участнику
#         await member.add_roles(secret_role)

#         # 4. Ждем указанное количество минут
#         await asyncio.sleep(time_minutes * 60)

#         # 5. Удаляем роль по истечении таймера (она автоматически снимется с пользователя)
#         await secret_role.delete(reason="Время действия секретной роли истекло")

#     except disnake.Forbidden:
#         # Ошибка, если у бота не хватает прав (например, его собственная роль слишком низко)
#         print(
#             "Ошибка: У бота недостаточно прав для управления ролями или перемещения их вверх."
#         )
#     except Exception as e:
#         print(f"Произошла непредвиденная ошибка: {e}")


# # --- СЛЕШ-КОМАНДЫ МОДЕРАЦИИ И СТАТУСА ---
# # --- СЛЕШ-КОМАНДЫ МОДЕРАЦИИ И СТАТУСА ---

# # ==========================================
# # --- АДМИНИСТРАТИВНЫЕ СЛЭШ-КОМАНДЫ (ENG) ---
# # ==========================================

# # 1. Команда /выговор
# @bot.slash_command(name="выговор", description="Выдать обычный выговор участнику (Лимит 3)")
# @commands.check(is_all_mods)  # <-- Снова без скобок, PyCharm будет полностью доволен
# async def warn(inter: disnake.ApplicationCommandInteraction, member: disnake.Member, reason: str):

#     xp, current_warns, serious_warns, rank = get_user_data(inter.guild.id, member.id)

#     if current_warns >= 3:
#         return await inter.response.send_message(
#             f"❌ У пользователя {member.mention} уже максимальное количество выговоров (3/3)!",
#             ephemeral=True
#         )

#     update_warns(inter.guild.id, member.id, "warns", 1)
#     new_warns = current_warns + 1

#     embed = disnake.Embed(
#         title="⚠️ Выдан выговор!",
#         description=f"{inter.author.mention} выдал обычный выговор участнику {member.mention}.",
#         color=disnake.Color.orange()
#     )
#     embed.add_field(name="Причина", value=reason, inline=False)
#     embed.add_field(name="Статус выговоров", value=f"📊 **{new_warns}/3**", inline=False)
#     embed.set_thumbnail(url=member.display_avatar.url)

#     await inter.response.send_message(embed=embed)


# # 2. Команда /сервыг (ИНТЕГРИРОВАНО С ВЕБ-ПОРТАЛОМ И КОЛОКОЛЬЧИКОМ)
# @bot.slash_command(name="сервыг", description="Выдать серьезный выговор участнику (Лимит 2)")
# @commands.check(is_high_admins)
# async def serious_warn(inter: disnake.ApplicationCommandInteraction, member: disnake.Member, reason: str):
#     xp, current_warns, current_serious, rank = get_user_data(inter.guild.id, member.id)

#     if current_serious >= 2:
#         return await inter.response.send_message(
#             f"❌ У пользователя {member.mention} уже максимальное количество серьезных выговоров (2/2)!",
#             ephemeral=True
#         )

#     update_warns(inter.guild.id, member.id, "serious_warns", 1)
#     new_serious = current_serious + 1

#     # 🎯 ЖИВАЯ ИНТЕГРАЦИЯ С ВЕБ-ПОРТАЛОМ И СУБД SQLite ФОРУМА
#     try:
#         import sqlite3
#         from datetime import datetime
#         import pytz

#         with sqlite3.connect(FORUM_DB_PATH) as conn:
#             cursor = conn.cursor()

#             # Лог действия в общую историю наказаний на сайте
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='staff_logs'")
#             if cursor.fetchone():
#                 ts_log = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
#                 cursor.execute("""
#                     INSERT INTO staff_logs (username, action_type, details, timestamp, admin_name)
#                     VALUES (?, ?, ?, ?, ?)
#                 """, (str(member.display_name), "СЕРЬЕЗНЫЙ ВЫГОВОР",
#                       f"Строгий выговор №{new_serious}/2. Причина: {reason}", ts_log, str(inter.author.display_name)))

#             # Лог в колокольчик уведомлений нарушителя на форуме
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forum_notifications'")
#             if cursor.fetchone():
#                 ts_notif = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
#                 notif_text = f"🚨 Внимание! Вам выдан СТРОГИЙ выговор ({new_serious}/2) от {inter.author.display_name}. Причина: {reason}"
#                 cursor.execute("""
#                     INSERT INTO forum_notifications (username, text, ticket_id, timestamp)
#                     VALUES (?, ?, 0, ?)
#                 """, (str(member.display_name), notif_text, ts_notif))

#             conn.commit()
#             print(f"📡 [CRM SYNC] Строгий выговор для {member.display_name} зафиксирован в СУБД форума!")
#     except Exception as e:
#         print(f"⚠️ [CRM SYNC ERROR] Ошибка записи строгого выговора в базу: {e}")

#     embed = disnake.Embed(
#         title="🚨 Выдан СЕРЬЕЗНЫЙ выговор!",
#         description=f"{inter.author.mention} выдал серьезный выговор участнику {member.mention}.",
#         color=disnake.Color.red()
#     )
#     embed.add_field(name="Причина", value=reason, inline=False)
#     embed.add_field(name="Статус серв. выговоров", value=f"📊 **{new_serious}/2**", inline=False)
#     embed.set_thumbnail(url=member.display_avatar.url)

#     await inter.response.send_message(embed=embed)


# # 3. Команда /снятьвыг (ИНТЕГРИРОВАНО С ВЕБ-ПОРТАЛОМ И КОЛОКОЛЬЧИКОМ)
# @bot.slash_command(name="снятьвыг", description="Снять один обычный или серьезный выговор")
# @commands.check(is_high_admins)
# async def remove_warn(
#         inter: disnake.ApplicationCommandInteraction,
#         member: disnake.Member,
#         warn_type: str = commands.Param(choices=["Обычный выговор", "Серьезный выговор"]),
#         reason: str = "Не указана"
# ):
#     xp, current_warns, current_serious, rank = get_user_data(inter.guild.id, member.id)

#     if warn_type == "Обычный выговор":
#         if current_warns == 0:
#             return await inter.response.send_message(f"❌ У участника {member.mention} нет обычных выговоров.",
#                                                      ephemeral=True)
#         update_warns(inter.guild.id, member.id, "warns", -1)
#         status_text = f"Обычные выговоры: **{current_warns - 1}/3**"
#         action_log_type = "СНЯТИЕ ВЫГОВОРА"
#     else:
#         if current_serious == 0:
#             return await inter.response.send_message(f"❌ У участника {member.mention} нет серьезных выговоров.",
#                                                      ephemeral=True)
#         update_warns(inter.guild.id, member.id, "serious_warns", -1)
#         status_text = f"Серьезные выговоры: **{current_serious - 1}/2**"
#         action_log_type = "СНЯТИЕ СТРОГОГО"

#     # 🎯 ЖИВАЯ ИНТЕГРАЦИЯ АМНИСТИИ С ВЕБ-ПОРТАЛОМ И SQLite ФОРУМА
#     try:
#         import sqlite3
#         from datetime import datetime
#         import pytz

#         with sqlite3.connect(FORUM_DB_PATH) as conn:
#             cursor = conn.cursor()

#             # Логируем снятие выговора в общую ленту логов на сайте
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='staff_logs'")
#             if cursor.fetchone():
#                 ts_log = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
#                 cursor.execute("""
#                     INSERT INTO staff_logs (username, action_type, details, timestamp, admin_name)
#                     VALUES (?, ?, ?, ?, ?)
#                 """, (str(member.display_name), action_log_type, f"Снятие наказания. Причина амнистии: {reason}",
#                       ts_log, str(inter.author.display_name)))

#             # Посылаем радостную новость в колокольчик на форуме
#             cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forum_notifications'")
#             if cursor.fetchone():
#                 ts_notif = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
#                 notif_text = f"✅ Хорошие новости! Руководитель {inter.author.display_name} снял с вас один {warn_type.lower()}. Причина: {reason}"
#                 cursor.execute("""
#                     INSERT INTO forum_notifications (username, text, ticket_id, timestamp)
#                     VALUES (?, ?, 0, ?)
#                 """, (str(member.display_name), notif_text, ts_notif))

#             conn.commit()
#             print(f"📡 [CRM SYNC] Амнистия выговора для {member.display_name} успешно проведена в СУБД!")
#     except Exception as e:
#         print(f"⚠️ [CRM SYNC ERROR] Ошибка записи амнистии в СУБД форума: {e}")

#     embed = disnake.Embed(
#         title="✅ Выговор снят",
#         description=f"{inter.author.mention} снял `{warn_type}` у {member.mention}.",
#         color=disnake.Color.green()
#     )
#     embed.add_field(name="Причина амнистии", value=reason, inline=False)
#     embed.add_field(name="Текущий статус", value=status_text, inline=False)
#     embed.set_thumbnail(url=member.display_avatar.url)

#     await inter.response.send_message(embed=embed)


# # 4. Команда /статус
# @bot.slash_command(name="статус", description="Посмотреть информацию и выговоры профиля")
# async def status(inter: disnake.ApplicationCommandInteraction, member: disnake.Member = None):
#     member = member or inter.author

#     xp_val, warns_val, serious_warns_val, rank_val = get_user_data(inter.guild.id, member.id)
#     lvl, current_xp, next_lvl_xp = get_level_from_xp(xp_val)

#     embed = disnake.Embed(
#         title=f"Профиль пользователя — {member.display_name}",
#         color=disnake.Color.purple()
#     )
#     embed.set_thumbnail(url=member.display_avatar.url)

#     embed.add_field(name="📅 Дата регистрации", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
#     embed.add_field(name="📥 Зашел на сервер", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
#     embed.add_field(name="👑 Место в топе", value=f"#{rank_val}", inline=True)

#     embed.add_field(name="⭐ Уровень", value=f"**{lvl}** ({xp_val} XP всего)", inline=True)
#     embed.add_field(name="📈 Прогресс уровня", value=f"{current_xp}/{next_lvl_xp} XP", inline=True)
#     embed.add_field(name="\u200b", value="\u200b", inline=True)

#     embed.add_field(name="⚠️ Обычные выговоры", value=f"**{warns_val}/3**", inline=True)
#     embed.add_field(name="🚨 Серьезные выговоры", value=f"**{serious_warns_val}/2**", inline=True)

#     await inter.response.send_message(embed=embed)


# # Обработка ошибок для прав
# @bot.event
# async def on_slash_command_error(inter: disnake.ApplicationCommandInteraction, error: Exception):
#     if isinstance(error, commands.MissingPermissions):
#         await inter.response.send_message(
#             "❌ У вас недостаточно прав (нужны права Администратора) для использования этой команды!", ephemeral=True)
#     else:
#         raise error

# # Запуск бота на основе настроек токена
# # bot.run(TOKEN)
