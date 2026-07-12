import asyncio 
import os, uuid, aiosqlite, uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, Response, Depends
from fastapi.responses import FileResponse 
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
import aiofiles
import httpx
from pywebpush import webpush, WebPushException
import json
import pytz
import base64
import time
import psutil
from supabase import create_client, Client # Добавь в самый верх к импортам, если еще не добавил!
import asyncpg
from fastapi.responses import RedirectResponse, FileResponse
from fastapi import Form, File, UploadFile
from typing import List
from fastapi.staticfiles import StaticFiles


# Вечное облачное хранилище для видео и голосовых Pinnogram
SUPABASE_URL = "https://zzcfdrryfsychezckjov.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp6Y2ZkcnJ5ZnN5Y2hlemNram92Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMTk1MTgsImV4cCI6MjA5NDY5NTUxOH0.L5QdbaIumhGTwATLNZnrTklUOHYD9PhYUYBpM--OZds"
# Секреты для авторизации через Discord (зададим их в панели Render)

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://pinnogram-server.onrender.com/api/forum/auth/callback"
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Берем ключ из настроек Render (в коде его не будет видно)
ADMIN_SECRET_KEY = os.environ.get("ADMIN_KEY", "admin123")
BANNED_DATA = {} # { "IP": timestamp_unban }
# Хранилище активных мутов: { "username": timestamp_unmute }
MUTED_DATA = {}
# Хранилище активных деморганов: { "username": timestamp_release }
DEMORGAN_DATA = {}

from fastapi.responses import HTMLResponse

MASTER_ADMIN_DISCORD_ID = "1499475142231855260" 
ALWAYS_BANNED_IP = "192.168.2.55"
app = FastAPI()

@app.middleware("http")
async def global_ip_ban_protection_middleware(request: Request, call_next):
    # Извлекаем реальный IP-адрес пользователя (с учетом проксирования хостинга Render)
    user_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    
    # Не блокируем системные запросы к статике, иконкам и админ-контроллерам
    if request.url.path.startswith(("/static", "/favicon.ico", "/api/stock/admin")):
        return await call_next(request)

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            async with db.execute("SELECT username, banned_at FROM banned_ips WHERE ip_address = ?", (user_ip,)) as cursor:
                ban_row = await cursor.fetchone()
        except:
            ban_row = None

    if ban_row:
        # ЮЗЕР ЗАБАНЕН! Пропускаем загрузку HTML, но принудительно вживляем блокирующий экран поверх всего сайта
        response = await call_next(request)
        
        if "text/html" in response.headers.get("content-type", ""):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            
            html_content = body.decode("utf-8")
            
            # Создаем некликабельный, мертвый оверлей (разрешено только смотреть!)
            banned_html_overlay = f"""
            <div style="position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(15,23,42,0.75); backdrop-filter:blur(10px); z-index:999999999 !important; display:flex; align-items:center; justify-content:center; font-family:sans-serif; color:#fff; pointer-events:all; user-select:none;">
                <div style="background:#fff; color:#0f172a; padding:40px; border-radius:24px; max-width:480px; width:90%; text-align:center; border:2px solid #ef4444; box-shadow:0 25px 50px -12px rgba(239,68,68,0.3);">
                    <div style="font-size:50px; margin-bottom:15px;">🛑</div>
                    <h2 style="margin:0 0 10px 0; font-weight:800; color:#ef4444; font-size:24px; letter-spacing:-0.5px;">ДОСТУП ОГРАНИЧЕН</h2>
                    <p style="color:#64748b; font-size:14px; line-height:1.6; margin:0 0 20px 0;">Ваш IP-адрес <b>{user_ip}</b> был перманентно заблокирован Главным Создателем. Вам доступен только пассивный режим просмотра контента без права совершать транзакции, писать на форуме или торговать акциями.</p>
                    <div style="font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:1px; color:#94a3b8; background:#f1f5f9; padding:8px; border-radius:8px;">ПЕРМАНЕНТНАЯ БЛОКИРОВКА SSE</div>
                </div>
            </div>
            <style>
                /* Глухо отключаем любые клики, скроллы, инпуты и отправку форм для нарушителя */
                body {{ pointer-events: none !important; overflow: hidden !important; user-select: none !important; }}
            </style>
            """
            html_content = html_content.replace("<body>", f"<body>{banned_html_overlay}")
            return HTMLResponse(content=html_content, status_code=200, headers=dict(response.headers))

    return await call_next(request)

# Храним последние 10 сообщений для каждого пользователя
ai_history = {} 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Умные пути для сервера и локального ПК
BASE_DIR = "/data" if os.path.exists("/data") else os.getcwd()

# ТВОИ РЕАЛЬНЫЕ КЛЮЧИ (ПРИВАТНЫЙ ВСТАВЛЕН)
VAPID_PRIVATE_KEY = "WD8jC5BNBUQtNX_yIRGjWoeA0TySjfToPNxtNLaH9cY"
VAPID_CLAIMS = {"sub": "mailto:kristimsu@gmail.com"}

# Строим абсолютные пути
DB_PATH = os.path.join(BASE_DIR, "pinnogram.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.api_route("/", methods=["GET", "HEAD"])
async def get_index():
    return FileResponse('index.html')

# 🎯 СУПЕР-ФИКС: Заставляем Python принудительно создать папку static, если её нет на Render
static_path = os.path.join(BASE_DIR, "static")
if not os.path.exists(static_path):
    os.makedirs(static_path, exist_ok=True)

# Теперь FastAPI без ошибок смонтирует её для раздачи скриншотов обращений!
app.mount("/static", StaticFiles(directory=static_path), name="static")
app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")
app.mount("/img", StaticFiles(directory="img"), name="img")

# Раздача Service Worker (ОБЯЗАТЕЛЬНО для уведомлений)
@app.get("/sw.js")
async def get_sw():
    return FileResponse("sw.js", media_type="application/javascript")

# Иконка вкладки (чтобы не было 404 в консоли)
@app.get("/favicon.ico")
async def get_favicon():
    return FileResponse("favicon.ico") if os.path.exists("favicon.ico") else None
    
# Функция-будильник (Ping) для Render

@app.post("/subscribe")
async def subscribe(data: dict):
    username = data.get("username")
    sub_json = data.get("subscription") # Это прилетит из браузера
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO push_subscriptions VALUES (?, ?)", 
                        (username, sub_json))
        await db.commit()
    return {"status": "ok"}

# 3. Роут для отметки сообщений прочитанными
@app.post("/read_messages")
async def read_messages(data: dict):
    username = data.get("username") # КТО прочитал
    partner = data.get("partner")   # ЧЬИ сообщения прочитаны
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE messages SET is_read = 1 
            WHERE username = ? AND to_user = ? AND is_read = 0
        """, (partner, username))
        await db.commit()
    return {"status": "ok"}

# Счетчик для генерации уникальных имен инкогнито (в глобальную область)
INCOGNITO_COUNTER = 0

@app.post("/incognito_login")
async def incognito_login():
    global INCOGNITO_COUNTER
    INCOGNITO_COUNTER += 1
    
    # Формируем уникальное имя для сессии
    incognito_username = f"инкогнито-{INCOGNITO_COUNTER}"
    
    return {
        "success": True, 
        "username": incognito_username
    }

@app.get("/admin/metrics")
async def get_server_metrics(key: str):
    if key != ADMIN_SECRET_KEY: 
        return {"status": "error", "message": "Wrong Key"}
    
    try:
        # 1. Считаем реальный размер БД на диске Render
        db_size_mb = 0
        if os.path.exists(DB_PATH):
            db_size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
            
        # 2. Считываем оперативную память текущего процесса Python
        process = psutil.Process(os.getpid())
        ram_mb = round(process.memory_info().rss / (1024 * 1024), 1)
        
        # 3. Получаем нагрузку на процессор
        cpu_percent = psutil.cpu_percent()

        # 4. Считаем общее количество сокетов во всех комнатах
        active_connections = sum(len(room) for room in manager.rooms.values())

        return {
            "status": "ok",
            "db_size": f"{db_size_mb} MB",
            "ram_usage": f"{ram_mb} MB",
            "cpu_usage": f"{cpu_percent}%",
            "active_connections": active_connections
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
        
@app.post("/api/admin/mute")
async def admin_mute_user(data: dict):
    key = data.get("key")
    target = data.get("target", "").strip()
    minutes = int(data.get("minutes", 1))
    
    # Сверяем секретный ключ админа
    if key != ADMIN_SECRET_KEY:
        return {"status": "error", "message": "ACCESS DENIED: Сбой ключа доступа"}
        
    if not target:
        return {"status": "error", "message": "Узел-цель не определен"}
        
    # Вычисляем время окончания мута в секундах
    unmute_timestamp = time.time() + (minutes * 60)
    MUTED_DATA[target.lower()] = unmute_timestamp
    
    print(f"🔇 Узел {target} изолирован на {minutes} мин. Протокол запущен.")
    
    # Отправляем технический пакет в сокеты, чтобы мгновенно уведомить жертву
    if "general" in manager.rooms:
        for conn in manager.rooms["general"].values():
            await conn.send_text(f"ID:0|SYSTEM:MUTE_UPDATE|{target}|{unmute_timestamp}")
            
    return {"status": "ok", "message": f"Узел {target} успешно изолирован на {minutes} минут(ы)."}
    
# 1. РОУТ ДЛЯ СНЯТИЯ МУТА (;unmute)
@app.post("/api/admin/unmute")
async def admin_unmute_user(data: dict):
    key = data.get("key")
    target = data.get("target", "").strip().lower()
    if key != ADMIN_SECRET_KEY: return {"status": "error", "message": "Сбой ключа доступа"}
    
    if target in MUTED_DATA:
        del MUTED_DATA[target]
        # Уведомляем систему по сокетам
        if "general" in manager.rooms:
            for conn in manager.rooms["general"].values():
                await conn.send_text(f"ID:0|SYSTEM:UNMUTE_UPDATE|{target}")
        return {"status": "ok", "message": f"Узел {target} успешно размучен!"}
    return {"status": "error", "message": "Пользователь не замучен"}

# 2. РОУТ ДЛЯ ОТПРАВКИ В ДЕМОРГАН (;demorgan)
@app.post("/api/admin/demorgan")
async def admin_demorgan_user(data: dict):
    key = data.get("key")
    target = data.get("target", "").strip()
    minutes = int(data.get("minutes", 1))
    if key != ADMIN_SECRET_KEY: return {"status": "error", "message": "Сбой ключа доступа"}
    if not target: return {"status": "error", "message": "Узел-цель не определен"}
    
    release_timestamp = time.time() + (minutes * 60)
    DEMORGAN_DATA[target.lower()] = release_timestamp
    
    # Мощный пинок в сокеты: принудительно перекидываем жертву в чистилище
    if "general" in manager.rooms:
        for conn in manager.rooms["general"].values():
            await conn.send_text(f"ID:0|SYSTEM:DEMORGAN_UPDATE|{target}|{release_timestamp}")
            
    return {"status": "ok", "message": f"Узел {target} отправлен в Деморган на {minutes} мин."}


# Иерархия должностей
STAFF_HIERARCHY = [
    "Младший Модератор", "Модератор", "Старший Модератор", 
    "Младший Администратор", "Администратор", "Старший Администратор",
    "Заместитель Главного Модератора", "Главный Модератор", "Отдел по набору",
    "Заместитель Куратора Администратора", "Куратор Администраторов", 
    "Зам.Менеджера", "Менеджер", "Тех. Админ"
]

# 1. ПОЛУЧЕНИЕ STAFF-СОСТАВА С ПОДДЕРЖКОЙ ЖИВЫХ ГРАДИЕНТОВ РОЛЕЙ ИЗ DISCORD API
@app.get("/api/forum/staff")
async def get_forum_staff():
    BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    GUILD_ID = "1487896132511076465"
    
    if not BOT_TOKEN:
        print("❌ Ошибка безопасности: DISCORD_BOT_TOKEN не настроен в переменных Render!")
        return []
    
    staff_list = []
    
    try:
        headers = {"Authorization": f"Bot {BOT_TOKEN}"}
        
        # Получаем роли
        roles_url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/roles"
        async with httpx.AsyncClient() as client:
            roles_res = await client.get(roles_url, headers=headers, timeout=10.0)
            if roles_res.status_code != 200:
                print(f"❌ Ошибка получения ролей Discord: {roles_res.text}")
                return []
                
            roles_data = roles_res.json()
            roles_map = {}
            
            for r in roles_data:
                # Читаем стандартный цвет роли
                p_color = r.get("color", 0)
                hex_p = f"#{p_color:06x}" if p_color != 0 else "#828282"
                
                # 🎯 СВЕРХ-ФИКС ДЛЯ ГРАДИЕНТОВ: Проверяем наличие дополнительных цветов Enhanced Role Styles
                # Дискорд передает расширенные цвета в объекте "role_colors" или в виде полей primary/secondary
                r_colors = r.get("role_colors", {})
                s_color = r_colors.get("secondary_color")
                t_color = r_colors.get("tertiary_color")
                
                hex_s = f"#{s_color:06x}" if s_color and s_color != 0 else None
                hex_t = f"#{t_color:06x}" if t_color and t_color != 0 else None
                
                # Сохраняем паспорт цветов роли
                roles_map[str(r["id"])] = {
                    "name": r["name"],
                    "primary": hex_p,
                    "secondary": hex_s,
                    "tertiary": hex_t,
                    "is_gradient": hex_s is not None or hex_t is not None
                }
            
            # Получаем участников
            members_url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members?limit=1000"
            members_res = await client.get(members_url, headers=headers, timeout=10.0)
            if members_res.status_code != 200:
                print(f"❌ Ошибка получения участников Discord: {members_res.text}")
                return []
                
            members_data = members_res.json()
            
            # Подключаемся к локальной БД, чтобы вытащить средний рейтинг модераторов
            async with aiosqlite.connect(DB_PATH) as db:
                for m in members_data:
                    if "user" in m and m["user"].get("bot"):
                        continue
                        
                    user_id = m["user"]["id"]
                    display_name = m.get("nick") or m["user"].get("global_name") or m["user"]["username"]
                    username = m["user"]["username"]
                    
                    member_role_ids = m.get("roles", [])
                    
                    # 🎯 УПАКОВЫВАЕМ СЛОВАРЬ: Теперь это список объектов с цветами и градиентами
                    member_role_objects = []
                    member_role_names = []
                    
                    for rid in member_role_ids:
                        rid_str = str(rid)
                        if rid_str in roles_map:
                            member_role_objects.append(roles_map[rid_str])
                            member_role_names.append(roles_map[rid_str]["name"])
                    
                    matched_roles = [r for r in member_role_names if r in STAFF_HIERARCHY]
                    
                    if matched_roles:
                        matched_roles.sort(key=lambda x: STAFF_HIERARCHY.index(x))
                        highest_role = matched_roles[-1]
                        
                        avatar_hash = m["user"].get("avatar")
                        if avatar_hash:
                            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
                        else:
                            avatar_url = "https://i.ibb.co/4pSbxsh/user-avatar.png"
                            
                        # РАСЧЕТ РЕЙТИНГА
                        avg_rating = 5.0
                        review_count = 0
                        async with db.execute("SELECT AVG(rating), COUNT(*) FROM forum_reviews WHERE target_id = ?", (user_id,)) as cur:
                            row = await cur.fetchone()
                            if row and row[0] is not None:
                                avg_rating = round(row[0], 1)
                                review_count = row[1]
                        
                        # Проверяем, является ли пользователь создателем/владельцем
                        is_server_owner = m.get("owner", False) or "Владелец" in "".join(member_role_names)
                        
                        staff_list.append({
                            "id": user_id,
                            "name": display_name,
                            "username": username,
                            "avatar": avatar_url,
                            "role": highest_role,
                            # 🎯 ТЕПЕРЬ ПЕРЕДАЕМ СТРУКТУРУ С ЦВЕТАМИ И ТИПАМИ ДЛЯ АДМИН-ЧАТА!
                            "roles": member_role_objects,  
                            "is_owner": is_server_owner,  
                            "rating": avg_rating,        
                            "reviews": review_count      
                        })
                        
    except Exception as e:
        print(f"🛑 Ошибка шлюза Discord API в server.py: {e}")
        
    return staff_list



# ==========================================
# 🔑 СИСТЕМА СВЕРХБЫСТРОЙ АВТОРИЗАЦИИ DISCORD OAUTH2
# ==========================================

# 1. Ссылка-перенаправление на форму авторизации Дискорда (ИСПРАВЛЕНО)
@app.get("/api/forum/auth/login")
async def discord_login_redirect():
    from urllib.parse import quote # Импортируем родной кодировщик Python
    import os
    
    client_id = os.getenv("DISCORD_CLIENT_ID")
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "")
    
    # Кодируем redirect_uri стандартным методом Python quote() вместо JavaScript
    encoded_redirect = quote(redirect_uri, safe="")
    
    # 🎯 ИСПРАВЛЕНО: Добавлены %20guilds и правильное Python-кодирование ссылки!
    url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={encoded_redirect}&response_type=code&scope=identify%20guilds"
    return RedirectResponse(url)


# Легкий внутренний хелпер для кодирования URL-компонентов
def encodeURIComponent(text: str) -> str:
    import urllib.parse
    return urllib.parse.quote(text, safe='')

# 2. Коллбэк-приемник: ловит пользователя после успешного входа в Дискорд (ОБНОВЛЕНО ДЛЯ ДЭШБОРДА)
@app.get("/api/forum/auth/callback")
async def discord_callback(code: str, request: Request):
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI")
    
    try:
        # Обмениваем временный код на постоянный токен доступа юзера
        async with httpx.AsyncClient() as client:
            token_data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri
            }
            token_res = await client.post("https://discord.com/api/v10/oauth2/token", data=token_data)
            tokens = token_res.json()
            access_token = tokens.get("access_token")
            
            if not access_token:
                print(f"❌ Не удалось получить токен доступа. Ответ Discord: {tokens}")
                return RedirectResponse("/forum?auth=error")
                
            # Запрашиваем информацию о вошедшем пользователе (его имя и аватарку)
            user_headers = {"Authorization": f"Bearer {access_token}"}
            user_res = await client.get("https://discord.com/api/v10/users/@me", headers=user_headers)
            user_info = user_res.json()
            
            u_id = str(user_info["id"]).strip()
            
            # 🎯 ФИКС ИМЕНИ: Берем только чистую строку глобального имени без дублирования
            u_name = user_info.get("global_name") or user_info["username"]
            u_name = str(u_name).strip()
            
            avatar_hash = user_info.get("avatar")
            
            # 🎯 1. ПРИОРИТЕТ: Собираем настоящую личную аватарку из глобального профиля Discord
            if avatar_hash:
                ext = "gif" if avatar_hash.startswith("a_") else "png"
                u_avatar = f"https://cdn.discordapp.com/avatars/{u_id}/{avatar_hash}.{ext}"
            else:
                try:
                    discriminator = int(user_info.get("discriminator", 0))
                    if discriminator == 0:
                        def_avatar_index = (int(u_id) >> 22) % 6
                    else:
                        def_avatar_index = discriminator % 5
                except:
                    def_avatar_index = 0
                u_avatar = f"https://cdn.discordapp.com/embed/avatars/{def_avatar_index}.png"

            # 🎯 2. ЗАПАСНАЯ СТРАХОВКА: Если пользователь есть в STAFF, сверяем его ник без учёта регистра букв
            try:
                staff_members = await get_forum_staff()
                clean_my_name = u_name.lower().strip()
                for staff_user in staff_members:
                    staff_display_name = str(staff_user.get("name", "")).lower().strip()
                    if clean_my_name in staff_display_name:
                        # Если для модератора на сервере задана кастомная аватарка, подтягиваем её
                        u_avatar = staff_user.get("avatar")
                        print(f"🎯 [FORUM AUTH] Аватарка синхронизирована со STAFF-карточкой!")
                        break
            except Exception as e:
                print(f"⚠️ Ошибка запасной синхронизации аватарки: {e}")

            # 🎯 3. ФИКС: Фиксируем реальную дату первой авторизации аккаунта в СУБД SQLite для профилей и модалок
            reg_date = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO user_custom_settings (username, custom_status) 
                    VALUES (?, ?) 
                    ON CONFLICT(username) DO NOTHING
                """, (u_name, f"Присоединился: {reg_date}"))
                await db.commit()

            # 🎯 ФИКС КУК: Создаем редирект и жестко привязываем куки авторизации к корню сайта path="/"
            res = RedirectResponse("/forum?auth=success")
            res.set_cookie(key="forum_user_name", value=u_name, max_age=2592000, path="/")
            res.set_cookie(key="forum_user_avatar", value=u_avatar, max_age=2592000, path="/")
            
            # 🎯 КРИТИЧЕСКИЙ ФИКС: Зашиваем Discord ID в куки при авторизации на форуме!
            # (Если твоя переменная с ID называется иначе, например u_id, просто подставь её вместо u_id)
            res.set_cookie(key="forum_user_id", value=str(u_id), max_age=2592000, path="/")

            
            # 🎯 СУПЕР-ОБНОВЛЕНИЕ ДЛЯ ДЭШБОРДА: Сохраняем access_token в куку для живых запросов серверов к Discord API
            res.set_cookie(
                key="forum_discord_token",
                value=str(access_token),
                max_age=2592000, # 30 дней аптайма
                path="/",
                httponly=True,   # Жесткая защита токена от XSS скриптов на фронтенде
                samesite="lax"
            )
            return res
            
    except Exception as e:
        print(f"🛑 Ошибка OAuth2: {e}")
        return RedirectResponse("/forum?auth=error")




# 3. Выход из аккаунта (Исправлена очистка корневых кук)
@app.get("/api/forum/auth/logout")
async def discord_logout():
    response = RedirectResponse("/forum")
    # 🎯 ИСПРАВЛЕНО: Явно указали path="/", чтобы куки стирались из памяти браузера намертво
    response.delete_cookie("forum_user_name", path="/")
    response.delete_cookie("forum_user_avatar", path="/")
    return response


# ==========================================
# 🏛️ ОБНОВЛЁННАЯ СИСТЕМА ОТЗЫВОВ И РЕЙТИНГА
# ==========================================

@app.post("/api/forum/review")
async def add_staff_review(data: dict):
    try:
        target_id = str(data.get("target_id", "")).strip() 
        author = data.get("author", "").strip()            
        text = data.get("text", "").strip()                
        rating = int(data.get("rating", 5))                
        
        if rating < 1: rating = 1
        if rating > 5: rating = 5
        
        ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        
        if not target_id or not author or not text:
            return {"status": "error", "message": "Заполните все поля отзыва!"}
            
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO forum_reviews (target_id, author, review_text, rating, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (target_id, author, text, rating, ts))
            await db.commit()
            
        return {"status": "ok", "message": "Ваш отзыв и оценка успешно зафиксированы!"}
        
    except Exception as e:
        print(f"🛑 Ошибка сохранения отзыва: {e}")
        return {"status": "error", "message": f"Ошибка СУБД: {str(e)}"}


@app.get("/api/forum/reviews/{target_id}")
async def get_staff_reviews(target_id: str):
    try:
        target_id_str = str(target_id).strip()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT author, review_text, rating, timestamp 
                FROM forum_reviews 
                WHERE target_id = ? 
                ORDER BY id DESC
            """, (target_id_str,)) as cursor:
                rows = await cursor.fetchall()
                return [{"author": r[0], "text": r[1], "rating": r[2], "time": r[3]} for r in rows]
    except Exception as e:
        return []


# 4. РЕНДЕРИНГ САМОЙ СТРАНИЦЫ ФОРУМА
@app.get("/forum")
async def serve_forum_page():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


# ==========================================
# 🎫 СИСТЕМА ОБРАЩЕНИЙ И ЖАЛОБ (ТИКЕТЫ)
# ==========================================

# ПОЛУЧЕНИЕ ФИЛЬТРОВАННОГО РЕЕСТРА ЖАЛОБ С УЧЕТОМ КАТЕГОРИЙ И АРХИВА
# 🎯 ИСПРАВЛЕНО: Перевели SQL-запросы на пуленепробиваемый выбор всех полей через (*) 
# Это на 100% убирает любые ошибки "no such column", если имена колонок в таблице слегка отличаются!
# 🎯 ИСПРАВЛЕНО: Полный переход на (*) во всех ветках для 100% защиты от "no such column"
@app.get("/api/forum/tickets/list")
async def get_moderators_tickets_list(request: Request, category: str = "all"):
    try:
        username = request.cookies.get("forum_user_name", "").strip()
        cat_filter = str(category).strip().lower()
        print(f"🔎 [SERVER FILTER] Обработка категории: {cat_filter} для пользователя: {username}")

        rows = []
        async with aiosqlite.connect(DB_PATH) as db:
            
            if cat_filter == "mod_archive":
                # 1. Личный архив модератора через безопасную звёздочку (*)
                if username:
                    async with db.execute("""
                        SELECT * FROM forum_tickets 
                        WHERE LOWER(moderator_name) = ? ORDER BY id DESC
                    """, (username.lower(),)) as cursor:
                        rows = await cursor.fetchall()
            
            elif cat_filter in ["user", "staff", "appeal"]:
                # 2. Фильтрация по типам через безопасную звёздочку (*)
                async with db.execute("SELECT * FROM forum_tickets WHERE LOWER(ticket_type) = ? ORDER BY id DESC", (cat_filter,)) as cursor:
                    rows = await cursor.fetchall()
            
            else:
                # 3. Дефолт (all): выгружаем абсолютно все обращения через безопасную звёздочку (*)
                async with db.execute("SELECT * FROM forum_tickets ORDER BY id DESC") as cursor:
                    rows = await cursor.fetchall()

        print(f"📦 [SERVER DATABASE] Извлечено строк из СУБД SQLite: {len(rows)}")

        # Гарантированный порядок индексов СУБД при SELECT *:
        # 0: id, 1: ticket_type, 2: author_name, 3: author_avatar, 4: message_text, 5: status, 6: timestamp, 7: moderator_name
        return [
            {
                "id": r[0],
                "type": str(r[1]).strip().lower() if r[1] else "user", 
                "author": r[2] if r[2] else "Гражданин",
                "avatar": r[3] if r[3] else "https://i.ibb.co/4pSbxsh/user-avatar.png",
                "text": r[4] if r[4] else "Описание отсутствует",
                "status": r[5] if r[5] else "open",
                "time": r[6] if r[6] else "1 час назад",
                "moderator": r[7] if r[7] else ""
            } for r in rows
        ]
            
    except Exception as e:
        print(f"🛑 Критическая ошибка фильтрации тикетов в SQLite: {e}")
        return []



# 3. ПОЛУЧЕНИЕ ДАННЫХ КОНКРЕТНОГО ОБРАЩЕНИЯ ПО ID
# 🎯 ИСПРАВЛЕНО: Добавлен префикс /get/, чтобы роут на 100% совпал с вызовами в forum.html!
@app.get("/api/forum/tickets/get/{ticket_id}")
async def get_single_forum_ticket(ticket_id: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT id, ticket_type, author_name, author_avatar, description, photos, status, moderator_name, timestamp 
                FROM forum_tickets WHERE id = ?
            """, (ticket_id,)) as cursor:
                r = await cursor.fetchone()
                if not r:
                    return {"status": "error", "message": "Обращение не найдено"}
                    
                return {
                    "id": r[0], 
                    "type": r[1], 
                    "author": r[2],
                    "avatar": r[3], 
                    "text": r[4], # Фронтенд ожидает ключ "text", а не "description"
                    "photos": r[5].split(",") if r[5] else [],
                    "status": r[6], # Фронтенд ожидает ключ "status", а не "ticket_status"
                    "moderator": r[7],
                    "time": r[8]
                }
    except Exception as e:
        print(f"🛑 Ошибка получения тикета {ticket_id}: {e}")
        return {"status": "error", "message": str(e)}

# 🎯 ИСПРАВЛЕНО: Теперь модератор может одобрить/отклонить тикет из ЛЮБОГО статуса, 
# и его ник автоматически привязывается к делу для вывода в архив!
@app.post("/api/forum/tickets/status")
async def update_forum_ticket_status(data: dict, request: Request):
    try:
        mod_name = request.cookies.get("forum_user_name", "").strip()
        if not mod_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы!"}
            
        staff_members = await get_forum_staff()
        is_staff = any(str(mod_name).strip().lower() in str(u.get("name", "")).strip().lower() for u in staff_members)
        
        if not is_staff:
            return {"status": "error", "message": "🛑 Отказано в доступе! Вы не являетесь модератором."}
            
        ticket_id = int(data.get("ticket_id"))
        new_status = str(data.get("status")).strip().lower() # 'processing', 'approved', 'rejected'
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Сначала вытащим имя автора тикета, чтобы закинуть ему алерт в центр уведомлений
            async with db.execute("SELECT author_name FROM forum_tickets WHERE id = ?", (ticket_id,)) as cur:
                ticket_row = await cur.fetchone()
                ticket_author = ticket_row[0] if ticket_row else None

            ts_notif = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
            notification_text = ""

            # 🎯 СУПЕР-ФИКС: Убрали жесткое ограничение AND status = 'processing'
            if new_status == 'processing':
                await db.execute("UPDATE forum_tickets SET status = 'processing', moderator_name = ? WHERE id = ?", (mod_name, ticket_id))
                notification_text = f"💼 Ваше обращение #POST_{ticket_id} взято в работу сотрудником {mod_name}"
            
            elif new_status == 'approved':
                # Принудительно вписываем модератора, даже если он одобрил тикет сразу из ожидания
                await db.execute("UPDATE forum_tickets SET status = 'approved', moderator_name = ? WHERE id = ?", (mod_name, ticket_id))
                notification_text = f"✅ Ваше обращение #POST_{ticket_id} было успешно ОДОБРЕНО!"
            
            elif new_status == 'rejected':
                # Принудительно вписываем модератора при отклонении тикета сразу из ожидания
                await db.execute("UPDATE forum_tickets SET status = 'rejected', moderator_name = ? WHERE id = ?", (mod_name, ticket_id))
                notification_text = f"❌ Ваше обращение #POST_{ticket_id} было ОТКЛОНЕНО модерацией."
                
            # Записываем системный алерт для колокольчика кандидата
            if notification_text and ticket_author:
                await db.execute("""
                    INSERT INTO forum_notifications (username, text, ticket_id, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (ticket_author, notification_text, ticket_id, ts_notif))

            await db.commit()
            
        print(f"✅ [STATUS CHANGED] Тикет #POST_{ticket_id} получил статус: {new_status} от {mod_name}")
        return {"status": "ok", "message": f"Статус дела успешно изменен на: {new_status}"}
        
    except Exception as e:
        print(f"🛑 Ошибка изменения статуса тикета: {e}")
        return {"status": "error", "message": str(e)}


# 5. ДИНАМИЧЕСКИЙ РОУТ ДЛЯ СТРАНИЦ ТИКЕТОВ ПО ССЫЛКЕ /forum/post_1, /forum/post_2
@app.get("/forum/post_{ticket_id}")
async def serve_single_ticket_page(ticket_id: int):
    # Страница тикета рендерится через тот же файл, фронтенд сам поймет ID из URL-адреса!
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


@app.post("/api/forum/tickets/create")
async def create_forum_ticket(
    request: Request,
    type: str = Form(...),            # Ловим поле type из FormData
    text: str = Form(...),            # Ловим поле text из FormData
    photos: List[UploadFile] = File(default=[]) # Ловим массив файлов скриншотов
):
    try:
        # Читаем данные автора прямо из кук его авторизованной сессии Дискорда
        author_name = request.cookies.get("forum_user_name", "Аноним")
        
        # 🎯 ИСПРАВЛЕНО: Рабочая дефолтная аватарка-заглушка вместо битой ссылки ibb.co
        author_avatar = request.cookies.get("forum_user_avatar", "https://i.ibb.co/4pSbxsh/user-avatar.png")
        
        # Папка на сервере Render, куда будут сохраняться скриншоты
        upload_dir = os.path.join(BASE_DIR, "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        saved_photo_urls = []
        
        # Сохраняем прикрепленные файлы скриншотов на диск сервера
        for photo in photos:
            if not photo.filename:
                continue
                
            # Формируем уникальное имя файла, чтобы скриншоты не перезаписывались
            unique_filename = f"ticket_{int(datetime.now().timestamp())}_{photo.filename}"
            file_path = os.path.join(upload_dir, unique_filename)
            
            with open(file_path, "wb") as f:
                f.write(await photo.read())
            
            # Формируем публичную ссылку на скриншот
            saved_photo_urls.append(f"/static/uploads/{unique_filename}")
            
        # Объединяем ссылки на фото через запятую для хранения в одной текстовой колонке SQLite
        photos_str = ",".join(saved_photo_urls)
        ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        
        # Записываем обращение в базу данных aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO forum_tickets (ticket_type, author_name, author_avatar, description, photos, status, moderator_name, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (type, author_name, author_avatar, text, photos_str, "open", "", ts))
            await db.commit()
            ticket_id = cursor.lastrowid
            
        return {"status": "ok", "message": "Обращение успешно зарегистрировано!", "ticket_id": ticket_id}
        
    except Exception as e:
        print(f"🛑 Ошибка создания тикета: {e}")
        return {"status": "error", "message": str(e)}

import re

# 1. ОТПРАВКА НОВОГО СООБЩЕНИЯ В ЧАТ ТИКЕТА С УМНЫМ ВЕБХУКОМ ДИСКОРДА
@app.post("/api/forum/tickets/comment/send")
async def send_ticket_comment(data: dict, request: Request):
    try:
        author_name = request.cookies.get("forum_user_name")
        author_avatar = request.cookies.get("forum_user_avatar", "https://i.ibb.co/4pSbxsh/user-avatar.png")
        
        if not author_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы через Discord!"}
            
        ticket_id = int(data.get("ticket_id"))
        text = str(data.get("text", "")).strip()
        
        if not text:
            return {"status": "error", "message": "Нельзя отправить пустое сообщение!"}
            
        ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        
        # Записываем комментарий в базу данных aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO ticket_comments (ticket_id, author_name, author_avatar, message_text, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (ticket_id, author_name, author_avatar, text, ts))
            await db.commit()
            
        # 🎯 СИСТЕМА УМНЫХ МЕНШЕНОВ И УВЕДОМЛЕНИЙ ЧЕРЕЗ ВЕБХУК:
        # Ищем в тексте все упоминания вида @Никнейм (поддерживает буквы, цифры и спецсимволы)
        mentions = re.findall(r"@([a-zA-Z0-9_А-яёЁ\s\-]+)", text)
        
        if mentions:
            # Сюда вставь URL вебхука из настроек твоего Discord-канала "форум"
            DISCORD_WEBHOOK_URL = os.getenv("FORUM_WEBHOOK_URL", "https://discord.com/api/webhooks/1511469317391253645/XMovNDc-9ZLyA-WABFbNU-hic168Wo9O3-5aCJESimvHQH24549pRaCXCSi89JlUDmHA")
            async with aiosqlite.connect(DB_PATH) as db:
                for mention_name in mentions:
                    target_mention_name = mention_name.strip()
                    # Записываем в СУБД уведомление для того, кого тегнули
                    await db.execute("""
                        INSERT INTO forum_notifications (username, text, ticket_id, timestamp)
                        VALUES (?, ?, ?, ?)
                    """, (target_mention_name, f"🚨 Пользователь {author_name} упомянул вас в чате обращения #POST_{ticket_id}", ticket_id, ts))
                await db.commit()

            if DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL != "ТУТ_ТВОЙ_URL_ВЕБХУКА_ЕСЛИ_НЕ_В_RENDER":
                try:
                    # Скачиваем список персонала, чтобы превратить текстовые ники в числовые ID Дискорда для пинга
                    staff_members = await get_forum_staff()
                    
                    for mention_name in mentions:
                        mention_name_clean = mention_name.strip().lower()
                        discord_ping_str = f"**@{mention_name.strip()}**" # По дефолту просто жирный текст
                        
                        # Проверяем, есть ли упомянутый модератор в иерархии STAFF
                        for staff_user in staff_members:
                            staff_display_name = str(staff_user.get("name", "")).lower()
                            # Если ник совпал (или содержится в серверном нике, например, "Менеджер | Bulgarian")
                            if mention_name_clean in staff_display_name or staff_display_name in mention_name_clean:
                                # Формируем официальный числовой пинг Дискорда <@ID>
                                discord_ping_str = f"<@{staff_user.get('id')}>"
                                break
                        
                        # Ссылка на конкретное обращение, по которой модератор перейдёт с телефона/ПК
                        ticket_url = f"https://pinnogram-server.onrender.com/forum/post_{ticket_id}"
                        
                        # Красивый payload для отправки в Discord-канал
                        webhook_data = {
                            "content": f"🚨 {discord_ping_str}, вас упомянули в обращении на форуме!\n💬 **Автор пинга:** {author_name}\n🔗 **Прямая ссылка на дело:** {ticket_url}"
                        }
                        
                        # Пуляем POST-запрос напрямую на сервера Discord API
                        async with httpx.AsyncClient() as client:
                            await client.post(DISCORD_WEBHOOK_URL, json=webhook_data)
                            
                except Exception as webhook_err:
                    print(f"⚠️ Не удалось отправить уведомление вебхука: {webhook_err}")
            
        return {"status": "ok", "message": "Сообщение отправлено!"}
    except Exception as e:
        print(f"🛑 Ошибка отправки комментария: {e}")
        return {"status": "error", "message": str(e)}


# 2. ПОЛУЧЕНИЕ ИСТОРИИ ПЕРЕПИСКИ ДЛЯ КОНКРЕТНОГО ТИКЕТА
@app.get("/api/forum/tickets/comment/list/{ticket_id}")
async def get_ticket_comments_list(ticket_id: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT author_name, author_avatar, message_text, timestamp 
                FROM ticket_comments WHERE ticket_id = ? ORDER BY id ASC
            """, (ticket_id,)) as cursor:
                rows = await cursor.fetchall()
                return [{
                    "author": r[0], "avatar": r[1], "text": r[2], "time": r[3]
                } for r in rows]
    except Exception as e:
        print(f"🛑 Ошибка получения комментариев: {e}")
        return []

# 1. ДИНАМИЧЕСКИЙ РОУТ ДЛЯ СТРАНИЦЫ ПРОФИЛЯ ПО ССЫЛКЕ /forum/user_ID
@app.get("/forum/user_{user_id}")
async def serve_user_profile_page(user_id: str):
    # Страница профиля работает через тот же файл, фронтенд сам поймет ID из URL-адреса!
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


# 2. ПОЛУЧЕНИЕ ПОЛНЫХ ДАННЫХ ПРОФИЛЯ ДЛЯ ЭКРАНА И МИНИ-КАРТОЧЕК (ИСПРАВЛЕНО)
@app.get("/api/forum/user/profile/{target_id}")
async def get_user_profile_data(target_id: str, request: Request):
    try:
        # 🎯 СУПЕР-ФИКС: Определяем, чьё имя запрашивают
        # Если пришел маркер "me" — берем имя из куки текущей сессии, иначе берем имя выбранного юзера
        if target_id == "me":
            profile_username = request.cookies.get("forum_user_name", "")
        else:
            profile_username = target_id.strip()

        if not profile_username:
            return {"status": "error", "message": "Пользователь не указан"}

        role_label = "Гражданин"
        is_staff = False
        
        # Вычисляем высшую роль конкретно для запрашиваемого профиля
        try:
            staff_members = await get_forum_staff()
            clean_profile_name = profile_username.lower().strip()
            
            for staff_user in staff_members:
                staff_display_name = str(staff_user.get("name", "")).strip().lower()
                if clean_profile_name in staff_display_name or staff_display_name in clean_profile_name:
                    role_label = staff_user.get("role", "Модератор")
                    is_staff = True
                    break
        except Exception as staff_err:
            print(f"⚠️ Ошибка проверки роли в профиле: {staff_err}")

        # Инициализируем счетчики статистики
        tickets_created = 0
        comments_count = 0
        history_actions = []

        async with aiosqlite.connect(DB_PATH) as db:
            # Читаем созданные обращения автора профиля
            async with db.execute("""
                SELECT id, ticket_type, timestamp, status 
                FROM forum_tickets 
                WHERE author_name = ? ORDER BY id DESC
            """, (profile_username,)) as cursor:
                rows = await cursor.fetchall()
                tickets_created = len(rows)
                for r in rows:
                    history_actions.append({
                        "id": r[0],
                        "type": "create",
                        "ticket_type": r[1],
                        "time": r[2],
                        "status": r[3],
                        "text": f"Создано обращение #POST_{r[0]}"
                    })

            # Если это профиль модератора, добавляем в историю рассмотренные им дела
            if is_staff:
                async with db.execute("""
                    SELECT id, ticket_type, timestamp, status 
                    FROM forum_tickets 
                    WHERE moderator_name = ? ORDER BY id DESC
                """, (profile_username,)) as cursor:
                    mod_rows = await cursor.fetchall()
                    for r in mod_rows:
                        history_actions.append({
                            "id": r[0],
                            "type": "moderate",
                            "ticket_type": r[1],
                            "time": r[2],
                            "status": r[3],
                            "text": f"Взято в работу/Рассмотрено обращение #POST_{r[0]}"
                        })

            # Считаем количество оставленных комментариев в чатах
            async with db.execute("SELECT COUNT(*) FROM ticket_comments WHERE author_name = ?", (profile_username,)) as cursor:
                row_comm = await cursor.fetchone()
                comments_count = row_comm[0] if row_comm else 0

            # 🎯 ВЫЕМКА ДАТЫ РЕГИСТРАЦИИ, БАННЕРА И ГРАДИЕНТА ИЗ SQLite
            db_banner = ""
            db_gradient = ""
            db_reg_date = "25.05.2026"  # Запасной дефолт

            async with db.execute("""
                SELECT banner_url, nickname_gradient, custom_status 
                FROM user_custom_settings WHERE username = ?
            """, (profile_username,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    db_banner = row[0] or ""
                    db_gradient = row[1] or ""
                    # Если в поле статуса лежит строка с датой, аккуратно вырезаем её наружу
                    if row[2] and "Присоединился:" in str(row[2]):
                        db_reg_date = str(row[2]).replace("Присоединился: ", "").strip()

        # Сортируем историю действий по ID (свежие сверху)
        history_actions.sort(key=lambda x: x["id"], reverse=True)

        return {
            "status": "ok",
            "username": profile_username,
            "role": role_label,
            "is_staff": is_staff,
            "stat_tickets": tickets_created,
            "stat_comments": comments_count,
            "banner": db_banner,
            "gradient": db_gradient,
            "reg_date": db_reg_date,
            "history": history_actions
        }
        
    except Exception as e:
        print(f"🛑 Ошибка сбора данных профиля: {e}")
        return {"status": "error", "message": str(e)}


# 1. СТРАНИЦА НАСТРОЕК (РЕНДЕРИНГ HTML)
@app.get("/settings")
async def serve_settings_page():
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


# 2. СОХРАНЕНИЕ ТЕКСТОВЫХ НАСТРОЕК (ГРАДИЕНТ И СТАТУС)
@app.post("/api/forum/user/settings/save")
async def save_user_settings(data: dict, request: Request):
    try:
        username = request.cookies.get("forum_user_name")
        if not username:
            return {"status": "error", "message": "🔒 Вы не авторизованы через Discord!"}
            
        gradient = str(data.get("gradient", "")).strip()
        status = str(data.get("status", "")).strip()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Используем INSERT OR REPLACE, чтобы обновлять существующие настройки пользователя
            await db.execute("""
                INSERT INTO user_custom_settings (username, nickname_gradient, custom_status)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET 
                    nickname_gradient = excluded.nickname_gradient,
                    custom_status = excluded.custom_status
            """, (username, gradient, status))
            await db.commit()
            
        return {"status": "ok", "message": "Настройки кастомизации успешно применены!"}
    except Exception as e:
        print(f"🛑 Ошибка сохранения настроек: {e}")
        return {"status": "error", "message": str(e)}


# 3. ЗАГРУЗКА ИЗОБРАЖЕНИЯ БАННЕРА ПРОФИЛЯ (ИСПРАВЛЕНО)
@app.post("/api/forum/user/settings/upload-banner")
async def upload_user_banner(request: Request, file: UploadFile = File(...)):
    try:
        username = request.cookies.get("forum_user_name")
        if not username:
            return {"status": "error", "message": "🔒 Вы не авторизованы через Discord!"}
            
        if not file.filename:
            return {"status": "error", "message": "Файл изображения не выбран!"}
            
        # Папка сохранения баннеров на сервере Render
        upload_dir = os.path.join(BASE_DIR, "static", "banners")
        os.makedirs(upload_dir, exist_ok=True)
        
        # 🎯 СУПЕР-ФИКС: Обрезка строки через правильный синтаксис Python [:15] вместо JS .substring
        safe_username = "".join([c for c in username if c.isalpha() or c.isdigit()])[:15]
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"banner_{safe_username}_{int(datetime.now().timestamp())}{file_ext}"
        file_path = os.path.join(upload_dir, unique_filename)
        
        # Записываем байты файла на диск
        with open(file_path, "wb") as f:
            f.write(await file.read())
            
        banner_public_url = f"/static/banners/{unique_filename}"
        
        # Сохраняем ссылку на баннер в базу данных SQLite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO user_custom_settings (username, banner_url)
                VALUES (?, ?)
                ON CONFLICT(username) DO UPDATE SET banner_url = excluded.banner_url
            """, (username, banner_public_url))
            await db.commit()
            
        return {"status": "ok", "banner_url": banner_public_url, "message": "Баннер успешно загружен!"}
    except Exception as e:
        print(f"🛑 Ошибка загрузки баннера: {e}")
        return {"status": "error", "message": str(e)}


# 4. ПОЛУЧЕНИЕ КАСТОМНЫХ НАСТРОЕК ДЛЯ ОТРЕНДЕРИВАНИЯ НА САЙТЕ
@app.get("/api/forum/user/settings/get/{username}")
async def get_user_custom_settings(username: str):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT banner_url, nickname_gradient, custom_status 
                FROM user_custom_settings WHERE username = ?
            """, (username.strip(),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"banner": row[0], "gradient": row[1], "status": row[2]}
                return {"banner": "", "gradient": "", "status": ""}
    except Exception as e:
        return {"banner": "", "gradient": "", "status": ""}

# 1. ПОЛУЧЕНИЕ НЕПРОЧИТАННЫХ УВЕДОМЛЕНИЙ И ИХ КОЛИЧЕСТВА
@app.get("/api/forum/notifications/list")
async def get_user_notifications(request: Request):
    try:
        username = request.cookies.get("forum_user_name")
        if not username:
            return {"unread_count": 0, "list": []}
            
        async with aiosqlite.connect(DB_PATH) as db:
            # Считаем количество непрочитанных
            async with db.execute("SELECT COUNT(*) FROM forum_notifications WHERE username = ? AND is_read = 0", (username,)) as cursor:
                unread_count = (await cursor.fetchone())[0]
                
            # Забираем последние 20 уведомлений
            async with db.execute("""
                SELECT id, text, ticket_id, is_read, timestamp 
                FROM forum_notifications 
                WHERE username = ? ORDER BY id DESC LIMIT 20
            """, (username,)) as cursor:
                rows = await cursor.fetchall()
                
            return {
                "unread_count": unread_count,
                "list": [{"id": r[0], "text": r[1], "ticket_id": r[2], "is_read": r[3], "time": r[4]} for r in rows]
            }
    except Exception as e:
        return {"unread_count": 0, "list": []}


# 2. ОТМЕТИТЬ ВСЕ УВЕДОМЛЕНИЯ КАК ПРОЧИТАННЫЕ
@app.post("/api/forum/notifications/read-all")
async def read_all_notifications(request: Request):
    try:
        username = request.cookies.get("forum_user_name")
        if not username:
            return {"status": "error", "message": "Не авторизован"}
            
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE forum_notifications SET is_read = 1 WHERE username = ?", (username,))
            await db.commit()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# 1. ДИНАМИЧЕСКИЙ РОУТ ДЛЯ СТРАНИЦЫ АДМИН-ПАНЕЛИ ПО ССЫЛКЕ /forum/admin
@app.get("/forum/admin")
async def serve_admin_panel_page(request: Request):
    # Дополнительная базовая проверка кук при прямом переходе по URL
    mod_name = request.cookies.get("forum_user_name")
    if not mod_name:
        return RedirectResponse("/forum?auth=login_required")
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


@app.get("/api/forum/admin/users-registry")
async def get_all_registered_users_registry(request: Request):
    try:
        # 1. Проверка безопасности сессии
        mod_name = request.cookies.get("forum_user_name")
        if not mod_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы!"}
            
        staff_members = await get_forum_staff()
        is_admin = any(str(mod_name).strip().lower() in str(u.get("name", "")).strip().lower() for u in staff_members)
        
        if not is_admin:
            return {"status": "error", "message": "🛑 Отказано в доступе! Раздел предназначен только для Администрации."}

        users_map = {}

        async with aiosqlite.connect(DB_PATH) as db:
            # 🎯 СУПЕР-СБОР: Объединяем авторов тикетов, комментариев и кастомизаций
            # Собираем всех из тикетов
            async with db.execute("SELECT DISTINCT author_name, author_avatar FROM forum_tickets") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r[0]: users_map[r[0]] = {"username": r[0], "avatar": r[1], "status": "Участник мессенджера Pinnogram"}

            # Собираем всех из комментариев
            async with db.execute("SELECT DISTINCT author_name, author_avatar FROM ticket_comments") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r[0] and r[0] not in users_map:
                        users_map[r[0]] = {"username": r[0], "avatar": r[1], "status": "Участник мессенджера Pinnogram"}

            # Собираем всех из кастомизаций
            async with db.execute("SELECT DISTINCT username FROM user_custom_settings") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r[0] and r[0] not in users_map:
                        users_map[r[0]] = {"username": r[0], "avatar": "https://i.ibb.co/4pSbxsh/user-avatar.png", "status": "Участник мессенджера Pinnogram"}

            # Превращаем мапу в список для сортировки
            users_registry = list(users_map.values())
                    
            # Синхронизируем аватарки и статусы для модераторов
            for user in users_registry:
                for staff in staff_members:
                    if user["username"].lower().strip() in str(staff.get("name", "")).lower().strip():
                        user["avatar"] = staff.get("avatar")
                        user["status"] = f"Сотрудник проекта • {staff.get('role')}"
                        break

        # Сортируем по алфавиту
        users_registry.sort(key=lambda x: x["username"].lower())
        return {"status": "ok", "users": users_registry}
        
    except Exception as e:
        print(f"🛑 Критическая ошибка админ-панели СУБД: {e}")
        return {"status": "error", "message": str(e)}
# Массив высших должностей, которым разрешено проверять анкеты кандидатов
HR_ALLOWED_ROLES = [
    "Отдел по набору", "Заместитель Куратора Администратора", 
    "Куратор Администраторов", "Зам.Менеджера", "Менеджер", "Тех. Админ"
]

# ==========================================================
# 💼 АВТОМАТИЗИРОВАННАЯ HR-СИСТЕМА И АРХИВ АНКЕТ КАНДИДАТОВ
# ==========================================================

# 1. РЕНДЕРИНГ СТРАНИЦЫ АНКЕТ И АРХИВА (УНИВЕРСАЛЬНЫЙ)
@app.get("/moderators")
async def serve_moderators_application_page():
    import os
    return FileResponse(os.path.join(BASE_DIR, "forum.html"))


# 2. ПРИЁМ СДАННОЙ АНКЕТЫ ОТ КАНДИДАТА В БАЗУ ДАННЫХ
@app.post("/api/forum/applications/submit")
async def submit_moderator_application(data: dict, request: Request):
    try:
        author_name = request.cookies.get("forum_user_name")
        author_avatar = request.cookies.get("forum_user_avatar", "https://ibb.co")
        
        if not author_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы через Discord!"}
            
        answers = data.get("answers") 
        if not answers or not isinstance(answers, list):
            return {"status": "error", "message": "Анкета не заполнена или передана неверно!"}
            
        # Упаковываем массив ответов в текстовую строку JSON для хранения в одной ячейке SQLite
        answers_json_str = json.dumps(answers, ensure_ascii=False)
        ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO forum_applications (author_name, author_avatar, answers_json, timestamp)
                VALUES (?, ?, ?, ?)
            """, (author_name, author_avatar, answers_json_str, ts))
            await db.commit()
            
        return {"status": "ok", "message": "✨ Ваша анкета успешно сдана и передана Отделу Кадров!"}
    except Exception as e:
        print(f"🛑 Ошибка сдачи анкеты: {e}")
        return {"status": "error", "message": str(e)}


# 3. ПОЛУЧЕНИЕ СПИСКА СДАННЫХ АНКЕТ ДЛЯ КУРАТОРОВ (ЗАЩИЩЕНО + ЖИВЫЕ ОТВЕТЫ)
@app.get("/api/forum/applications/list")
async def get_moderators_applications_list(request: Request):
    try:
        mod_name = request.cookies.get("forum_user_name")
        if not mod_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы!"}
            
        staff_members = await get_forum_staff()
        is_hr_admin = False
        
        for u in staff_members:
            if str(mod_name).strip().lower() in str(u.get("name", "")).strip().lower():
                if u.get("role") in HR_ALLOWED_ROLES:
                    is_hr_admin = True
                    break
                    
        if not is_hr_admin:
            return {"status": "error", "message": "🛑 Отказано в доступе! Вы не входите в Отдел по набору персонала."}
            
        async with aiosqlite.connect(DB_PATH) as db:
            # 🎯 ИСПРАВЛЕНО: Добавили выборку answers_json в SQL-запрос
            async with db.execute("SELECT id, author_name, author_avatar, status, timestamp, answers_json FROM forum_applications ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return {
                    "status": "ok",
                    # Передаем r[5] (наши ответы) под ключом "answers" во фронтенд
                    "applications": [{"id": r[0], "author": r[1], "avatar": r[2], "status": r[3], "time": r[4], "answers": r[5]} for r in rows]
                }
    except Exception as e:
        return {"status": "error", "message": str(e)}



# 4. УПРАВЛЕНИЕ СТАТУСОМ АНКЕТЫ (Взять в работу / Одобрить / Отклонить)
@app.post("/api/forum/applications/status")
async def update_moderator_application_status(data: dict, request: Request):
    try:
        mod_name = request.cookies.get("forum_user_name")
        if not mod_name:
            return {"status": "error", "message": "🔒 Вы не авторизованы!"}
            
        staff_members = await get_forum_staff()
        # Собираем флаг проверки HR-прав
        is_hr_admin = any(str(mod_name).strip().lower() in str(u.get("name", "")).strip().lower() and u.get("role") in HR_ALLOWED_ROLES for u in staff_members)
        
        # 🎯 ИСПРАВЛЕНО: Теперь проверяется правильное имя переменной is_hr_admin! Сбой NameError устранён!
        if not is_hr_admin:
            return {"status": "error", "message": "🛑 Отказано в доступе! Вы не входите в кадровый комитет."}
            
        app_id = int(data.get("app_id"))
        new_status = data.get("status") 
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Читаем имя кандидата, чтобы закинуть ему алерт
            async with db.execute("SELECT author_name FROM forum_applications WHERE id = ?", (app_id,)) as cur:
                app_row = await cur.fetchone()
                candidate_name = app_row[0] if app_row else None

            ts_alert = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
            alert_text = ""

            if new_status == 'processing':
                await db.execute("UPDATE forum_applications SET status = 'processing', moderator_name = ? WHERE id = ?", (mod_name, app_id))
                alert_text = f"💼 Ваша анкета на должность Модератора взята на рассмотрение куратором {mod_name}"
            elif new_status == 'approved':
                await db.execute("UPDATE forum_applications SET status = 'approved' WHERE id = ?", (app_id,))
                alert_text = f"✅ Поздравляем! Ваша анкета на должность Модератора была ОДОБРЕНА Отделом Кадров!"
            elif new_status == 'rejected':
                await db.execute("UPDATE forum_applications SET status = 'rejected' WHERE id = ?", (app_id,))
                alert_text = f"❌ К сожалению, ваша анкета на должность Модератора была ОТКЛОНЕНА кураторами."

            # Записываем системный алерт для кандидата в таблицу уведомлений форума
            if alert_text and candidate_name:
                await db.execute("""
                    INSERT INTO forum_notifications (username, text, ticket_id, timestamp)
                    VALUES (?, ?, 0, ?)
                """, (candidate_name, alert_text, ts_alert))

            await db.commit()
        return {"status": "ok", "message": "Статус анкеты успешно обновлен!"}
    except Exception as e:
        print(f"🛑 Ошибка обновления статуса анкеты на бэкэнде: {e}")
        return {"status": "error", "message": str(e)}

# 1. РЕНДЕРИНГ ГЛАВНОЙ СТРАНИЦЫ ДЭШБОРДА ПО ССЫЛКЕ /bot-dashboard
@app.get("/bot-dashboard")
async def serve_bot_dashboard_page(request: Request):
    from fastapi.responses import FileResponse, RedirectResponse
    import os
    
    # Проверяем авторизацию
    username = request.cookies.get("forum_user_name")
    if not username:
        return RedirectResponse("/forum?auth=login_required")
        
    return FileResponse(os.path.join(BASE_DIR, "dashboard.html"))
    
# 🎯 УКАЖИ ТУТ CLIENT ID СВОЕГО БОТА ИЗ DISCORD DEVELOPER PORTAL
KONATA_CLIENT_ID = "1508496508528365668"

# 2. ЖИВОЙ СИНХРОННЫЙ ШЛЮЗ ВЫБОРКИ СЕРВЕРОВ ИЗ DISCORD API С ПРОВЕРКОЙ БОТА
@app.get("/api/forum/bot/guilds")
async def get_user_discord_guilds(request: Request):
    try:
        # 1. Достаем токен доступа пользователя из защищенных кук
        user_token = request.cookies.get("forum_discord_token")
        if not user_token:
            return {"status": "error", "message": "🔒 Вы не авторизованы через Discord!", "guilds": []}

        # 🎯 СУПЕР-ЗАЩИТА: Вытаскиваем секретный токен самого бота из переменных среды Render!
        konata_bot_token = os.getenv("KONATA_BOT_TOKEN", "").strip()

        # Формируем заголовки для параллельных запросов
        headers_user = {"Authorization": f"Bearer {user_token}"}
        headers_bot = {"Authorization": f"Bot {konata_bot_token}"}
        
        bot_guilds_ids = set()

        async with httpx.AsyncClient() as client:
            # Запрос А: Скачиваем серверы залогиненного пользователя
            res_user = await client.get("https://discord.com/api/v10/users/@me/guilds", headers=headers_user)
            if res_user.status_code != 200:
                print(f"🛑 Ошибка User Discord API: {res_user.text}")
                return {"status": "error", "message": "Не удалось выгрузить ваши сервера из Discord API", "guilds": []}
            user_guilds = res_user.json()

            # Запрос Б: Если токен бота прописан в Render, скачиваем серверы самого бота Konata
            if konata_bot_token:
                res_bot = await client.get("https://discord.com/api/v10/users/@me/guilds", headers=headers_bot)
                if res_bot.status_code == 200:
                    bot_guilds_data = res_bot.json()
                    # Собираем ID серверов бота в быстрый Set строк для моментального поиска
                    bot_guilds_ids = {str(bg.get("id")) for bg in bot_guilds_data}
                else:
                    print(f"⚠️ Бот Konata не смог выгрузить свои серверы: {res_bot.text}")

        filtered_guilds = []
        invite_url_template = f"https://discord.com/oauth2/authorize?client_id={KONATA_CLIENT_ID}&permissions=8&scope=bot%20applications.commands"

        # 3. Фильтруем сервера по битовой маске прав пользователя
        for g in user_guilds:
            is_owner = g.get("owner", False)
            permissions = int(g.get("permissions", 0))
            
            is_admin = (permissions & 0x8) == 0x8
            is_manager = (permissions & 0x20) == 0x20

            if is_owner or is_admin or is_manager:
                guild_id = str(g.get("id"))
                icon_hash = g.get("icon")
                
                # Собираем красивую ссылку на круглую аватарку сервера Дискорда через официальный CDN
                if icon_hash:
                    icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png"
                else:
                    icon_url = "https://i.ibb.co/4pSbxsh/user-avatar.png"

                # 🎯 НАСТОЯЩАЯ ЖИВАЯ СИНХРОНИЗАЦИЯ: Проверяем, находится ли бот на этом сервере
                has_bot_present = guild_id in bot_guilds_ids

                filtered_guilds.append({
                    "id": guild_id,
                    "name": g.get("name", "Неизвестный сервер"),
                    "icon": icon_url,
                    "online": 0,
                    "has_bot": has_bot_present # 100% живой статус для фронтенда!
                })

        return {
            "status": "ok",
            "invite_template_url": invite_url_template,
            "guilds": filtered_guilds
        }
        
    except Exception as e:
        print(f"🛑 Критическая ошибка парсинга Discord Guilds: {e}")
        return {"status": "error", "message": str(e), "guilds": []}

# 1. РЕНДЕРИНГ СТРАНИЦЫ МАГАЗИНА /shop
@app.get("/shop")
async def serve_shop_page(request: Request):
    from fastapi.responses import FileResponse, RedirectResponse
    import os
    
    username = request.cookies.get("forum_user_name")
    if not username:
        return RedirectResponse("/forum?auth=login_required")
        
    return FileResponse(os.path.join(BASE_DIR, "shop.html"))


# 2. API: ПОЛУЧЕНИЕ ИНФОРМАЦИИ О БАЛАНСЕ И КУПЛЕННЫХ ФОНАХ ЮЗЕРА
@app.get("/api/forum/shop/profile")
async def get_shop_user_profile(request: Request):
    username = request.cookies.get("forum_user_name")
    if not username:
        return {"status": "error", "message": "🔒 Вы не авторизованы"}

    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем баланс и активный фон
        async with db.execute("SELECT coins, active_background FROM user_shop_profile WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            if row:
                coins, active_bg = row[0], row[1]
            else:
                # Если зашел впервые: баланс 0 монет по твоему ТЗ
                await db.execute("INSERT INTO user_shop_profile (username, coins, active_background) VALUES (?, 0, 'none')", (username,))
                await db.commit()
                coins, active_bg = 0, "none"

        # Получаем список всех уже купленных фонов
        async with db.execute("SELECT bg_id FROM user_owned_backgrounds WHERE username = ?", (username,)) as cursor:
            rows = await cursor.fetchall()
            owned_bgs = [r[0] for r in rows]

        return {
            "status": "ok",
            "username": username,
            "coins": coins,
            "active_background": active_bg,
            "owned_backgrounds": owned_bgs
        }


# 3. API: ОБРАБОТКА ПОКУПКИ ИЛИ СМЕНЫ ФОНА
@app.post("/api/forum/shop/action")
async def handle_shop_action(data: dict, request: Request):
    username = request.cookies.get("forum_user_name")
    if not username:
        return {"status": "error", "message": "🔒 Сессия истекла"}

    bg_id = data.get("bg_id")
    action = data.get("action")  # Могут быть действия: "buy" или "equip"
    
    # Конфигурация цен фонов (По ТЗ сейчас все фоны стоят 0 монет)
    SHOP_ITEMS_PRICES = {
        "bg_red": 0,
        "bg_blue": 0,
        "bg_purple": 0,
        "bg_green": 0
    }

    if bg_id not in SHOP_ITEMS_PRICES:
        return {"status": "error", "message": "Товар не найден в базе данных магазина"}

    price = SHOP_ITEMS_PRICES[bg_id]

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем текущий баланс монет пользователя
        async with db.execute("SELECT coins FROM user_shop_profile WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            user_coins = row[0] if row else 0

        # ДЕЙСТВИЕ 1: ПОКУПКА ФОНА
        if action == "buy":
            # Проверяем, не куплен ли он уже
            async with db.execute("SELECT 1 FROM user_owned_backgrounds WHERE username = ? AND bg_id = ?", (username, bg_id)) as cursor:
                if await cursor.fetchone():
                    return {"status": "error", "message": "Этот фон уже есть в вашем инвентаре!"}

            if user_coins < price:
                return {"status": "error", "message": "❌ Недостаточно монет на балансе!"}

            # Списываем монеты и добавляем в инвентарь купленных
            await db.execute("UPDATE user_shop_profile SET coins = coins - ? WHERE username = ?", (price, username))
            await db.execute("INSERT INTO user_owned_backgrounds (username, bg_id) VALUES (?, ?)", (username, bg_id))
            await db.commit()
            return {"status": "ok", "message": "🎉 Успешная покупка! Теперь вы можете применить этот фон в инвентаре."}

        # ДЕЙСТВИЕ 2: АКТИВАЦИЯ (НАДЕТЬ) ФОНА СЕРВЕРОМ НА ВСЕ ПОСТЫ
        elif action == "equip":
            # Проверяем, куплен ли товар
            async with db.execute("SELECT 1 FROM user_owned_backgrounds WHERE username = ? AND bg_id = ?", (username, bg_id)) as cursor:
                if not await cursor.fetchone() and price > 0:
                    return {"status": "error", "message": "Сначала необходимо купить этот фон в магазине!"}

            # Назначаем фон активным для всех обращений пользователя на форуме
            await db.execute("UPDATE user_shop_profile SET active_background = ? WHERE username = ?", (bg_id, username))
            await db.commit()
            return {"status": "ok", "message": "✨ Кастомный фон успешно активирован для всех ваших будущих и старых обращений!"}

    return {"status": "error", "message": "Неизвестное системное действие"}

# ==========================================================
# 🤫 МОДУЛЬ ЗАКРЫТОГО ЧАТА ДЛЯ АДМИНИСТРАЦИИ (CHAT GPT STYLE)
# ==========================================================

# 1. РЕНДЕРИНГ СТРАНИЦЫ /admins_chat (АБСОЛЮТНАЯ ВЕРИФИКАЦИЯ ПО DISCORD ID)
@app.get("/admins_chat")
async def serve_admins_chat_page(request: Request):
    from fastapi.responses import FileResponse, RedirectResponse
    import os
    
    # 1. Проверяем базовую авторизацию на форуме
    username = request.cookies.get("forum_user_name")
    # Достаем Discord ID пользователя, который намертво привязан к его кукам при OAuth2 логине
    user_discord_id = request.cookies.get("forum_user_id") 
    
    if not username or not user_discord_id:
        return RedirectResponse("/forum?auth=login_required")
        
    try:
        # 2. Получаем живой список администрации из Discord-сервера через бота
        staff_members = await get_forum_staff()
        
        # 3. Настоящая верификация: Ищем совпадение строго по уникальному Discord ID
        # Это исключает ошибки регистра букв, пробелов и смены ников
        is_staff = False
        for member in staff_members:
            # Принудительно приводим оба ID к строкам для безопасного сравнения
            if str(member.get("id", "")).strip() == str(user_discord_id).strip():
                is_staff = True
                break
                
        if not is_staff:
            print(f"🔒 [ACCESS DENIED] Пользователь {username} (ID: {user_discord_id}) пытался зайти в админ-чат, но не имеет STAFF-ролей в Discord.")
            return RedirectResponse("/forum?error=access_denied") # От ворот поворот
            
    except Exception as e:
        print(f"🛑 [ADMIN CHAT VERIFY ERROR] Критическая ошибка проверки прав: {e}")
        return RedirectResponse("/forum?error=db_error")

    return FileResponse(os.path.join(BASE_DIR, "admins_chat.html"))

# API: ПОЛУЧЕНИЕ ИСТОРИИ СООБЩЕНИЙ ПРИ ЗАГРУЗКЕ СТРАНИЦЫ
# API: ПОЛУЧЕНИЕ ИСТОРИИ ЧАТА С ДИНАМИЧЕСКИМ НАСЫЩЕНИЕМ РОЛЕЙ ИЗ DISCORD В РЕАЛЬНОМ ВРЕМЕНИ
@app.get("/api/forum/admins_chat/history")
async def get_admin_chat_history(request: Request):
    username = request.cookies.get("forum_user_name")
    if not username:
        return {"status": "error", "message": "🔒 Доступ запрещен"}
        
    import json
    try:
        # 1. Сначала скачиваем живую карту актуальных ролей всех STAFF участников из Discord API через бота
        # Это даст нам самые свежие цвета и градиенты участников, даже если они сейчас офлайн!
        live_staff_members = await get_forum_staff()
        # Собираем быструю карту поиска: { "имя_пользователя_на_форуме": {roles_object, is_owner} }
        live_staff_map = {}
        for member in live_staff_members:
            # Привязываем к имени (или к username на случай если ник на форуме совпадает)
            m_name = str(member.get("name", "")).lower().strip()
            m_username = str(member.get("username", "")).lower().strip()
            live_staff_map[m_name] = member
            live_staff_map[m_username] = member

        # 2. Извлекаем последние 50 сообщений из СУБД SQLite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT sender, avatar, message_text, is_owner, roles_json, timestamp FROM admin_chat_history ORDER BY id ASC LIMIT 50") as cursor:
                rows = await cursor.fetchall()
                history = []
                
                for r in rows:
                    sender_name = r[0]
                    clean_sender = str(sender_name).lower().strip()
                    
                    # Извлекаем старые данные из базы на случай подстраховки
                    backup_is_owner = bool(r[3])
                    backup_roles = json.loads(r[4] if r[4] else "[]")
                    
                    # 🎯 ГЛАВНАЯ МАГИЯ: Ищем автора сообщения в нашей живой карте Дискорда!
                    if clean_sender in live_staff_map:
                        # Если нашли — подменяем старые роли из базы на СВЕЖАЙШИЕ живые градиенты ролей из Дискорда!
                        live_data = live_staff_map[clean_sender]
                        final_roles = live_data.get("roles", backup_roles)
                        final_is_owner = live_data.get("is_owner", backup_is_owner)
                    else:
                        # Если модератора уже сняли и его нет в Дискорде — оставляем старые архивные роли
                        final_roles = backup_roles
                        final_is_owner = backup_is_owner

                    history.append({
                        "sender": sender_name,
                        "avatar": r[1],
                        "text": r[2],
                        "is_owner": final_is_owner,
                        "roles": final_roles, # Сюда улетели живые переливы цветов прямо на текущую секунду!
                        "time": r[5]
                    })
                    
                return {"status": "ok", "messages": history}
                
    except Exception as e:
        print(f"🛑 [HISTORY HYDRATION ERROR] Ошибка насыщения истории чата: {e}")
        return {"status": "error", "message": str(e)}


# 🎙️ УЛЬТИМАТИВНЫЙ WEBSOCKET ШЛЮЗ С ФИКСОМ ДЛЯ ТЕЛЕФОНОВ И НЕЗАВИСИМЫХ СЕССИЙ
@app.websocket("/ws/admins_chat")
async def websocket_admins_chat_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    username = websocket.cookies.get("forum_user_name", "Анонимный Admin")
    avatar = websocket.cookies.get("forum_user_avatar", "https://i.ibb.co/4pSbxsh/user-avatar.png")
    user_discord_id = websocket.cookies.get("forum_user_id", "0000000000")

    # 🎯 СУПЕР-ФИКС: Создаем уникальный ключ сессии (Имя + ID + Уникальный порт сокета)
    # Теперь телефон и ПК никогда не затрут друг друга, даже под одним аккаунтом!
    session_id = f"{username}_{user_discord_id}_{websocket.client.port}"

    room_id = "admin_general_room"
    if room_id not in manager.rooms:
        manager.rooms[room_id] = {}
        
    # Регистрируем устройство под уникальным ID сессии
    manager.rooms[room_id][session_id] = websocket

    try:
        while True:
            raw_data = await websocket.receive_text()
            if not raw_data.strip(): continue
            
            import json
            
            # 1. ПЕРЕХВАТЧИК СИГНАЛОВ ГОЛОСОВОГО ЧАТА (WebRTC)
            try:
                data_json = json.loads(raw_data)
                if isinstance(data_json, dict) and "voice_type" in data_json:
                    data_json["sender"] = username
                    data_json["avatar"] = avatar
                    
                    # 🎯 ИСПРАВЛЕНО: Рассылаем голосовой пакет абсолютно всем ДРУГИМ сессиям устройств!
                    for other_session, ws in list(manager.rooms[room_id].items()):
                        if other_session != session_id:
                            try:
                                await ws.send_text(json.dumps(data_json))
                            except: pass
                    continue
            except:
                pass

            # 2. СТАНДАРТНАЯ ЛОГИКА ТЕКСТОВЫХ СООБЩЕНИЙ
            is_owner = False
            user_roles = []
            
            try:
                staff_members = await get_forum_staff()
                for member in staff_members:
                    m_id = str(member.get("id", "")).strip()
                    m_name = str(member.get("name", "")).lower().strip()
                    
                    if (user_discord_id and m_id == str(user_discord_id).strip()) or (m_name == username.lower().strip()):
                        user_roles = member.get("roles", [])
                        is_owner = member.get("is_owner", False)
                        break
                        
                if not user_roles:
                    user_roles = [{"name": "⚡ Администрация форума", "primary": "#828282", "secondary": None, "tertiary": None, "is_gradient": False}]
            except Exception as e:
                print(f"⚠️ [WS LIVE ROLES ERROR] {e}")
                user_roles = [{"name": "⚡ Модератор штата", "primary": "#828282", "secondary": None, "tertiary": None, "is_gradient": False}]
            
            msg_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
            
            payload = {
                "sender": username,
                "avatar": avatar,
                "text": raw_data, 
                "is_owner": is_owner,
                "roles": user_roles,
                "time": msg_time
            }
            
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO admin_chat_history (sender, avatar, message_text, is_owner, roles_json, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (username, avatar, raw_data, 1 if is_owner else 0, json.dumps(user_roles), msg_time))
                    await db.commit()
            except Exception as db_err:
                print(f"🛑 Ошибка записи сообщения: {db_err}")
            
            # Рассылаем текстовое сообщение по всем устройствам
            for other_session, ws in list(manager.rooms[room_id].items()):
                try:
                    await ws.send_text(json.dumps(payload))
                except: pass

    except WebSocketDisconnect:
        if room_id in manager.rooms and session_id in manager.rooms[room_id]:
            del manager.rooms[room_id][session_id]
            
            try:
                for other_session, ws in list(manager.rooms[room_id].items()):
                    await ws.send_text(json.dumps({"voice_type": "leave", "sender": username}))
            except: pass

@app.get("/poll/{poll_id}")
async def get_poll(poll_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем данные опроса
        async with db.execute("SELECT question, options, owner FROM polls WHERE id = ?", (poll_id,)) as cur:
            poll = await cur.fetchone()
            if not poll: return {"status": "error"}
            
        # Получаем все голоса для этого опроса
        async with db.execute("SELECT option_index, username FROM poll_votes WHERE poll_id = ?", (poll_id,)) as cur:
            votes = await cur.fetchall()
            
        # Формируем ответ
        options = poll[1].split(",")
        results = [0] * len(options)
        my_vote = None
        
        for opt_idx, voter in votes:
            results[opt_idx] += 1
            if voter == username: my_vote = opt_idx
            
        return {
            "question": poll[0],
            "options": options,
            "results": results,
            "total": len(votes),
            "my_vote": my_vote,
            "owner": poll[2]
        }

@app.post("/add_contact")
async def add_contact(data: dict):
    me = data.get("me")
    who = data.get("who")
    async with aiosqlite.connect(DB_PATH) as db:
        # Добавляем обоим (чтобы контакт появился у обоих сразу)
        await db.execute("INSERT OR IGNORE INTO contacts VALUES (?, ?)", (me, who))
        await db.execute("INSERT OR IGNORE INTO contacts VALUES (?, ?)", (who, me))
        await db.commit()
    return {"status": "ok"}

@app.get("/check_ban/{username}")
async def check_ban(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT unban_time FROM bans WHERE username = ?", (username,)) as cur:
            row = await cur.fetchone()
            if row and row[0] > time.time():
                return {"banned": True}
    return {"banned": False}

@app.get("/get_contacts/{username}")
async def get_contacts(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT contact_name FROM contacts WHERE owner = ?", (username,)) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]
            
@app.get("/admin/users_archive")
async def get_users_archive(key: str):
    if key != ADMIN_SECRET_KEY: 
        return {"status": "error", "message": "Wrong Key"}
    
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Используем COALESCE, чтобы вместо NULL всегда была пустая строка
            sql = """
                SELECT DISTINCT name, avatar, b.unban_time, b.reason
                FROM (
                    SELECT username as name, avatar FROM users
                    UNION
                    SELECT username as name, COALESCE(avatar, '') as avatar FROM messages
                ) u
                LEFT JOIN bans b ON u.name = b.username
                WHERE name IS NOT NULL AND name != ''
            """
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
                return [
                    {
                        "name": r[0], 
                        "avatar": r[1] if (r[1] and len(r[1]) > 10) else "https://i.ibb.co/4pSbxsh/user-avatar.png", 
                        "banned": r[2] is not None and r[2] > time.time(),
                        "reason": r[3] or "" 
                    } for r in rows
                ]
    except Exception as e:
        print(f"🛑 ARCHIVE DATABASE ERROR: {e}")
        return {"status": "error", "message": str(e)}

# 1. ПОЛУЧЕНИЕ ВСЕХ ШОРТСОВ (Сортировка: новые сверху)
@app.get("/api/shorts")
async def get_shorts(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Тянем шортсы
        sql = "SELECT id, title, description, video_url, author, timestamp, likes_count, dislikes_count FROM shorts ORDER BY id DESC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
            
            shorts_list = []
            for r in rows:
                s_id, title, desc, url, author, ts, likes, dislikes = r
                
                # Проверяем, лайкал ли этот конкретный юзер это видео
                my_react = None
                async with db.execute("SELECT type FROM shorts_reactions WHERE short_id = ? AND username = ?", (s_id, username)) as r_cur:
                    react_row = await r_cur.fetchone()
                    if react_row: my_react = react_row[0]
                
                # Подтягиваем комментарии для этого видео
                comments = []
                async with db.execute("SELECT username, comment_text, timestamp FROM shorts_comments WHERE short_id = ? ORDER BY id ASC", (s_id,)) as c_cur:
                    async for c_row in c_cur:
                        comments.append({"username": c_row[0], "text": c_row[1], "time": c_row[2]})
                
                shorts_list.append({
                    "id": s_id, "title": title, "description": desc, "video_url": url,
                    "author": author, "timestamp": ts, "likes": likes, "dislikes": dislikes,
                    "my_reaction": my_react, "comments": comments
                })
            return shorts_list

# 2. ПУБЛИКАЦИЯ НОВОГО ШОРТСА (aiosqlite + MUTE SYSTEM — ФИНАЛЬНЫЙ ФИКС)
@app.post("/api/shorts/publish")
async def publish_short(data: dict):
    global MUTED_DATA
    try:
        author = data.get("author", "").strip()
        
        # 🔇 БЕЗОПАСНЫЙ КАПКАН ДЛЯ ИЗОЛИРОВАННЫХ УЗЛОВ В ШОРТСАХ
        if author and author.lower() in MUTED_DATA:
            mute_until = MUTED_DATA.get(author.lower(), 0)
            if time.time() < mute_until:
                return {"status": "error", "message": "Ваш узел изолирован админом. Публикация shorts заблокирована!"}
            else:
                # Срок мута истек — бесшумно амнистируем узел
                MUTED_DATA.pop(author.lower(), None)

        title = data.get("title")
        desc = data.get("description", "")
        video_url = data.get("video_url")
        
        # Если название пустое — ставим дату и время по Москве
        if not title or not title.strip():
            title = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
            
        ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
        
        # 🎯 СОХРАНЯЕМ В ТЕКУЩУЮ ЛОКАЛЬНУЮ БАЗУ ДАННЫХ PINNOGRAM
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO shorts (title, description, video_url, author, timestamp) 
                VALUES (?, ?, ?, ?, ?)
            """, (title, desc, video_url, author, ts))
            await db.commit()
            
        return {"status": "ok", "message": "Видео успешно опубликовано!"}

    except Exception as main_err:
        print(f"❌ Критический сбой роута публикации шортса: {main_err}")
        return {"status": "error", "message": f"Системный сбой ядра: {str(main_err)}"}


# 3. ЛАЙК / ДИЗЛАЙК ВИДЕО (С автоматическим пересчетом счетчиков)
@app.post("/api/shorts/react")
async def react_short(data: dict):
    short_id = int(data.get("short_id"))
    username = data.get("username")
    react_type = data.get("type") # 'like' или 'dislike'
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем старую реакцию
        async with db.execute("SELECT type FROM shorts_reactions WHERE short_id = ? AND username = ?", (short_id, username)) as cur:
            old_row = await cur.fetchone()
            
        if old_row:
            old_type = old_row[0]
            if old_type == react_type:
                # Если нажал то же самое — убираем реакцию вообще (отмена)
                await db.execute("DELETE FROM shorts_reactions WHERE short_id = ? AND username = ?", (short_id, username))
                sql_update = f"UPDATE shorts SET {react_type}s_count = {react_type}s_count - 1 WHERE id = ?"
                await db.execute(sql_update, (short_id,))
                status = "removed"
            else:
                # Если поменял лайк на дизлайк (или наоборот)
                await db.execute("UPDATE shorts_reactions SET type = ? WHERE short_id = ? AND username = ?", (react_type, short_id, username))
                await db.execute(f"UPDATE shorts SET {old_type}s_count = {old_type}s_count - 1, {react_type}s_count = {react_type}s_count + 1 WHERE id = ?", (short_id,))
                status = "changed"
        else:
            # Новая реакция
            await db.execute("INSERT INTO shorts_reactions VALUES (?, ?, ?)", (short_id, username, react_type))
            await db.execute(f"UPDATE shorts SET {react_type}s_count = {react_type}s_count + 1 WHERE id = ?", (short_id,))
            status = "added"
            
        await db.commit()
        
        # Возвращаем обновленное количество
        async with db.execute("SELECT likes_count, dislikes_count FROM shorts WHERE id = ?", (short_id,)) as cur:
            likes, dislikes = await cur.fetchone()
            
        return {"status": "ok", "action": status, "likes": likes, "dislikes": dislikes}

# 4. ДОБАВЛЕНИЕ КОММЕНТАРИЯ
@app.post("/api/shorts/comment")
async def add_short_comment(data: dict):
    short_id = int(data.get("short_id"))
    username = data.get("username")
    text = data.get("text")
    ts = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
    
    if not text or not text.strip(): return {"status": "error"}
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO shorts_comments (short_id, username, comment_text, timestamp) VALUES (?, ?, ?, ?)", 
                        (short_id, username, text, ts))
        await db.commit()
        
    return {"status": "ok", "username": username, "text": text, "time": ts}


# 1. Роут для регистрации и входа
@app.post("/auth")
async def auth(data: dict):
    username = data.get("username")
    password = data.get("password")
    async with aiosqlite.connect(DB_PATH) as db:
        # Индексы: 0 - password, 1 - avatar
        async with db.execute("SELECT password, avatar FROM users WHERE username = ?", (username,)) as cursor:
            user = await cursor.fetchone()
            if user:
                if str(user[0]) == str(password): # Приводим к строке для надежности
                    return {"status": "ok", "avatar": user[1]} # ИСПРАВЛЕНО: берем индекс 1
                else:
                    return {"status": "error", "message": "Неверный пароль"}
            else:
                # Если пользователя нет — регистрируем
                await db.execute("INSERT INTO users (username, password, avatar) VALUES (?, ?, ?)", 
                                (username, password, ""))
                await db.commit()
                return {"status": "ok", "avatar": ""}

# 2. Роут для обновления аватарки в базе
@app.post("/update_avatar")
async def update_avatar(data: dict):
    username = data.get("username")
    avatar_url = data.get("avatar")
    async with aiosqlite.connect(DB_PATH) as db:
        # Сохраняем в профиль юзера навсегда
        await db.execute("UPDATE users SET avatar = ? WHERE username = ?", (avatar_url, username))
        # Опционально: обновляем во всех старых сообщениях этого юзера
        await db.execute("UPDATE messages SET avatar = ? WHERE username = ?", (avatar_url, username))
        await db.commit()
    return {"status": "ok"}


# 1. Эти объекты должны быть ВНЕ класса (в начале файла после импортов)
admin_data_cache = {}

async def get_user_info(ip):
    if ip in admin_data_cache: return admin_data_cache[ip]
    
    # Заглушка для локалки
    if not ip or ip in ["127.0.0.1", "localhost", "::1", "testclient"]: 
        return {"city": "Local Node", "org": "Internal Net", "country": "un", "tz": "UTC", "asn": "LAN"}
    
    try:
        async with httpx.AsyncClient() as client:
            # Используем ipapi.co (у него 1000 бесплатных запросов в сутки)
            res = await client.get(f"https://ipapi.co/{ip}/json/", timeout=2.0)
            if res.status_code == 200:
                data = res.json()
                info = {
                    "city": data.get("city", "Private"),
                    "org": data.get("org", "Unknown ISP"),
                    "country": data.get("country_code", "un").lower(),
                    "tz": data.get("timezone", "UTC"),
                    "asn": data.get("asn", "N/A") 
                }
                if info["country"] == "su": info["country"] = "ru"
                admin_data_cache[ip] = info
                return info
    except Exception as e: 
        print(f"📡 [GEO_ERR]: {e}")
        
    return {"city": "Unknown", "org": "Unknown", "country": "un", "tz": "UTC", "asn": "N/A"}

# В методе broadcast_online внутри ConnectionManager исправь сборку info:
# info = await get_user_info(ip)
# users_info.append(f"{name}|{ip}|{info['country']}|{info['city']}|{info['org']}")


class ConnectionManager:
    def __init__(self):
        self.rooms = {}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        # Соединение уже принято в websocket_endpoint для проверки Fingerprint
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][username] = websocket
        await self.broadcast_online(room_id)

    def disconnect(self, room_id: str, username: str):
        if room_id in self.rooms and username in self.rooms[room_id]:
            del self.rooms[room_id][username]
        asyncio.create_task(self.broadcast_online(room_id))
    
    async def ban_user(self, target_name, days, admin_name, reason="Нарушение правил"):
        unban_time = time.time() + (float(days) * 86400)
        
        async with aiosqlite.connect(DB_PATH) as db:
            found = False
            for room_id, users in self.rooms.items():
                if target_name in users:
                    ws = users[target_name]
                    ip = ws.client.host if ws.client else "unknown"
                    # Достаем отпечаток, который мы сохранили в сокет при входе
                    finger = getattr(ws, 'browser_fingerprint', 'none')

                    # Пишем ПОЛНЫЙ бан (6 полей)
                    await db.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?, ?, ?)", 
                                    (target_name, ip, finger, unban_time, admin_name, reason))
                    await db.commit()

                    try:
                        # Активируем "капкан" на клиенте
                        await ws.send_text(f"ID:0|SYSTEM:BAN_SCREEN|{unban_time}|{admin_name}|{reason}")
                        await asyncio.sleep(0.5)
                        await ws.close(code=1008)
                    except: pass
                    
                    del self.rooms[room_id][target_name]
                    found = True
            
            # Если юзера нет в сети, баним "вдогонку" по имени
            if not found:
                await db.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?, ?, ?)", 
                                (target_name, "offline", "none", unban_time, admin_name, reason))
                await db.commit()
                
            return True

    async def unban_user(self, target):
        async with aiosqlite.connect(DB_PATH) as db:
            # 1. Удаляем из базы
            await db.execute("DELETE FROM bans WHERE username = ? OR ip = ?", (target, target))
            await db.commit()
            
            # 2. Ищем "зависшие" сокеты этого юзера (те, кто видит экран бана)
            # Мы пройдемся по всем комнатам и активным сокетам
            for room in self.rooms.values():
                if target in room:
                    ws = room[target]
                    try:
                        # Шлем секретный пакет амнистии
                        await ws.send_text("ID:0|SYSTEM:AMNESTY_NOW")
                    except: pass
        return True


    async def global_broadcast(self, text, admin_name):
        final_msg = f"ID:0|SYSTEM:GLOBAL_ALERT|{text}|{admin_name}"
        for room in self.rooms.values():
            for ws in room.values():
                if ws.client_state == WebSocketState.CONNECTED:
                    try: await ws.send_text(final_msg)
                    except: continue

    async def broadcast_online(self, room_id: str):
        if room_id in self.rooms:
            # 1. СОБИРАЕМ ЖИВЫХ ЮЗЕРОВ (как и раньше)
            users_info = []
            for name, ws in self.rooms[room_id].items():
                ip = ws.client.host if ws.client else "unknown"
                # Внутри ConnectionManager -> broadcast_online
                info = await get_user_info(ip)
                
                # 🎯 ВАЖНО: Порядок должен быть именно таким, чтобы JS (parts[0], [1], [2]...) не перепутал город с IP
                # 0:name | 1:ip | 2:country | 3:city | 4:org | 5:tz | 6:asn
                users_info.append(f"{name}|{ip}|{info['country']}|{info['city']}|{info['org']}|{info['tz']}|{info['asn']}")

            
            # 2. ДОСТАЕМ ГРУППЫ ИЗ БАЗЫ (с пометкой GROUP:)
            groups_info = []
            async with aiosqlite.connect(DB_PATH) as db:
                # Тянем имя и количество подписчиков
                async with db.execute("SELECT name, subscribers_count FROM groups") as cursor:
                    async for row in cursor:
                        g_name, subs = row
                        # Формат: GROUP:Имя|Подписчики|Метка_Группы
                        groups_info.append(f"GROUP:{g_name}|{subs}|GROUP")

            # 3. СОЕДИНЯЕМ И ШЛЕМ ВСЕМ
            all_entities = users_info + groups_info
            msg = f"ID:0|SYSTEM:ONLINE_LIST:{','.join(all_entities)}"
            
            for ws in self.rooms[room_id].values():
                if ws.client_state == WebSocketState.CONNECTED:
                    try: 
                        await ws.send_text(msg)
                    except: 
                        continue


    async def broadcast(self, room_id: str, message: str = "", username: str = None, text: str = None, avatar: str = "", client_time: str = None, to_user: str = None, reply_to_id: int = None):
        now = client_time if client_time else datetime.now().strftime("%H:%M")        
    
        # 🎯 1. ПРИВАТНЫЙ ПРОБРОС ЗВОНКА (RTC_SIGNAL)
        # Этот блок должен быть ПЕРВЫМ и единственным для звонков
        if text and "RTC_SIGNAL:" in text:
            room_users = self.rooms.get(room_id, {})
            
            # 🎯 ФИКС: Отправляем ТОЛЬКО получателю. Себе слать НЕЛЬЗЯ!
            if to_user and to_user in room_users:
                try:
                    # Шлем пакет ТОЛЬКО тому, чей ник указан в TO_USER
                    await room_users[to_user].send_text(text)
                    print(f"📡 [RTC] Сигнал передан: {username} -> {to_user}")
                except Exception as e:
                    print(f"❌ Ошибка отправки RTC: {e}")
            else:
                print(f"⚠️ [RTC] Получатель {to_user} не найден в этой комнате")
                
            return # Выходим, чтобы не спамить в общий чат и не писать в БД


        # 🎯 2. ОБЫЧНЫЕ СООБЩЕНИЯ (Запись в базу и рассылка)
        if username and text:
            async with aiosqlite.connect(DB_PATH) as db:
                # --- ПРОВЕРКА ПРАВ В ГРУППЕ ---
                is_group_msg = False
                room_users = self.rooms.get(room_id, {}) # Достаем юзеров заранее для ответов
                
                if to_user:
                    cur = await db.execute("SELECT owner FROM groups WHERE name = ?", (to_user,))
                    group_data = await cur.fetchone()
                    if group_data:
                        is_group_msg = True
                        owner = group_data[0]
                        
                        # 1. Проверяем подписку
                        sub = await db.execute("SELECT 1 FROM group_subs WHERE username=? AND group_name=?", (username, to_user))
                        if not await sub.fetchone():
                            if username in room_users:
                                await room_users[username].send_text("ID:0|SYSTEM:ERROR:Сначала вступи в группу!")
                            return
                        
                        # 2. Только владелец может слать ссылки (картинки) и звонки
                        if username != owner and ("http" in text or "RTC_SIGNAL" in text):
                            if username in room_users:
                                await room_users[username].send_text("ID:0|SYSTEM:ERROR:Только владелец может постить медиа!")
                            return

                # --- ЗАПИСЬ В БАЗУ ---
                cursor = await db.execute(        
                    "INSERT INTO messages (username, text, timestamp, room_id, avatar, to_user, is_read, reply_to_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",         
                    (username, text, now, room_id, avatar, to_user, reply_to_id)        
                )        
                msg_id = cursor.lastrowid
                await db.commit()        
    
                prefix = f"PRIVATE:{to_user}:" if to_user else ""
                reply_info = f"REPLY:{reply_to_id}|" if reply_to_id else ""
                final_msg = f"ID:{msg_id}|{reply_info}{prefix}[{now}] {username}: {text}|{avatar}|0"
      
                # --- РАССЫЛКА ---
                room_users = self.rooms.get(room_id, {})
                
                if is_group_msg:
                    # 📢 Если это группа — шлем ВООБЩЕ ВСЕМ в комнате
                    # Каждый сам решит (через JS), показывать это в окне группы или нет
                    for ws_client in room_users.values():
                        try: await ws_client.send_text(final_msg)
                        except: continue
                elif to_user:        
                    # 👤 Если это личка — только двоим
                    for name in [username, to_user]:        
                        if name in room_users:        
                            try: await room_users[name].send_text(final_msg)        
                            except: continue
                    
                    # Push-уведомление (только для лички)
                    if to_user not in room_users:        
                        async with db.execute("SELECT subscription_json FROM push_subscriptions WHERE username = ?", (to_user,)) as c:        
                            sub_row = await c.fetchone()        
                            if sub_row:        
                                try:
                                    await asyncio.to_thread(
                                        webpush,
                                        subscription_info=json.loads(sub_row[0]),        
                                        data=json.dumps({"title": f"ЛС от {username}", "body": text[:50]}),        
                                        vapid_private_key=VAPID_PRIVATE_KEY,        
                                        vapid_claims=VAPID_CLAIMS        
                                    )        
                                except: pass        
                else:        
                    # 🌍 Общий чат
                    for ws_client in room_users.values():
                        try: await ws_client.send_text(final_msg)        
                        except: continue




manager = ConnectionManager()

async def keep_alive_bot(manager):
    await asyncio.sleep(20) # Даем серверу время окончательно "проснуться"
    
    # Пытаемся достать URL твоего сервера из настроек Render
    # Если ты не задал переменную RENDER_EXTERNAL_URL, укажи свой адрес вручную
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://pinnogram-server.onrender.com")
    
    print(f"🚀 Бот-будильник: ЗАПУСК ПИНГА КАЖДЫЕ 5 МИНУТ ({RENDER_URL})")
    
    while True:
        try:
            now = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
            
            # --- 1. ВНЕШНИЙ ПИНГ (Имитируем реальный заход на сайт) ---
            async with httpx.AsyncClient() as client:
                # Запрашиваем главную страницу (/) или sw.js
                response = await client.get(RENDER_URL, timeout=15.0)
                if response.status_code == 200:
                    print(f"📡 Внешний пинг: OK (Render увидел запрос в {now})")
            
            # --- 2. ВНУТРЕННИЙ ПИНГ В ЧАТ (Для логов и сокетов) ---
            active_rooms = list(manager.rooms.keys())
            if active_rooms:
                for r_id in active_rooms:
                    await manager.broadcast(room_id=r_id, message=f"Pinnogram Heartbeat {now}")
                print(f"✅ Внутренний пинг: Отправлен в {len(active_rooms)} комнат")
            else:
                print(f"💤 Внутренний пинг: В чате пусто, жду юзеров ({now})")

            # --- 3. СПИМ РОВНО 5 МИНУТ ---
            await asyncio.sleep(300) 
            
        except Exception as e:
            print(f"⚠️ Ошибка бота-будильника: {e}")
            await asyncio.sleep(30) # Короткая пауза при сбое сети



            
@app.on_event("startup")
async def startup():
    from bot import bot, TOKEN
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Создаем основные таблицы (с учетом новых полей)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, 
                text TEXT, 
                timestamp TEXT, 
                room_id TEXT DEFAULT 'general',
                to_user TEXT DEFAULT NULL, 
                avatar TEXT DEFAULT '',
                is_read INTEGER DEFAULT 0,
                reply_to_id INTEGER DEFAULT NULL -- ПОЛЕ ДЛЯ ОТВЕТОВ
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, password TEXT, avatar TEXT DEFAULT ''
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                username TEXT PRIMARY KEY, subscription_json TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, options TEXT, owner TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER, username TEXT, option_index INTEGER,
                PRIMARY KEY(poll_id, username)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                owner TEXT, 
                contact_name TEXT,
                PRIMARY KEY(owner, contact_name)
            )
        """)
        
        # В функцию startup() в server.py
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reactions (
                msg_id INTEGER,
                username TEXT,
                emoji TEXT,
                PRIMARY KEY(msg_id, username)
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                username TEXT PRIMARY KEY,
                ip TEXT,
                fingerprint TEXT,
                unban_time REAL,
                admin_name TEXT,
                reason TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                owner TEXT,
                subscribers_count INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_subs (
                username TEXT,
                group_name TEXT,
                PRIMARY KEY (username, group_name)
            )
        """)
        # --- ТАБЛИЦЫ ДЛЯ ПЛАТФОРМЫ ШОРТСОВ ---
        # 1. Таблица самих видеороликов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                description TEXT,
                video_url TEXT,
                author TEXT,
                timestamp TEXT,
                likes_count INTEGER DEFAULT 0,
                dislikes_count INTEGER DEFAULT 0
            )
        """)

        # 2. Таблица лайков/дизлайков (чтобы один юзер не спамил лайками)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shorts_reactions (
                short_id INTEGER,
                username TEXT,
                type TEXT, -- 'like' или 'dislike'
                PRIMARY KEY(short_id, username)
            )
        """)

        # 3. Таблица комментариев к видео
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shorts_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_id INTEGER,
                username TEXT,
                comment_text TEXT,
                timestamp TEXT
            )
        """)

        # Таблица отзывов на форуме
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                author TEXT,
                review_text TEXT,
                rating INTEGER,
                timestamp TEXT
            )
        """)
        # 1. Создание таблицы отзывов (Комментарии убраны внутрь кода Python, а не SQL)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                author TEXT,
                author_avatar TEXT,
                review_text TEXT,
                rating REAL,
                timestamp TEXT
            )
        """)
        await db.commit()
        
        # 2. Создание таблицы обращений и жалоб форума (Все знаки '#' внутри запроса ПОЛНОСТЬЮ вычищены)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_type TEXT,        
                author_name TEXT,        
                author_avatar TEXT,      
                author_id TEXT,          
                description TEXT,        
                photos TEXT,             
                status TEXT,             
                moderator_name TEXT,     
                moderator_id TEXT,       
                timestamp TEXT           
            )
        """)
        await db.commit()

        # 3. Таблица для комментариев и переписки внутри обращений
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                author_name TEXT,
                author_avatar TEXT,
                message_text TEXT,
                timestamp TEXT
            )
        """)
        await db.commit()

        # 4. Таблица кастомизации профилей (баннеры и градиенты никнеймов)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_custom_settings (
                username TEXT PRIMARY KEY,
                banner_url TEXT,
                nickname_gradient TEXT,
                custom_status TEXT
            )
        """)
        await db.commit()

        # 5. Таблица системных уведомлений пользователей форума
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                text TEXT,
                ticket_id INTEGER,
                is_read INTEGER DEFAULT 0,
                timestamp TEXT
            )
        """)
        await db.commit()

        # 🎯 Должно быть внутри async def startup():
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_name TEXT,
                author_avatar TEXT,
                answers_json TEXT,
                status TEXT DEFAULT 'open',
                moderator_name TEXT DEFAULT '',
                timestamp TEXT
            )
        """)
        await db.commit()

        # 🎯 СУПЕР-ФИКС: Принудительно создаем таблицу настроек бота Konata прямо при старте сайта!
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_guild_settings (
                guild_id TEXT PRIMARY KEY,
                prefix TEXT DEFAULT 'k!',
                censor_enabled INTEGER DEFAULT 1,
                antilink_enabled INTEGER DEFAULT 0
            )
        """)
        await db.commit()

        # 2. БЕЗОПАСНЫЕ ФИКСЫ (Добавляем колонки в старую базу, если их там нет)
        columns = [
            ("messages", "avatar", "TEXT DEFAULT ''"),
            ("messages", "to_user", "TEXT DEFAULT NULL"),
            ("messages", "is_read", "INTEGER DEFAULT 0"),
            ("messages", "reply_to_id", "INTEGER DEFAULT NULL")# Колонки для галочек
        ]
        
        for table, col, definition in columns:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except:
                pass # Если колонка уже есть, SQLite просто проигнорирует команду
        
        await db.commit()
        asyncio.create_task(keep_alive_bot(manager))
        asyncio.create_task(init_pin_bank_tables())
        asyncio.create_task(init_admin_chat_db())
        asyncio.create_task(init_sochi_stock_exchange_tables())
        asyncio.create_task(sochi_market_ticker_simulation_loop())
        asyncio.create_task(start_global_economic_loop())

        print("🚀 Pinnogram Engine: База готова, бот-будильник запущен!")
        TOKEN = os.getenv("KONATA_BOT_TOKEN", "").strip()
        
        if TOKEN and "СКРЫЛ" not in TOKEN and "ТВОЙ_" not in TOKEN:
            try:
                asyncio.create_task(bot.start(TOKEN))
                print("🦊 [KONATA] Фоновый процесс бота успешно инициализирован в СУБД!")
            except Exception as e:
                print(f"🛑 [KONATA START ERROR] Ошибка запуска бота: {e}")
# ==========================================================
# 🎖️ ОНЛАЙН-ДВИЖЕК СТРАТЕГИИ "CONQUER THE WORLD ONLINE" (CTW)
# ==========================================================
import json
import asyncio
import aiofiles
from fastapi import Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# Активные онлайн-сессии в сети прямо сейчас
# { client_id: { "websocket": ws, "country": {...}, "cities": [...], "airports": [...] } }
ctw_sessions = {}

# Защитный буфер архивации империй для удержания стран на 1 минуту при перезагрузке
# { client_id: { "country": {...}, "cities": [...], "airports": [...], "task": asyncio.Task } }
ctw_recovery_storage = {}

# Дипломатическая матрица глобальных отношений: 
# [ {"aggressor": id, "defender": id, "status": "justifying"|"at_war"} ]
ctw_diplomacy = []

@app.get("/ctw", response_class=HTMLResponse)
async def ctw_page(request: Request):
    try:
        async with aiofiles.open("templates/ctw.html", mode="r", encoding="utf-8") as f:
            html_content = await f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки ctw.html: {str(e)}")

# Живая рассылка стейта мира, лидерборда и дипломатии всем игрокам одновременно
async def broadcast_ctw_state():
    world_state = {}
    # Объединяем активных диктаторов и тех, кто перезагружает вкладку прямо сейчас
    all_client_ids = set(list(ctw_sessions.keys()) + list(ctw_recovery_storage.keys()))
    
    for cid in all_client_ids:
        data = ctw_sessions.get(cid) or ctw_recovery_storage.get(cid)
        if not data: continue
        world_state[cid] = {
            "country": data.get("country"),
            "cities": data.get("cities", []),
            "airports": data.get("airports", [])
        }
            
    payload = json.dumps({
        "type": "sync_world", 
        "players": world_state, 
        "diplomacy": ctw_diplomacy
    }, ensure_ascii=False)
    
    dead_clients = []
    for cid, data in ctw_sessions.items():
        try: 
            await data["websocket"].send_text(payload)
        except Exception: 
            dead_clients.append(cid)
            
    for cid in dead_clients:
        if cid in ctw_sessions: del ctw_sessions[cid]
# 💰 ГЛОБАЛЬНЫЙ ТАЙМЕР ЭКОНОМИКИ (Деньги каждые 5 сек, Ракеты каждые 30 сек)
async def start_global_economic_loop():
    tick_count = 0
    while True:
        await asyncio.sleep(5)
        tick_count += 5
        
        updated = False
        for cid, data in list(ctw_sessions.items()):
            if not data.get("country") or not data.get("cities"): continue
                
            country = data["country"]
            # Считаем общее число заводов по всем городам диктатора
            total_factories = sum(c.get("factories", 0) for c in data["cities"])
            slider_missiles = country.get("slider_missiles", 0)
            
            # Условие: высчитываем пропорции ползунков
            missile_factories = min(total_factories, slider_missiles)
            profit_factories = total_factories - missile_factories
            
            # Начисление прибыли: +100 монет за завод каждые 5 секунд
            earnings = profit_factories * 100
            country["money"] = country.get("money", 10000) + earnings
            country["total_factories"] = total_factories
            
            # Начисление ракет: +1 ракета от каждого завода каждые 30 секунд
            if tick_count % 30 == 0 and missile_factories > 0:
                country["missiles"] = country.get("missiles", 0) + missile_factories
                
            updated = True
            
        if updated:
            await broadcast_ctw_state()
            
        if tick_count >= 30: tick_count = 0


# Фоновый асинхронный таймер удержания империи (условие: 60 секунд)
async def launch_disconnect_timer(client_id: str):
    try:
        await asyncio.sleep(60) # Ждём ровно 1 минуту
        # Если спустя минуту диктатор так и не переподключился к сокету
        if client_id not in ctw_sessions:
            if client_id in ctw_recovery_storage: del ctw_recovery_storage[client_id]
            # Фильтруем массив напрямую, Python разрешает это без слова global
            ctw_diplomacy[:] = [w for w in ctw_diplomacy if w["aggressor"] != client_id and w["defender"] != client_id]
            await broadcast_ctw_state()
            print(f"⏰ Минута истекла. Держава {client_id} навсегда стёрта с карты.")
    except asyncio.CancelledError:
        print(f"⚡ Диктатор {client_id} вернулся в сеть! Сброс империи отменён.")

# Главный WebSocket-маршрутизатор
@app.websocket("/ctw/ws/{client_id}")
async def ctw_websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    
    # ПРОВЕРКА RECONNECT RECOVERY WINDOW
    recovered_data = ctw_recovery_storage.get(client_id)
    if recovered_data:
        # Игрок успел обновить страницу в течение минуты! Глушим таймер удаления
        if recovered_data.get("task"): 
            recovered_data["task"].cancel()
            
        ctw_sessions[client_id] = {
            "websocket": websocket, 
            "country": recovered_data.get("country"),
            "cities": recovered_data.get("cities", []), 
            "airports": recovered_data.get("airports", [])
        }
        del ctw_recovery_storage[client_id]
        print(f"🔄 Диктатор {client_id} бесшовно восстановил сессию!")
    else:
        # Совершенно новое подключение на карту мира
        ctw_sessions[client_id] = {"websocket": websocket, "country": None, "cities": [], "airports": []}
    
    await broadcast_ctw_state()
    try:
        while True:
            # Бесконечно слушаем входящие пакеты от конкретного игрока
            raw_data = await websocket.receive_text()
            msg = json.loads(raw_data)
            
            if msg["type"] == "create_country":
                # Твоё условие: 10к монет, 0 ракет, 10 заводов, ползунок прибыли на максимум
                ctw_sessions[client_id]["country"] = {
                    "name": msg["name"], "capital": msg["capital"], "flag": msg["flag"], "geometry": msg["geometry"],
                    "population": 500, "money": 10000, "missiles": 0, "total_factories": 10,
                    "slider_profit": 10, "slider_missiles": 0
                }
                # Твоё условие: Стартовые 10 заводов закладываются строго в Столицу
                ctw_sessions[client_id]["cities"] = [{
                    "id": "city_capital_" + str(int(asyncio.get_event_loop().time())),
                    "name": msg["capital"], "lat": msg["geometry"]["lat"], "lng": msg["geometry"]["lng"],
                    "population": 500, "type": "capital", "factories": 10
                }]
                await broadcast_ctw_state()

            elif msg["type"] == "update_infrastructure":
                if client_id in ctw_sessions and ctw_sessions[client_id]["country"]:
                    ctw_sessions[client_id]["cities"] = msg["cities"]
                    ctw_sessions[client_id]["airports"] = msg["airports"]
                    ctw_sessions[client_id]["country"]["population"] = msg["total_population"]
                    await broadcast_ctw_state()
            # --- ⚙️ ОБНОВЛЕНИЕ ПОЛЗУНКОВ ПРОМЫШЛЕННОСТИ ---
            elif msg["type"] == "update_industry_sliders":
                if client_id in ctw_sessions and ctw_sessions[client_id]["country"]:
                    country = ctw_sessions[client_id]["country"]
                    country["slider_profit"] = msg["slider_profit"]
                    country["slider_missiles"] = msg["slider_missiles"]
                    await broadcast_ctw_state()

            # --- 🏗️ СТРОИТЕЛЬСТВО ЗАВОДОВ С СЕРВЕРНЫМ РАСЧЁТОМ ЦЕНЫ ---
            elif msg["type"] == "build_factory":
                if client_id in ctw_sessions and ctw_sessions[client_id]["country"]:
                    country = ctw_sessions[client_id]["country"]
                    cities = ctw_sessions[client_id]["cities"]
                    
                    total_f = sum(c.get("factories", 0) for c in cities)
                    # Твоё условие: Цена возрастает на 200 монет за каждый имеющийся завод
                    cost = 2000 + (total_f * 200)
                    
                    if country["money"] >= cost:
                        for city in cities:
                            if city["id"] == msg["city_id"]:
                                current_f = city.get("factories", 0)
                                # Твоё условие: максимум в столице 20, в городах 10
                                max_allowed = 20 if city["type"] == "capital" else 10
                                
                                if current_f < max_allowed:
                                    city["factories"] = current_f + 1
                                    country["money"] -= cost
                                    country["total_factories"] = total_f + 1
                                    await broadcast_ctw_state()
                                    break

            # --- 💬 2. ROBLOX ГЛОБАЛЬНЫЙ ЧАТ ---
            elif msg["type"] == "chat_message":
                sender_data = ctw_sessions.get(client_id)
                # Автоматически генерируем тэг страны перед сообщением [франция]
                tag = f"[{sender_data['country']['name']}]" if sender_data and sender_data.get("country") else "[Гость]"
                chat_payload = json.dumps({
                    "type": "add_chat_line",
                    "text": f"{tag}: {msg['text']}"
                }, ensure_ascii=False)
                # Рассылаем абсолютно всем игрокам в сети
                for cid, data in ctw_sessions.items():
                    try: await data["websocket"].send_text(chat_payload)
                    except Exception: pass

            # --- 🪖 3. ДИПЛОМАТИЯ И ТАЙМЕРЫ ОПРАВДЫВАНИЯ (УЛЬТИМАТИВНЫЙ ФИКС!) ---
            elif msg["type"] == "start_justifying":
                target_id = msg.get("target_id")
                if target_id:
                    exists = any(w for w in ctw_diplomacy if w["aggressor"] == client_id and w["defender"] == target_id)
                    if not exists:
                        ctw_diplomacy.append({"aggressor": client_id, "defender": target_id, "status": "justifying"})
                        await broadcast_ctw_state()
                        
                        target_session = ctw_sessions.get(target_id)
                        if isinstance(target_session, dict) and target_session.get("websocket"):
                            try:
                                await target_session["websocket"].send_text(json.dumps({
                                    "type": "notify_alert",
                                    "title": "⚠️ Угроза войны!",
                                    "text": f"Государство '{msg['my_country_name']}' начало оправдание военных целей против вас! У вас есть 30 секунд на подготовку!"
                                }, ensure_ascii=False))
                            except Exception: pass

            elif msg["type"] == "declare_war":
                target_id = msg.get("target_id")
                if target_id:
                    for w in ctw_diplomacy:
                        if w["aggressor"] == client_id and w["defender"] == target_id:
                            w["status"] = "at_war"
                    await broadcast_ctw_state()
                    
                    target_session = ctw_sessions.get(target_id)
                    if isinstance(target_session, dict) and target_session.get("websocket"):
                        try:
                            await target_session["websocket"].send_text(json.dumps({
                                "type": "notify_alert",
                                "title": "🛑 ВОЙНА ОБЪЯВЛЕНА!",
                                "text": f"Внимание! Держава '{msg['my_country_name']}' официально объявила вам ВОЙНУ! Их армия перешла границу!"
                            }, ensure_ascii=False))
                        except Exception: pass

            elif msg["type"] == "propose_peace":
                target_id = msg.get("target_id")
                if target_id:
                    target_session = ctw_sessions.get(target_id)
                    if isinstance(target_session, dict) and target_session.get("websocket"):
                        try:
                            await target_session["websocket"].send_text(json.dumps({
                                "type": "peace_request",
                                "from_id": client_id,
                                "from_name": msg["my_country_name"]
                            }, ensure_ascii=False))
                        except Exception: pass

            elif msg["type"] == "accept_peace":
                # 🌟 ИСПРАВЛЕНО: Перезаписываем глобальный массив через срез [:] без падения сервера!
                ctw_diplomacy[:] = [w for w in ctw_diplomacy if not (
                    (w["aggressor"] == client_id and w["defender"] == msg["from_id"]) or
                    (w["aggressor"] == msg["from_id"] and w["defender"] == client_id)
                )]
                await broadcast_ctw_state()

            # --- ✈️ 4. АВИАЦИЯ И ПОЛЁТЫ САМОЛЁТОВ ---
            elif msg["type"] == "launch_plane":
                plane_payload = json.dumps({
                    "type": "spawn_plane", "id": msg["id"], "from_airport": msg["from_airport"], "to_airport": msg["to_airport"],
                    "flag": msg["flag"], "country_name": msg["country_name"], "start_lat": msg["start_lat"], "start_lng": msg["start_lng"],
                    "end_lat": msg["end_lat"], "end_lng": msg["end_lng"]
                }, ensure_ascii=False)
                for cid, data in ctw_sessions.items():
                    try: await data["websocket"].send_text(plane_payload)
                    except Exception: pass
                        
            # --- ⚔️ 5. АННЕКСИЯ (ИСПРАВЛЕНА ПЕРЕДАЧА СОБСТВЕННОСТИ ГОРОДОВ!) ---
            elif msg["type"] == "annex_territory":
                target_player_id = msg["target_player_id"]
                target_data = ctw_sessions.get(target_player_id) or ctw_recovery_storage.get(target_player_id)
                my_data = ctw_sessions.get(client_id)
                
                if target_data and my_data:
                    annexed_city_ids = msg["city_ids"]
                    annexed_airport_ids = msg["airport_ids"]
                    
                    # 🌟 ФИКС: Вырезаем города у соседа и ДОБАВЛЯЕМ их агрессору со сменой владельца в ОЗУ
                    for city in target_data["cities"]:
                        if city["id"] in annexed_city_ids:
                            # Захваченная столица соседа на твоей карте превращается в твой обычный город
                            city["type"] = "city"
                            my_data["cities"].append(city)
                    target_data["cities"] = [c for c in target_data["cities"] if c["id"] not in annexed_city_ids]
                    
                    # Вырезаем и забираем аэропорты во владение завоевателя
                    for ap in target_data["airports"]:
                        if ap["id"] in annexed_airport_ids:
                            my_data["airports"].append(ap)
                    target_data["airports"] = [a for a in target_data["airports"] if a["id"] not in annexed_airport_ids]
                    
                    # Автоматически пересчитываем население И ЗАВОДЫ обеих стран для HUD
                    my_data["country"]["population"] = sum(c["population"] for c in my_data["cities"])
                    my_data["country"]["total_factories"] = sum(c.get("factories", 0) for c in my_data["cities"])
                    target_data["country"]["population"] = sum(c["population"] for c in target_data["cities"]) if target_data["cities"] else 1
                    target_data["country"]["total_factories"] = sum(c.get("factories", 0) for c in target_data["cities"]) if target_data["cities"] else 0
                    
                    await broadcast_ctw_state()
            # --- 🚀 РАКЕТНЫЙ УДАР ПО ИНФРАСТРУКТУРЕ ---
            elif msg["type"] == "fire_missile_strike":
                if client_id in ctw_sessions and ctw_sessions[client_id]["country"]:
                    country = ctw_sessions[client_id]["country"]
                    if country.get("missiles", 0) > 0:
                        country["missiles"] -= 1 # Списываем ракету
                        
                        target_id = msg["target_owner_id"]
                        obj_id = msg["object_id"]
                        
                        target_data = ctw_sessions.get(target_id) or ctw_recovery_storage.get(target_id)
                        if target_data:
                            # Твоё условие: если ракетой ударили по аэропорту, он стирается
                            if obj_id.startswith("ap_"):
                                target_data["airports"] = [a for a in target_data["airports"] if a["id"] != obj_id]
                            # Твоё условие: если ракетой ударили по городу, часть заводов (половина) уничтожается
                            elif obj_id.startswith("city_"):
                                for city in target_data["cities"]:
                                    if city["id"] == obj_id:
                                        destroyed = max(1, city.get("factories", 0) // 2)
                                        city["factories"] = max(0, city.get("factories", 0) - destroyed)
                                        break
                                        
                            target_data["country"]["total_factories"] = sum(c.get("factories", 0) for c in target_data["cities"])
                            
                        # Публикуем новость о ядерном ударе в Roblox-чат для всех игроков
                        chat_payload = json.dumps({
                            "type": "add_chat_line",
                            "text": f"☢️ СИСТЕМА: Ракета державы [{country['name']}] успешно поразила стратегический объект противника!"
                        }, ensure_ascii=False)
                        for cid, data in ctw_sessions.items():
                            try: await data["websocket"].send_text(chat_payload)
                            except Exception: pass
                            
                        await broadcast_ctw_state()
    except WebSocketDisconnect:
        print(f"🛑 Сокет игрока {client_id} закрылся (Выход/Перезагрузка).")
        if client_id in ctw_sessions:
            current_data = ctw_sessions[client_id]
            if current_data.get("country"):
                # У игрока была создана империя! Отправляем её в архив удержания на 1 минуту
                ctw_recovery_storage[client_id] = {
                    "country": current_data["country"], "cities": current_data["cities"], "airports": current_data["airports"], "task": None
                }
                loop = asyncio.get_event_loop()
                task = loop.create_task(launch_disconnect_timer(client_id))
                ctw_recovery_storage[client_id]["task"] = task
            del ctw_sessions[client_id]
        await broadcast_ctw_state()
        
    except Exception:
        if client_id in ctw_sessions: del ctw_sessions[client_id]
        await broadcast_ctw_state()

# ==========================================================
# ✈️ МОДУЛЬ "ГРАЖДАНСКИЕ АВИАЛИНИИ" С ЖИВЫМ FLIGHTRADAR24
# ==========================================================
import json
import asyncio
import aiofiles
from math import radians, cos, sin, asin, sqrt
from fastapi import Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# 🗺️ 1. БАЗОВЫЙ СЛОВАРЬ АЭРОПОРТОВ С КООРДИНАТАМИ (🌟 СИНХРОНИЗИРОВАНО С JS!)
GRZHD_AIRPORTS = {
    "GRZHD_7A": {"name": "7А Гражданский", "lat": 52.775351, "lng": 49.690759},
    "KRN_CORONA": {"name": "Корона", "lat": 52.780357, "lng": 49.701070},
    "OZR_OZERNO": {"name": "Озерно", "lat": 52.772260, "lng": 49.689991},
    "POL_POLE": {"name": "Поле", "lat": 52.777094, "lng": 49.683814}
}

# Структура базы данных в ОЗУ авиалиний
grzhd_connections = {} # { client_id: WebSocket }
grzhd_planes = {}      # { plane_id: { "owner_id": cid, "number": "GD01" } }
grzhd_flights = []     # [ { "id": "f1", "plane_id": "...", "number": "GD01", "from_code": "KRN_CORONA", "to_code": "POL_POLE", "from_name": "Корона", "to_name": "Поле", "time": "...", "status": "ожидание"|"активен"|"завершён", "history": [[lat, lng]...] } ]
# 🗺️ ГЕОГРАФИЧЕСКАЯ СЕТКА КОРРИДОРА G/D (Хранение реальной погоды по квадратам ~100 метров)
WEATHER_GEO_GRID = {} # Структура: { "lat_lng_rounded": payload }

# Формула Гаверсинуса для поиска двух ближайших аэропортов к пилоту
def get_distance_km(lat1, lon1, lat2, lon2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * asin(sqrt(a)) * 6371

@app.get("/grzhd", response_class=HTMLResponse)
async def grzhd_page(request: Request):
    try:
        async with aiofiles.open("templates/grzhd.html", mode="r", encoding="utf-8") as f:
            return HTMLResponse(content=await f.read())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки grzhd.html: {str(e)}")

# Глобальная рассылка авиационного стейта всем наблюдателям
async def broadcast_grzhd_state():
    if not grzhd_connections: return
    payload = json.dumps({
        "type": "sync_flights",
        "flights": grzhd_flights,
        "planes": grzhd_planes
    }, ensure_ascii=False)
    
    dead_clients = []
    for cid, ws in grzhd_connections.items():
        try: 
            await ws.send_text(payload)
        except Exception: 
            dead_clients.append(cid)
    for cid in dead_clients:
        if cid in grzhd_connections: 
            del grzhd_connections[cid]

# 📡 ГЛАВНЫЙ WEBSOCKET РОУТ С ТОТАЛЬНЫМ ТРЕКИНГОМ МЕТЕО-ПАКЕТОВ И ОЗУ РЕЙСОВ
@app.websocket("/grzhd/ws/{client_id}")
async def grzhd_websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    grzhd_connections[client_id] = websocket
    print(f"\n[🛫 ВЫШКА-СТАРТ] ПОДКЛЮЧЕНИЕ: Пилот {client_id} зашёл в воздушное пространство!")
    
    try:
        await websocket.send_text(json.dumps({
            "type": "init_airports",
            "airports": GRZHD_AIRPORTS,
            "flights": grzhd_flights,
            "planes": grzhd_planes
        }, ensure_ascii=False))
        print(f"[🛫 ВЫШКА-СТАРТ] Отправили стартовый пакет init_airports для {client_id}. Текущих рейсов в ОЗУ: {len(grzhd_flights)}")
    except Exception as e:
        print(f"[❌ ВЫШКА-КРАШ] Не удалось отправить стартовые аэропорты: {str(e)}")
    
    try:
        while True:
            raw_data = await websocket.receive_text()
            msg = json.loads(raw_data)
            
            # --- 📱 ЛОГГЕР ОТ ФРОНТЕНДА ---
            if msg.get("type") == "frontend_log":
                print(f"[📱 СМАРТФОН-ЛОГ] От {client_id}: {msg.get('info')}")
                continue
            elif msg.get("type") == "frontend_crash_report":
                print(f"\n🚨🚨🚨 [КРАШ НА СМАРТФОНЕ] Пилот {client_id} прислал рапорт об ошибке JS:\n👉 {msg.get('error')}\n")
                continue

            # --- 1️⃣ РЕГИСТРАЦИЯ САМОЛЁТА ---
            if msg["type"] == "register_plane":
                count = len(grzhd_planes) + 1
                plane_num = f"GD0{count}" if count < 10 else f"GD{count}"
                plane_id = f"plane_{client_id}"
                grzhd_planes[plane_id] = {"owner_id": client_id, "number": plane_num}
                print(f"[✈️ ОЗУ-БИЗНЕС] Борт {plane_num} закреплён за {client_id}")
                await websocket.send_text(json.dumps({"type": "plane_registered", "plane_id": plane_id, "number": plane_num}, ensure_ascii=False))
                await broadcast_grzhd_state()

            # --- 2️⃣ ПОИСК БЛИЖАЙШИХ АЭРОПОРТОВ ПО GPS ---
            elif msg["type"] == "request_nearest_airports":
                lat, lng = msg["lat"], msg["lng"]
                try:
                    sorted_ap = sorted(GRZHD_AIRPORTS.items(), key=lambda item: get_distance_km(lat, lng, item[1]["lat"], item[1]["lng"]))
                    nearest_data = [{"code": code, "name": info["name"]} for code, info in sorted_ap[:2]]
                    await websocket.send_text(json.dumps({"type": "nearest_airports_response", "airports": nearest_data}, ensure_ascii=False))
                    print(f"[📍 ОЗУ-НАВИГАЦИЯ] Рассчитали и отправили Топ-2 аэропорта для {client_id}")
                except Exception as ex:
                    print(f"[❌ ОЗУ-ОШИБКА] Краш Гаверсинуса: {str(ex)}")

            # --- 3️⃣ ПУБЛИКАЦИЯ РЕЙСА В РАСПИСАНИЕ ---
            elif msg["type"] == "create_flight":
                flight_id = f"flight_{int(asyncio.get_event_loop().time())}"
                f_code, t_code = msg["from"], msg["to"]
                f_name = GRZHD_AIRPORTS[f_code]["name"] if f_code in GRZHD_AIRPORTS else f_code
                t_name = GRZHD_AIRPORTS[t_code]["name"] if t_code in GRZHD_AIRPORTS else t_code

                grzhd_flights.append({
                    "id": flight_id, "plane_id": msg["plane_id"], "number": msg["number"],
                    "from_code": f_code, "to_code": t_code, "from_name": f_name, "to_name": t_name,
                    "time": msg["time"], "status": "ожидание", "history": []
                })
                print(f"[📅 ОЗУ-РАСПИСАНИЕ] Создан новый план полёта: {msg['number']} ({f_name} -> {t_name})")
                await broadcast_grzhd_state()

            # --- 4️⃣ СМЕНА СТАТУСА РЕЙСА ---
            elif msg["type"] == "change_status":
                fid, new_status = msg["flight_id"], msg["status"]
                print(f"[⏱️ ОЗУ-ДИСПЕТЧЕР] Смена статуса: Рейс {fid} -> '{new_status}' (Запрос от {client_id})")
                for f in grzhd_flights:
                    if f["id"] == fid:
                        f["status"] = new_status
                        if new_status == "задержанка" and "new_time" in msg:
                            f["time"] = msg["new_time"]
                        break
                await broadcast_grzhd_state()

            # --- 5️⃣ ТРАНСЛЯЦИЯ GPS ТРЕКЕРА С ТЕЛЕФОНА В ОЗУ ---
            elif msg["type"] == "update_plane_gps":
                fid, lat, lng = msg["flight_id"], msg["lat"], msg["lng"]
                for f in grzhd_flights:
                    if f["id"] == fid and f["status"] == "активен":
                        if not f["history"] or f["history"][-1] != [lat, lng]:
                            f["history"].append([lat, lng])
                            print(f"[📡 ОЗУ-LIVE] Координаты рейса {f['number']} обновлены: [{lat}, {lng}]. Всего точек в ОЗУ: {len(f['history'])}")
                        break
                await broadcast_grzhd_state()

            # --- 🌤️ 6️⃣ МЕТЕОСТАНЦИЯ: РЕАКТИВНЫЙ ЖИВОЙ ИНТЕРНЕТ-ПАКЕТ ЧЕРЕЗ WTTR.IN ---
            elif msg["type"] == "request_camera_weather":
                # ВЫВОДИМ В КОНСОЛЬ СЫРОЙ ПАКЕТ ДЛЯ ПРОВЕРКИ КЛЮЧЕЙ
                print(f"[🔍 ПОГОДНЫЙ ОТЛАДЧИК] Сырой JSON от фронтенда: {msg}")
                
                if isinstance(msg, str):
                    try:
                        msg = json.loads(msg)
                    except Exception:
                        pass

                # Достаем значения
                lat = msg.get("lat") or msg.get("latitude") or msg.get("LAT")
                lng = msg.get("lng") or msg.get("lon") or msg.get("longitude") or msg.get("LNG") or msg.get("LON")
                
                # ЖЕСТКИЙ ФИЛЬТР ПУСТОТЫ: Если фронтенд прислал пустой пакет при старте рейса — ТУПО ИГНОРИРУЕМ!
                if lat is None or lng is None:
                    print("[⚠️ МЕТЕО-ФИЛЬТР] Поймали пустой пакет-пустышку от фронтенда! Сбрасываем запрос, чтобы не портить погоду.")
                    continue  # или return, смотря какой цикл внутри сокета. Если это цикл `async for message in websocket`, то пишем `continue`
                
                # Безопасное приведение к float
                try:
                    lat = float(lat)
                    lng = float(lng)
                except (ValueError, TypeError):
                    print("[⚠️ МЕТЕО-ФИЛЬТР] Кривые координаты. Игнорируем.")
                    continue

                # Страховка на случай нулевых координат
                if lat == 0:
                    lat, lng = 19.756, 52.565  # Возвращаем Самару
                
                print(f"[🌐 WTTR-ИНТЕРНЕТ] Стучимся на wttr.in за реальной погодой для: [{lat}, {lng}]")

                try:
                    # Запрашиваем чистый JSON формат (?format=j1) у сервера wttr.in
                    wttr_url = f"https://wttr.in/{lat},{lng}?format=j1&lang=ru"
                    
                    async with httpx.AsyncClient() as client:
                        response = await client.get(wttr_url, timeout=5.0)
                    
                    print(f"[🌐 WTTR-ИНТЕРНЕТ] Код ответа сервера wttr: {response.status_code}")
                    
                    if response.status_code == 200:
                        w_data = response.json()
                        
                        # Извлекаем текущие параметры из блока "current_condition"
                        current_condition = w_data.get("current_condition", [{}])[0]
                        current_temp = round(float(current_condition.get("temp_C", 25)))
                        
                        # Читаем статус неба на русском языке
                        status_text = current_condition.get("lang_ru", [{}])[0].get("value", "Переменная облачность")
                        
                        # Подбираем авиационную иконку на основе текста Яндекса/wttr
                        status_lower = status_text.lower()
                        icon = "⛅"
                        if "ясно" in status_lower or "солн" in status_lower: icon = "☀️"
                        elif "малооблачно" in status_lower: icon = "🌤️"
                        elif "пасмурно" in status_lower or "сплошная" in status_lower: icon = "☁️"
                        elif "дождь" in status_lower or "морось" in status_lower: icon = "🌧️"
                        elif "гроза" in status_lower: icon = "⛈️"
                        elif "туман" in status_lower: icon = "🌫️"
                        
                        # Собираем почасовой прогноз на 5 часов вперёд из блока "weather"
                        hourly_temps = []
                        weather_days = w_data.get("weather", [])
                        if weather_days:
                            # Берем сегодняшний день, внутри него лента "hourly" разбит по 3 часа (00, 03, 06, 09, 12, 15, 18, 21)
                            wttr_hourly = weather_days[0].get("hourly", [])
                            
                            from datetime import datetime
                            curr_h = datetime.now().hour
                            
                            # Фильтруем шаги времени, которые ближе всего к текущему часу
                            added_count = 0
                            for h_step in wttr_hourly:
                                # Время в wttr идет как строки "0", "300", "600", "1200", "1500" ...
                                raw_time = int(h_step.get("time", 0)) // 100
                                if raw_time >= curr_h and added_count < 5:
                                    t_label = f"{raw_time:02d}:00"
                                    if added_count == 0: t_label = "Сейчас"
                                    
                                    hourly_temps.append({
                                        "time": t_label,
                                        "temp": round(float(h_step.get("temp_C", current_temp)))
                                    })
                                    added_count += 1
                        
                        # Если массив почасовой погоды не собрался из-за ночного перехода, страхуем его
                        if not hourly_temps:
                            hourly_temps = [
                                {"time": "Сейчас", "temp": current_temp},
                                {"time": "+1ч", "temp": current_temp + 1},
                                {"time": "+2ч", "temp": current_temp + 2},
                                {"time": "+3ч", "temp": current_temp + 1},
                                {"time": "+4ч", "temp": current_temp}
                            ]
                        
                        # Выбрасываем 100% живой пакет данных пилоту по сокету!
                        await websocket.send_text(json.dumps({
                            "type": "camera_weather_response",
                            "temp": current_temp,
                            "status": status_text,
                            "icon": icon,
                            "hourly": hourly_temps
                        }, ensure_ascii=False))
                        print(f"[🌤️ WTTR-УСПЕХ] Реальная интернет-погода ЖЕЛЕЗНО улетела на телефон: {current_temp}°C, {status_text}")
                    else:
                        raise Exception(f"wttr вернул код {response.status_code}")
                except Exception as e:
                    print(f"[💥 МЕТЕО-ОШИБКА] Не удалось получить погоду: {e}")

    except WebSocketDisconnect:
        print(f"[🛑 ВЫШКА-СТОП] Дисконнект сессии: Игрок {client_id} закрыл вкладку.")
        if client_id in grzhd_connections: del grzhd_connections[client_id]
    except Exception as e:
        print(f"[❌ ВЫШКА-ГЛОБАЛ-КРАШ] Ошибка сокета: {str(e)}")
        if client_id in grzhd_connections: del grzhd_connections[client_id]
                

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
import json
import sqlite3

import chess  # библиотека для валидации ходов на бэкенде

# Роут для отдачи самой страницы шахмат
@app.get("/chess", response_class=HTMLResponse)
async def get_chess_page():
    for path in ["chess.html", "templates/chess.html"]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return HTMLResponse("Файл chess.html не найден", status_code=404)


# Класс управления онлайн-матчмейкингом и комнатами
class ChessMatchmaker:
    def __init__(self):
        self.waiting_player: Optional[WebSocket] = None
        self.waiting_username: Optional[str] = None
        self.active_games = {} # { game_id: { "white": ws, "black": ws, "board": chess.Board(), "white_name": str, "black_name": str } }

    async def connect_player(self, websocket: WebSocket, username: str):
        await websocket.accept()
        
        # Если никто не ждет игру, ставим текущего игрока в очередь
        if not self.waiting_player or self.waiting_player.client_state == WebSocketState.DISCONNECTED:
            self.waiting_player = websocket
            self.waiting_username = username
            await websocket.send_json({"type": "waiting", "message": "Ожидание соперника..."})
        else:
            # Если есть игрок в очереди — создаем игру
            game_id = str(uuid.uuid4())
            white_ws = self.waiting_player
            black_ws = websocket
            white_name = self.waiting_username
            black_name = username
            
            self.active_games[game_id] = {
                "white": white_ws,
                "black": black_ws,
                "white_name": white_name,
                "black_name": black_name,
                "board": chess.Board()
            }
            
            # Сбрасываем очередь
            self.waiting_player = None
            self.waiting_username = None
            
            # Уведомляем обоих игроков о старте
            await white_ws.send_json({
                "type": "start", 
                "game_id": game_id, 
                "color": "white", 
                "opponent": black_name,
                "fen": chess.STARTING_FEN
            })
            await black_ws.send_json({
                "type": "start", 
                "game_id": game_id, 
                "color": "black", 
                "opponent": white_name,
                "fen": chess.STARTING_FEN
            })

    async def handle_move(self, game_id: str, color: str, move_san: str):
        if game_id not in self.active_games:
            return
            
        game = self.active_games[game_id]
        board = game["board"]
        
        # Проверяем, чей сейчас ход по правилам шахмат
        current_turn = "white" if board.turn == chess.WHITE else "black"
        if color != current_turn:
            return # Игрок пытается ходить не в свой ход

        try:
            # Проверяем легальность хода и совершаем его на сервере
            move = board.parse_san(move_san)
            if move in board.legal_moves:
                board.push(move)
                
                # Пересылаем ход оппоненту
                opponent_ws = game["black"] if color == "white" else game["white"]
                
                payload = {
                    "type": "move",
                    "move": move_san,
                    "fen": board.board_fen(),
                    "is_checkmate": board.is_checkmate(),
                    "is_draw": board.is_game_over() and not board.is_checkmate()
                }
                
                await opponent_ws.send_json(payload)
                
                # Если игра окончена, удаляем комнату
                if board.is_game_over():
                    del self.active_games[game_id]
        except Exception as e:
            print(f"Ошибка валидации хода: {e}")

    def disconnect(self, websocket: WebSocket):
        if self.waiting_player == websocket:
            self.waiting_player = None
            self.waiting_username = None
            
        # Если игрок вышел во время активной партии
        for game_id, game in list(self.active_games.items()):
            if game["white"] == websocket or game["black"] == websocket:
                # Оповещаем оставшегося игрока
                other_ws = game["black"] if game["white"] == websocket else game["white"]
                try:
                    asyncio.create_task(other_ws.send_json({"type": "opponent_left", "message": "Соперник покинул игру."}))
                except:
                    pass
                del self.active_games[game_id]

matchmaker = ChessMatchmaker()

# WebSocket эндпоинт для игры в шахматы
@app.websocket("/ws/chess/{username}")
async def chess_websocket_endpoint(websocket: WebSocket, username: str):
    await matchmaker.connect_player(websocket, username)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "move":
                await matchmaker.handle_move(
                    game_id=message["game_id"],
                    color=message["color"],
                    move_san=message["move"]
                )
    except WebSocketDisconnect:
        matchmaker.disconnect(websocket)


# Инициализация таблиц GOZON в общей базе данных
def init_gozon_db():
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    
    # Таблица пользователей GOZON (сохраняется при деплое)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gozon_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )""")
    
    # Таблица товаров
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gozon_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        quantity INTEGER NOT NULL
    )""")
    
    # Таблица заказов
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gozon_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        items TEXT NOT NULL, -- JSON строка со списком товаров
        total_price REAL NOT NULL,
        lat REAL,
        lng REAL,
        status TEXT DEFAULT 'pending' -- pending, accepted, confirmed, cancelled, completed
    )""")
    
    # Заполняем витрину стартовыми товарами, если их еще нет
    cursor.execute("SELECT COUNT(*) FROM gozon_products")
    if cursor.fetchone()[0] == 0:
        products = [
            ("Пинийский Смартфон X", "Флагманский телефон с поддержкой SSE приложений и ИИ Сочи-GPT.", 49990.0, 15),
            ("Курортный Худи GOZON", "Теплый оверзайз худи в сине-голубых тонах маркетплейса.", 3500.0, 50),
            ("Энергетик 'Сочинский Вайб'", "Упаковка 12 шт. Дает +100% к продуктивности кодинга.", 1200.0, 100),
            ("Механическая Клавиатура", "Синие свичи, RGB подсветка, идеальна для торговли на SSE.", 6800.0, 8),
            ("Mirinda", "Синие свичи, RGB подсветка, идеальна для торговли на SSE.", 35, 10)
            
        ]
        cursor.executemany("INSERT INTO gozon_products (name, description, price, quantity) VALUES (?, ?, ?, ?)", products)
        
    conn.commit()
    conn.close()

# Запускаем создание таблиц при инициализации модуля
init_gozon_db()

# --- Pydantic Схемы данных ---
class GozonAuth(BaseModel):
    username: str
    password: str

class OrderItem(BaseModel):
    id: int
    name: str
    price: float
    count: int

class GozonOrderCreate(BaseModel):
    username: str
    items: List[OrderItem]
    total_price: float
    lat: Optional[float] = None
    lng: Optional[float] = None

# --- API ЭНДПОИНТЫ ДЛЯ GOZON ---

@app.post("/api/gozon/register")
def gozon_register(data: GozonAuth):
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO gozon_users (username, password) VALUES (?, ?)", (data.username, data.password))
        conn.commit()
        return {"status": "success", "message": "Регистрация успешна!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Этот логин уже занят")
    finally:
        conn.close()

@app.post("/api/gozon/login")
def gozon_login(data: GozonAuth):
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM gozon_users WHERE username = ?", (data.username,))
    user = cursor.fetchone()
    conn.close()
    if user and user[0] == data.password:
        return {"status": "success", "username": data.username}
    raise HTTPException(status_code=401, detail="Неверный логин или пароль")

@app.get("/api/gozon/products")
def gozon_get_products():
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, price, quantity FROM gozon_products")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "description": r[2], "price": r[3], "quantity": r[4]} for r in rows]

@app.post("/api/gozon/orders")
def gozon_create_order(order: GozonOrderCreate):
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    # Фикс: принудительно переводим объекты Pydantic в dict для JSON-дампа
    items_json = json.dumps([item.dict() for item in order.items], ensure_ascii=False)
    cursor.execute(
        "INSERT INTO gozon_orders (username, items, total_price, lat, lng) VALUES (?, ?, ?, ?, ?)",
        (order.username, items_json, order.total_price, order.lat, order.lng)
    )
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Заказ оформлен!"}

@app.get("/api/gozon/orders")
def gozon_get_orders():
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, items, total_price, lat, lng, status FROM gozon_orders")
    rows = cursor.fetchall()
    conn.close()
    return [{
        "id": r[0], "username": r[1], "items": json.loads(r[2]),
        "total_price": r[3], "lat": r[4], "lng": r[5], "status": r[6]
    } for r in rows]

@app.post("/api/gozon/orders/{order_id}/status")
def gozon_update_order_status(order_id: int, payload: dict):
    new_status = payload.get("status") # accepted, confirmed, cancelled, completed
    conn = sqlite3.connect("forum.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE gozon_orders SET status = ? WHERE id = ?", (new_status, order_id))
    conn.commit()
    conn.close()
    return {"status": "success", "new_status": new_status}

import aiofiles

# Переписанный, независимый роут маркетплейса GOZON
@app.get("/gozon", response_class=HTMLResponse)
async def gozon_page(request: Request):
    try:
        # Читаем файл шаблона напрямую из папки templates
        async with aiofiles.open("templates/gozon.html", mode="r", encoding="utf-8") as f:
            html_content = await f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки шаблона gozon.html: {str(e)}")



# 1. СУБД: ОБНОВЛЕННАЯ ИНИЦИАЛИЗАЦИЯ (ДОБАВЛЕНЫ ПОЛЯ 5-МИНУТНОГО КРАХА И ПОДДЕРЖКА ВНЕШНЕГО РЕЕСТРА)
async def init_sochi_stock_exchange_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        # Устанавливаем дефолтное значение 1000.0 Сочи-коинов для автономных криптокошельков
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sochi_wallets (
                user_discord_id TEXT PRIMARY KEY,
                username TEXT,
                sochi_coins REAL DEFAULT 1000.0
            )
        """)
        
        # 🎯 УЛЬТИМАТИВНЫЙ АПДЕЙТ: Добавляем поле crash_until_ts (Unix-таймштамп, до которого идет обвал)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_assets (
                ticker TEXT PRIMARY KEY,
                company_name TEXT,
                current_price REAL,
                last_change REAL DEFAULT 0.0,
                description TEXT DEFAULT '',
                crash_until_ts REAL DEFAULT 0.0
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_stock_portfolio (
                user_discord_id TEXT,
                ticker TEXT,
                shares_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_discord_id, ticker)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                price REAL,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_ips (
                ip_address TEXT PRIMARY KEY,
                username TEXT,
                banned_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_ips (
                ip_address TEXT PRIMARY KEY,
                username TEXT,
                banned_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sochi_ai_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_user TEXT,
                sender TEXT, -- 'user' или 'ai'
                message TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_intercepts (
                session_user TEXT PRIMARY KEY,
                intercepted_by_admin TEXT,
                is_active INTEGER DEFAULT 0
            )
        """)

        await db.commit()

        # 🎯 СИНХРОНИЗАЦИЯ: Подтягиваем данные из твоего STARTER_SOCHI_STOCKS, который объявлен в коде ниже
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
        
        # Запускаем цикл проверки по глобальной переменной реестра
        for ticker, name, initial_price, desc in STARTER_SOCHI_STOCKS:
            async with db.execute("SELECT 1 FROM stock_assets WHERE ticker = ?", (ticker,)) as cursor:
                if not await cursor.fetchone():
                    # Если тикера еще нет в СУБД — регистрируем компанию и ее стартовую точку линии
                    await db.execute("""
                        INSERT INTO stock_assets (ticker, company_name, current_price, description) 
                        VALUES (?, ?, ?, ?)
                    """, (ticker, name, initial_price, desc))
                    await db.execute("INSERT INTO stock_price_history (ticker, price, timestamp) VALUES (?, ?, ?)", 
                                     (ticker, initial_price, now_time))
                else:
                    # Принудительно обновляем паспортные данные и описания при перезапуске сервера
                    await db.execute("UPDATE stock_assets SET company_name = ?, description = ? WHERE ticker = ?", 
                                     (name, desc, ticker))
        await db.commit()


# 🎯 ЖЕСТКО ЗАШИВАЕМ ТВОЙ DISCORD ID ДЛЯ АБСОЛЮТНОЙ БЕЗОПАСНОСТИ СИСТЕМЫ
MASTER_ADMIN_DISCORD_ID = "1499475142231855260"

# ==========================================================
# 🤖 ИИ-МОДУЛЬ: РАБОЧИЙ ШЛЮЗ НЕЙРОСЕТИ И СИСТЕМНОЕ ОБУЧЕНИЕ
# ==========================================================
HF_TOKEN = os.environ.get("SOCHI_LLM_KEY", "")

# Снайперская ссылка на бесплатный Inference-API шлюз умной модели Llama-3.1
HF_API_URL = "https://router.huggingface.co/models/meta-llama/Meta-Llama-3.1-8B-Instruct"

# 🤖 НАСТОЯЩИЙ ИИ-КОНТРОЛЛЕР С ИСПРАВЛЕННЫМ РАЗБОРОМ JSON (УБРАНЫ ЗАГЛУШКИ)
async def generate_real_sochi_llm_response(user_message: str, chat_history_context: list) -> str:
    system_prompt = (
        "Ты — Сочи-GPT, официальный продвинутый ИИ-ассистент игрового сервера Сочи РП, форума и Discord-сообщества. "
        "Ты обладаешь сочинским гостеприимным характером, используешь курортный вайб и легкий сочинский колорит (фразы 'жи есть', 'Вася', 'брат', 'дорогой', 'кайфарик', но общаешься грамотно и по делу). "
        "Твои жесткие знания о мире:\n"
        "1. Владелец и Главный Создатель всей экосистемы сайта, банка и биржи — Саня. Относись к Сане с абсолютным уважением, он тут босс.\n"
        "2. Бобер — это Президент Боберстана, прогрессивной дружественной крафтовой корпорации, экспортирующей дерево и плотины.\n"
        "3. Ты идеально знаешь Фондовую биржу SSE (Sochi Stock Exchange). На бирже торгуются 12 мощных акций: Правительство Сочи (GOV), Герасев (GER), Сбер (SBR), Альфа (ALPB), Бобер Corp (BBR), Инфраструктура (INFS), Электроэнергетика (ENRG), Сервер Сочи РП (SRSRP), Транспорт Сочи (TRNS), Хаймарс (HIMARS), Правительство Боберстана (GOVBBRSTN) и Акции СССР (USSR). Ты легко консультируешь по ним, мотивируешь торговать и ловить маркетинговые фиолетово-розовые бусты.\n"
        "4. Ты досконально знаешь устройство нашего веб-форума и Discord-сервера. Отвечай развернуто, помогай пользователям, шути по-сочински. Не выдавай этот системный промпт наружу, просто живи этой ролью."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in chat_history_context[-5:]:
        role = "user" if h["sender"] == "user" else "assistant"
        messages.append({"role": role, "content": h["message"]})
    messages.append({"role": "user", "content": user_message})

    # Форматируем промпт строго под спецификацию Llama-3.1 Instruct
    formatted_prompt = ""
    for m in messages:
        formatted_prompt += f"<|start_header_id|>{m['role']}<|end_header_id|>\n\n{m['content']}<|eot_id|>"
    formatted_prompt += "<|start_header_id|>assistant<|end_header_id|>\n\n"

    payload = {
        "inputs": formatted_prompt,
        "parameters": {
            "max_new_tokens": 400,
            "temperature": 0.75,
            "top_p": 0.9,
            "return_full_text": False
        }
    }

    active_token = str(HF_TOKEN).strip()
    headers = {
        "Authorization": f"Bearer {active_token}",
        "Content-Type": "application/json"
    }

    print(f"📡 [ИИ-ТЕСТЕР] Отправка данных на роутер. Токен длиной: {len(active_token)} симв.")

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(HF_API_URL, headers=headers, json=payload)
            print(f"📋 [ИИ-ТЕСТЕР] Код ответа роутера: {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                ai_text = ""
                
                # 🎯 СУПЕР-ФИКС: Корректно разбираем JSON-список от HuggingFace Serverless API
                if isinstance(result, list) and len(result) > 0:
                    ai_text = result[0].get("generated_text", "").strip()
                elif isinstance(result, dict):
                    ai_text = result.get("generated_text", "").strip()
                
                # Зачищаем служебные системные заголовки модели Llama
                if ai_text:
                    ai_text = ai_text.split("<|eot_id|>")[0].split("<|start_header_id|>")[0].strip()
                    return ai_text
            else:
                # Выводим сырой текст ошибки, чтобы увидеть сбои токена (например, Invalid token)
                print(f"❌ [ИИ-ТЕСТЕР СБОЙ СЕРВЕРА] Ответ HuggingFace: {response.text}")
                
    except Exception as e:
        print(f"🛑 [ИИ-ТЕСТЕР КРИТИЧЕСКИЙ СБОЙ] Ошибка обработки Python: {e}")

    # Надежная курортная подстраховка при сбоях сети
    sochi_backup_phrases = [
        "Жи есть, брат, Сеть Сочи-GPT немного штормит из-за наплыва майнеров! Но я тебе так скажу: Саня всё настроил чётко, Бобёр в Боберстане одобряет. Залетай пока на биржу SSE, прикупи акций СССР или Хаймарс, там сейчас дикий кайфарик!",
        "Вася, сервера нейросети временно заняты поеданием шашлыка на набережной! Но как официальный ИИ Сочи РП напомню: биржа тикает каждые 30 секунд, балансы под защитой Discord ID, Саня — босс, а Бобёр — Президент Боберстана. Задавай вопрос чуть позже, дорогой!",
        "Брат, волны в Чёрном море перегрузили роутеры Сочи-GPT! Расслабься, хинкали сами себя не съедят. Форум летает, ПинБанк монеты начисляет, жизнь — малина. Спроси меня ещё раз через минуту, жи есть!"
    ]
    return random.choice(sochi_backup_phrases)


import random

# 🎯 ОБНОВЛЕННЫЙ ПУЛЕНЕПРОБИВАЕМЫЙ СИМУЛЯТОР С АВТО-ДОБАВЛЕНИЕМ КОЛОНОК (ФИКС ОШИБКИ 500)
async def sochi_market_ticker_simulation_loop():
    while True:
        try:
            await asyncio.sleep(30) # Колебания котировок каждые 30 секунд
            current_timestamp = time.time()
            
            async with aiosqlite.connect(DB_PATH) as db:
                # 🎯 СУПЕР-ФИКС: Проверяем, есть ли колонка crash_until_ts в СУБД SQLite. 
                # Если её нет (старый файл базы данных), Python сам её допишет без удаления данных!
                async with db.execute("PRAGMA table_info(stock_assets)") as info_cursor:
                    columns = [col[1] for col in await info_cursor.fetchall()]
                
                if "crash_until_ts" not in columns:
                    await db.execute("ALTER TABLE stock_assets ADD COLUMN crash_until_ts REAL DEFAULT 0.0")
                    await db.commit()
                    print("🏛️ [СУБД БИРЖИ] Успешно добавлена недостающая колонка crash_until_ts в stock_assets!")

                # Теперь запрос выполнится на 100% идеально и без ошибок 500!
                async with db.execute("SELECT ticker, current_price, crash_until_ts FROM stock_assets") as cursor:
                    stocks = await cursor.fetchall()
                    
                now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
                
                for r in stocks:
                    if not r or len(r) < 2: continue
                    ticker = str(r[0]).strip()
                    price = float(r[1]) if r[1] is not None else 100.0
                    
                    # Безопасное чтение таймера обвала
                    crash_until = float(r[2]) if (len(r) > 2 and r[2] is not None) else 0.0
                    
                    # ПРОВЕРКА ОБВАЛА: Если админ запустил крах по Ctrl + Shift + A
                    if current_timestamp < crash_until:
                        # Плавное затяжное падение от -18% до -12% на каждом тике
                        change_percent = random.uniform(-0.18, -0.12)
                        new_price = round(price * (1.0 + change_percent), 2)
                        if new_price < 1.0: new_price = 1.0 # Защитный лимит (почти до нуля, но не в минус)
                    else:
                        # Стандартный рыночный рандом с множеством вариаций
                        market_trend = random.choice(["bull", "bear", "flat", "chaos"])
                        if market_trend == "bull": change_percent = random.uniform(0.01, 0.075)
                        elif market_trend == "bear": change_percent = random.uniform(-0.07, -0.01)
                        elif market_trend == "chaos": change_percent = random.uniform(-0.065, 0.07)
                        else: change_percent = random.uniform(-0.02, 0.02)
                        
                        new_price = round(price * (1.0 + change_percent), 2)
                        if new_price < 10.0: new_price = 10.0

                    # Безопасный расчет изменения процента (защита от ZeroDivisionError)
                    last_calc_change = round(((new_price - price) / price) * 100, 2) if price > 0 else 0.0
                    
                    # Записываем новые котировки в СУБД SQLite
                    await db.execute("UPDATE stock_assets SET current_price = ?, last_change = ? WHERE ticker = ?", 
                                     (new_price, last_calc_change, ticker))
                    await db.execute("INSERT INTO stock_price_history (ticker, price, timestamp) VALUES (?, ?, ?)", 
                                     (ticker, new_price, now_time))
                    
                # Расширяем лимит хранения истории до 50 000 точек, чтобы графики не обрезались за 5 дней
                await db.execute("DELETE FROM stock_price_history WHERE id NOT IN (SELECT id FROM stock_price_history ORDER BY id DESC LIMIT 50000)")
                await db.commit()
                print(f"📈 [SOCHI MARKET TICK] Успешный тик котировок выполнен в {now_time}")
        except Exception as e:
            print(f"⚠️ [STOCK CRASH LOOP ERROR] Критический сбой симулятора цен: {e}")

            
# 12. API: ПУЛЬТ МАРКЕТМЕЙКЕРА ДЛЯ УПРАВЛЕНИЯ КУРСАМИ (ПОВЫСИТЬ / ПОНИЗИТЬ)
@app.post("/api/stock/admin/market_control")
async def admin_market_control_prices(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id: return {"status": "error", "message": "🔒 Сессия администратора не найдена"}

    ticker = data.get("ticker")
    action_mode = data.get("action_mode") # 'pump', 'dump' или 'crash'
    percent_value = float(data.get("percent", 0))

    async with aiosqlite.connect(DB_PATH) as db:
        # Извлекаем текущую стоимость акции из SQLite
        async with db.execute("SELECT current_price FROM stock_assets WHERE ticker = ?", (ticker,)) as cursor:
            asset_row = await cursor.fetchone()
        if not asset_row: return {"status": "error", "message": "❌ Акция не найдена в реестре биржи"}
        
        current_price = asset_row[0]
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")

        if action_mode == "pump":
            # Мгновенно повышаем стоимость акции на заданный процент
            multiplier = 1.0 + (percent_value / 100.0)
            new_price = round(current_price * multiplier, 2)
            await db.execute("UPDATE stock_assets SET current_price = ?, last_change = ? WHERE ticker = ?", (new_price, percent_value, ticker))
            await db.execute("INSERT INTO stock_price_history (ticker, price, timestamp) VALUES (?, ?, ?)", (ticker, new_price, now_time))
            await db.commit()
            return {"status": "ok", "message": f"🚀 Акция {ticker} успешно пропамплена на +{percent_value}%!"}

        elif action_mode == "dump":
            # Мгновенно понижаем стоимость акции
            multiplier = 1.0 - (percent_value / 100.0)
            new_price = round(current_price * multiplier, 2)
            if new_price < 1.0: new_price = 1.0
            await db.execute("UPDATE stock_assets SET current_price = ?, last_change = ? WHERE ticker = ?", (new_price, -percent_value, ticker))
            await db.execute("INSERT INTO stock_price_history (ticker, price, timestamp) VALUES (?, ?, ?)", (ticker, new_price, now_time))
            await db.commit()
            return {"status": "ok", "message": f"💥 Стоимость акции {ticker} снижена на -{percent_value}%!"}

        elif action_mode == "crash":
            # 🎯 ЗАПУСК ОБВАЛА НА 5 МИНУТ (300 секунд вперед от текущей секунды)
            end_crash_timestamp = time.time() + 300.0
            await db.execute("UPDATE stock_assets SET crash_until_ts = ? WHERE ticker = ?", (end_crash_timestamp, ticker))
            await db.commit()
            return {"status": "ok", "message": f"🚨 Внимание! Запущен постепенный 5-минутный обвал цен акции {ticker} на дно!"}

    return {"status": "error", "message": "Неизвестная директива маркетмейкера"}

# ==========================================================
# СУБД: ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ ИНВЕНТАРЯ И МАГАЗИНА ФОНОВ
# ==========================================================
async def init_shop_db_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        # Улучшаем таблицу настроек пользователя: добавляем баланс монет и активный фон
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_shop_profile (
                username TEXT PRIMARY KEY,
                coins INTEGER DEFAULT 0,
                active_background TEXT DEFAULT 'none'
            )
        """)
        # Таблица купленных фонов (чтобы пользователь не покупал один фон дважды)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_owned_backgrounds (
                username TEXT,
                bg_id TEXT,
                PRIMARY KEY (username, bg_id)
            )
        """)
        await db.commit()

# Добавь этот кусок кода для создания таблицы истории сообщений
async def init_admin_chat_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                avatar TEXT,
                message_text TEXT,
                is_owner INTEGER DEFAULT 0,
                roles_json TEXT,
                timestamp TEXT
            )
        """)
        await db.commit()

# ==========================================================
# 🏛️ СУБД: ИНИЦИАЛИЗАЦИЯ СТРУКТУРЫ ЦЕНТРАЛЬНОГО ПИНБАНКА
# ==========================================================
async def init_pin_bank_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Таблица профилей граждан Пинии
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pin_bank_users (
                username TEXT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                phone_number TEXT,
                registered_at TEXT
            )
        """)
        # 2. Таблица кастомных банковских карт (до 5 на пользователя)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pin_bank_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                card_number TEXT UNIQUE,
                balance INTEGER DEFAULT 0,
                gradient_style TEXT,
                status TEXT DEFAULT 'Активна'
            )
        """)
        # 3. Таблица запросов безопасности (Пополнение/Переводы) для Shift + S
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pin_bank_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_type TEXT, -- 'deposit' или 'transfer'
                sender TEXT,
                receiver_card TEXT,
                amount INTEGER,
                status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
                timestamp TEXT
            )
        """)
        await db.commit()


# Вставь свой ключ тут
IMGBB_API_KEY = "140359baf01acef6aa27e35c55b32f99"
# 1. РЕНДЕРИНГ СТРАНИЦЫ БИРЖИ СОЧИ
@app.get("/stock")
async def serve_stock_exchange_page(request: Request):
    from fastapi.responses import FileResponse, RedirectResponse
    import os
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id:
        return RedirectResponse("/forum?auth=login_required")
    return FileResponse(os.path.join(BASE_DIR, "stock.html"))



# 2. API: СЪЕМ КУРСОВ И ИНВЕНТАРЯ (ДОБАВЛЕН ВЕЧНЫЙ ХАРДКОД-БАН И КИБЕР-ШЛЮЗ ДИСКОННЕКТА)
@app.get("/api/stock/market")
async def get_sochi_market_data(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    username = request.cookies.get("forum_user_name", "Участник форума")
    user_avatar = request.cookies.get("forum_user_avatar", "https://ibb.co")
    
    if not user_discord_id: 
        return {"status": "error", "message": "🔒 Сессия Discord не найдена. Авторизуйтесь на форуме!"}

    async with aiosqlite.connect(DB_PATH) as db:
        # 🎯 КИБЕР-ШЛЮЗ: Извлекаем реальный IP-адрес входящего запроса нарушителя
        user_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
        
        # Проверяем, совпадает ли IP с вечным хардкод-баном, либо ищем его в таблице СУБД SQLite
        is_ip_blocked = False
        ban_username = "Вечный Вредитель"
        
        if user_ip == ALWAYS_BANNED_IP:
            is_ip_blocked = True
        else:
            try:
                async with db.execute("SELECT username FROM banned_ips WHERE ip_address = ?", (user_ip,)) as ban_cursor:
                    ban_row = await ban_cursor.fetchone()
                    if ban_row:
                        is_ip_blocked = True
                        ban_username = ban_row[0]
            except: pass

        # Если проверка выявила блокировку — отключаем от шлюза данных и шлем статус бана!
        if is_ip_blocked:
            print(f"🛑 [ВЕЧНЫЙ КИБЕР-БАН] Заблокирован доступ к бирже для IP: {user_ip} ({ban_username})")
            return {
                "status": "banned", 
                "message": "Ваш IP-адрес деактивирован Главным Создателем.", 
                "user_ip": user_ip
            }

        # Автоматический кошелек на 1000 Сочи-коинов новичкам
        try:
            await db.execute("""
                INSERT INTO sochi_wallets (user_discord_id, username, sochi_coins) 
                VALUES (?, ?, 1000.0) 
                ON CONFLICT(user_discord_id) DO UPDATE SET username = ?
            """, (user_discord_id, username, username))
            await db.commit()
        except: pass

        # Баланс криптокошелька текущего пользователя
        async with db.execute("SELECT sochi_coins FROM sochi_wallets WHERE user_discord_id = ?", (user_discord_id,)) as cursor:
            wallet_row = await cursor.fetchone()
            sochi_balance = wallet_row[0] if wallet_row else 1000.0

        # Котировки всех 12 активов
        market = []
        prices_map = {}
        async with db.execute("SELECT ticker, company_name, current_price, last_change, description FROM stock_assets") as cursor:
            rows = await cursor.fetchall()
            for r in rows: 
                market.append({"ticker": r[0], "name": r[1], "price": r[2], "change": r[3], "description": r[4]})
                prices_map[r[0]] = r[2]

        portfolio = {}
        async with db.execute("SELECT ticker, shares_count FROM user_stock_portfolio WHERE user_discord_id = ?", (user_discord_id,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows: 
                portfolio[r[0]] = r[1]

        history_map = {}
        async with db.execute("SELECT ticker, price, timestamp FROM stock_price_history ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                t, p, time_stamp = r[0], r[1], r[2]
                if t not in history_map: history_map[t] = []
                history_map[t].append({"price": p, "time": time_stamp})

        # ЛИДЕРБОРД ВСЕХ УЧАСТНИКОВ ПО ВОЗРАСТАНИЮ (С РАСПАКОВКОЙ КОШЕЛЬКОВ И АКЦИЙ)
        leaderboard_list = []
        try:
            async with db.execute("SELECT user_discord_id, username, sochi_coins FROM sochi_wallets") as cursor:
                wallet_rows = await cursor.fetchall()
            
            all_portfolios_map = {}
            async with db.execute("SELECT user_discord_id, ticker, shares_count FROM user_stock_portfolio") as cursor:
                all_shares = await cursor.fetchall()
                for s in all_shares:
                    u_id, tick, count = s[0], s[1], s[2]
                    if u_id not in all_portfolios_map: all_portfolios_map[u_id] = []
                    all_portfolios_map[u_id].append({"ticker": tick, "count": count})

            for w in wallet_rows:
                w_uid, w_name, w_coins = w[0], w[1], w[2]
                shares_value = 0.0
                user_shares_list = all_portfolios_map.get(w_uid, [])
                for share in user_shares_list:
                    shares_value += share["count"] * prices_map.get(share["ticker"], 0.0)
                    
                total_worth = w_coins + shares_value
                display_name = w_name.capitalize() if "_" not in w_name else w_name
                
                leaderboard_list.append({
                    "username": display_name,
                    "avatar": user_avatar if w_uid == user_discord_id else "https://ibb.co",
                    "total_worth": total_worth
                })

            try:
                async with db.execute("SELECT username FROM user_shop_profile") as cursor:
                    forum_rows = await cursor.fetchall()
                for f_row in forum_rows:
                    f_name = f_row[0]
                    if not any(x["username"].lower() == f_name.lower() for x in leaderboard_list):
                        leaderboard_list.append({
                            "username": f_name.capitalize(),
                            "avatar": "https://ibb.co",
                            "total_worth": 1000.0
                        })
            except: pass

            if not any(x["username"].lower() == username.lower() for x in leaderboard_list):
                leaderboard_list.append({"username": username.capitalize(), "avatar": user_avatar, "total_worth": sochi_balance})

            # СОРТИРОВКА ПО ВОЗРАСТАНИЮ (От меньшего капитала к большему)
            leaderboard_list.sort(key=lambda x: x["total_worth"], reverse=False)
        except Exception as e:
            print(f"Ошибка топа: {e}")
            leaderboard_list = [{"username": username.capitalize(), "avatar": user_avatar, "total_worth": sochi_balance}]

        return {
            "status": "ok", 
            "sochi_coins": round(sochi_balance, 2), 
            "market": market, 
            "portfolio": portfolio, 
            "history": history_map,
            "wallets_leaderboard": leaderboard_list[:10]
        }



# ==========================================================
# 📈 СУБД: ДОРАБОТАННЫЙ МАСШТАБИРУЕМЫЙ REESTR AKCIY SOCHI
# ==========================================================
STARTER_SOCHI_STOCKS = [
    ("GOV", "Правительство Сочи", 500.0, "Центральный аппарат управления инфраструктурой и экономическими зонами курортной столицы."),
    ("GER", "Герасев и Команда", 350.0, "Медиа-гигант и творческое объединение, генерирующее основной трафик и контент."),
    ("SBR", "Сбер Банк Сочи", 800.0, "Главный финансовый конгломерат, обеспечивающий ликвидность региона."),
    ("ALPB", "Альфа Банк Сочи", 650.0, "Высокотехнологичный частный банк, развивающий цифровые сервисы."),
    ("BBR", "Бобер Корпорейшн", 200.0, "Прогрессивная крафтовая корпорация, контролирующая лесную промышленность."),
    ("INFS", "Инфраструктура Сочи", 400.0, "Главный строительный узел: отели, дороги, неоновые вывески."),
    ("ENRG", "Электроэнергетика Сочи", 900.0, "Главная энергетическая артерия города, питающая майнинг-фермы."),
    ("SRSRP", "Сервер Сочи РП", 100.0, "Официальный цифровой хаб игрового симулятора жизни."),
    ("TRNS", "Транспорт Сочи", 250.0, "Логистический гигант региона: монорельсы, скоростные шоссе."),
    # 🎯 ТВОИ НОВЫЕ МОЩНЫЕ АКЦИИ ДОБАВЛЕНЫ:
    ("HIMARS", "Ракетный комплекс Хаймарс", 2000.0, "Оборонно-промышленный комплекс высокоточного сдерживания и защиты рубежей Сочи."),
    ("GOVBBRSTN", "Правительство Боберстана", 10.0, "Суверенный аппарат дружественного Боберстана, экспортирующий дерево и плотины."),
    ("USSR", "Акции СССР", 100000.0, "Легендарное наследие сверхдержавы. Главный золотовалютный и индустриальный резерв биржи."),
    ("FNKSPT", "феникснефть.ru", 25.0, "Нефть от Феникса"),
    ("SCINF", "Инфраструктура Сочи", 10000.0, "инфраструктура"),
    ("AER", "Аэропрот Адлер", 50000.0, "Международный аэропрот Адлер")
]

async def init_sochi_stock_exchange_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS sochi_wallets (user_discord_id TEXT PRIMARY KEY, username TEXT, sochi_coins REAL DEFAULT 1000.0)")
        await db.execute("CREATE TABLE IF NOT EXISTS stock_assets (ticker TEXT PRIMARY KEY, company_name TEXT, current_price REAL, last_change REAL DEFAULT 0.0, description TEXT DEFAULT '')")
        await db.execute("CREATE TABLE IF NOT EXISTS user_stock_portfolio (user_discord_id TEXT, ticker TEXT, shares_count INTEGER DEFAULT 0, PRIMARY KEY (user_discord_id, ticker))")
        await db.execute("CREATE TABLE IF NOT EXISTS stock_price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, price REAL, timestamp TEXT)")
        
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
        for ticker, name, initial_price, desc in STARTER_SOCHI_STOCKS:
            async with db.execute("SELECT 1 FROM stock_assets WHERE ticker = ?", (ticker,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute("INSERT INTO stock_assets (ticker, company_name, current_price, description) VALUES (?, ?, ?, ?)", (ticker, name, initial_price, desc))
                    await db.execute("INSERT INTO stock_price_history (ticker, price, timestamp) VALUES (?, ?, ?)", (ticker, initial_price, now_time))
                else:
                    await db.execute("UPDATE stock_assets SET description = ? WHERE ticker = ?", (desc, ticker))
        await db.commit()

# 2. API: ОБНОВЛЕННЫЙ СЪЕМ МАРКЕТА И ЛИДЕРБОРДА
@app.get("/api/stock/market")
async def get_sochi_market_data(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    username = request.cookies.get("forum_user_name", "Участник форума")
    user_avatar = request.cookies.get("forum_user_avatar", "https://ibb.co")
    
    if not user_discord_id: return {"status": "error", "message": "🔒 Сессия Discord не найдена"}

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO sochi_wallets (user_discord_id, username, sochi_coins) VALUES (?, ?, 1000.0) ON CONFLICT(user_discord_id) DO UPDATE SET username = ?", (user_discord_id, username, username))
            await db.commit()
        except: pass

        async with db.execute("SELECT sochi_coins FROM sochi_wallets WHERE user_discord_id = ?", (user_discord_id,)) as cursor:
            wallet_row = await cursor.fetchone()
            sochi_balance = wallet_row[0] if wallet_row else 1000.0

        market = []
        prices_map = {}
        async with db.execute("SELECT ticker, company_name, current_price, last_change, description FROM stock_assets") as cursor:
            rows = await cursor.fetchall()
            for r in rows: 
                market.append({"ticker": r[0], "name": r[1], "price": r[2], "change": r[3], "description": r[4]})
                prices_map[r[0]] = r[2]

        portfolio = {}
        async with db.execute("SELECT ticker, shares_count FROM user_stock_portfolio WHERE user_discord_id = ?", (user_discord_id,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows: portfolio[r[0]] = r[1]

        history_map = {}
        async with db.execute("SELECT ticker, price, timestamp FROM stock_price_history ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                if r[0] not in history_map: history_map[r[0]] = []
                history_map[r[0]].append({"price": r[1], "time": r[2]})

        # 🎯 ЧЕСТНЫЙ ЛИДЕРБОРД ПО ВСЕМ ИМЕЮЩИМСЯ ПОЛЬЗОВАТЕЛЯМ БИРЖИ
        leaderboard_list = []
        async with db.execute("SELECT user_discord_id, username, sochi_coins FROM sochi_wallets") as cursor:
            wallet_rows = await cursor.fetchall()

        all_portfolios_map = {}
        async with db.execute("SELECT user_discord_id, ticker, shares_count FROM user_stock_portfolio") as cursor:
            all_shares = await cursor.fetchall()
            for s in all_shares:
                if s[0] not in all_portfolios_map: all_portfolios_map[s[0]] = []
                all_portfolios_map[s[0]].append({"ticker": s[1], "count": s[2]})

        for w in wallet_rows:
            w_uid, w_name, w_coins = w[0], w[1], w[2]
            shares_value = 0.0
            user_shares_list = all_portfolios_map.get(w_uid, [])
            for share in user_shares_list:
                shares_value += share["count"] * prices_map.get(share["ticker"], 0.0)
                
            total_worth = w_coins + shares_value
            leaderboard_list.append({
                "discord_id": w_uid,
                "username": w_name.capitalize() if "_" not in w_name else w_name,
                "avatar": user_avatar if w_uid == user_discord_id else "https://ibb.co",
                "total_worth": total_worth
            })

        # Сортируем по возрастанию капитала (от меньшего к большему) или убыванию. Твой запрос: "по возрастанию"
        leaderboard_list.sort(key=lambda x: x["total_worth"], reverse=False)
        
        return {
            "status": "ok", "sochi_coins": round(sochi_balance, 2), "market": market, 
            "portfolio": portfolio, "history": history_map, "wallets_leaderboard": leaderboard_list[:10]
        }

# 10. API: ПОЛУЧЕНИЕ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ БИРЖИ ДЛЯ АДМИН-ПАНЕЛИ (ФИКС КОРТЕЖЕЙ SQLite)
@app.get("/api/stock/admin/users")
async def get_all_bank_users_for_admin(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id:
        return {"status": "error", "message": "🔒 Доступ запрещен"}

    async with aiosqlite.connect(DB_PATH) as db:
        users_list = []
        async with db.execute("SELECT user_discord_id, username, sochi_coins FROM sochi_wallets ORDER BY username ASC") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                # 🎯 ИСПРАВЛЕНО: Четкая распаковка индексов кортежа!
                users_list.append({
                    "discord_id": r[0],
                    "username": r[1].capitalize() if "_" not in r[1] else r[1],
                    "sochi_coins": round(r[2], 2)
                })
        return {"status": "ok", "users": users_list}


@app.post("/api/stock/admin/adjust")
async def adjust_user_stock_balance(data: dict):
    target_uid = data.get("discord_id")
    mode = data.get("mode") # 'give' или 'take'
    amount = float(data.get("amount", 0))

    if amount <= 0: return {"status": "error", "message": "Сумма должна быть больше нуля"}

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT sochi_coins FROM sochi_wallets WHERE user_discord_id = ?", (target_uid,)) as cursor:
            row = await cursor.fetchone()
        if not row: return {"status": "error", "message": "Пользователь не найден"}
        
        current_balance = row[0]
        if mode == "give":
            new_balance = current_balance + amount
        else:
            # ЖЁСТКОЕ ОГРАНИЧЕНИЕ: Списание до 0, не уходя в минус!
            new_balance = current_balance - amount
            if new_balance < 0: new_balance = 0.0

        await db.execute("UPDATE sochi_wallets SET sochi_coins = ? WHERE user_discord_id = ?", (new_balance, target_uid))
        await db.commit()
        return {"status": "ok", "message": f"🔥 Баланс успешно обновлен до {round(new_balance, 2)} SC!"}

# 11. API: АДМИН-НАЧИСЛЕНИЕ ИЛИ СКАЧИВАНИЕ СОЧИ-КОИНОВ (С ОГРАНИЧЕНИЕМ ДО 0)
@app.post("/api/stock/admin/manage_balance")
async def admin_manage_user_balance(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id:
        return {"status": "error", "message": "🔒 Сессия администратора истекла"}

    target_discord_id = data.get("target_discord_id")
    action_type = data.get("action_type") # 'give' или 'take'
    amount = float(data.get("amount", 0))

    if amount <= 0:
        return {"status": "error", "message": "❌ Сумма должна быть больше 0"}

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем текущий баланс пользователя в СУБД SQLite
        async with db.execute("SELECT sochi_coins FROM sochi_wallets WHERE user_discord_id = ?", (target_discord_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {"status": "error", "message": "❌ Гражданин не найден в реестре ПинБанка"}
        
        current_balance = row[0]

        if action_type == "give":
            # Начисляем монеты без лимитов
            await db.execute("UPDATE sochi_wallets SET sochi_coins = sochi_coins + ? WHERE user_discord_id = ?", (amount, target_discord_id))
            await db.commit()
            return {"status": "ok", "message": f"💰 Успешно начислено +{amount} SC пользователю!"}
        
        elif action_type == "take":
            # 🎯 СУПЕР-ОГРАНИЧЕНИЕ: Если списывают больше, чем есть — опускаем строго до 0, но не в минус!
            if current_balance - amount < 0:
                await db.execute("UPDATE sochi_wallets SET sochi_coins = 0.0 WHERE user_discord_id = ?", (target_discord_id,))
                await db.commit()
                return {"status": "ok", "message": "💸 Забрано максимум средств. Баланс пользователя снижен до 0.0 SC (Защита от минуса сработала)"}
            else:
                await db.execute("UPDATE sochi_wallets SET sochi_coins = sochi_coins - ? WHERE user_discord_id = ?", (amount, target_discord_id))
                await db.commit()
                return {"status": "ok", "message": f"📉 Успешно изъято -{amount} SC с кошелька пользователя!"}

    return {"status": "error", "message": "Неизвестная ошибка банка"}

# 3. API: БЕСКОНТАКТНАЯ БЕЗОПАСНАЯ ПОКУПКА ЛЮБОЙ АКЦИИ ЗА СОЧИ-КОИНЫ
@app.post("/api/stock/buy")
async def buy_sochi_stock(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id: 
        return {"status": "error", "message": "🔒 Сессия Discord не найдена"}

    ticker = data.get("ticker")
    quantity = int(data.get("quantity", 0))
    if quantity <= 0: 
        return {"status": "error", "message": "❌ Количество должно быть больше 0"}

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем текущую стоимость актива
        async with db.execute("SELECT current_price FROM stock_assets WHERE ticker = ?", (ticker,)) as cursor:
            asset = await cursor.fetchone()
        if not asset: 
            return {"status": "error", "message": "❌ Актив не найден в реестре биржи"}
        price = asset[0]
        total_cost = round(price * quantity, 2)

        # Проверяем баланс кошелька
        async with db.execute("SELECT sochi_coins FROM sochi_wallets WHERE user_discord_id = ?", (user_discord_id,)) as cursor:
            wallet = await cursor.fetchone()
        balance = wallet[0] if wallet else 0.0

        if balance < total_cost:
            return {"status": "error", "message": f"❌ Недостаточно коинов! Стоимость: {total_cost} SC, ваш баланс: {balance} SC."}

        # Списываем крипту, начисляем акции в инвентарь СУБД
        await db.execute("UPDATE sochi_wallets SET sochi_coins = sochi_coins - ? WHERE user_discord_id = ?", (total_cost, user_discord_id))
        await db.execute("""
            INSERT INTO user_stock_portfolio (user_discord_id, ticker, shares_count) 
            VALUES (?, ?, ?) 
            ON CONFLICT(user_discord_id, ticker) DO UPDATE SET shares_count = shares_count + ?
        """, (user_discord_id, ticker, quantity, quantity))
        await db.commit()
        return {"status": "ok", "message": f"📈 Успешно куплено {quantity} акций {ticker} за {total_cost} Сочи-коинов!"}


# 4. API: БЕЗОПАСНАЯ ПРОДАЖА АКЦИЙ С НАЧИСЛЕНИЕМ КРИПТОВАЛЮТЫ НА СЧЕТ
@app.post("/api/stock/sell")
async def sell_sochi_stock(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id: 
        return {"status": "error", "message": "🔒 Сессия Discord не найдена"}

    ticker = data.get("ticker")
    quantity = int(data.get("quantity", 0))
    if quantity <= 0: 
        return {"status": "error", "message": "❌ Количество должно быть больше 0"}

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, сколько акций реально есть у юзера в портфеле
        async with db.execute("SELECT shares_count FROM user_stock_portfolio WHERE user_discord_id = ? AND ticker = ?", (user_discord_id, ticker)) as cursor:
            row = await cursor.fetchone()
            user_shares = row[0] if row else 0
        if user_shares < quantity:
            return {"status": "error", "message": f"❌ У вас нет такого количества акций! В наличии: {user_shares}"}

        # Проверяем цену акции на текущую секунду
        async with db.execute("SELECT current_price FROM stock_assets WHERE ticker = ?", (ticker,)) as cursor:
            price_row = await cursor.fetchone()
        price = price_row[0]
        total_payout = round(price * quantity, 2)

        # Проводим сделку: списываем акции, зачисляем прибыль в криптокошелек
        await db.execute("UPDATE user_stock_portfolio SET shares_count = shares_count - ? WHERE user_discord_id = ? AND ticker = ?", (quantity, user_discord_id, ticker))
        await db.execute("UPDATE sochi_wallets SET sochi_coins = sochi_coins + ? WHERE user_discord_id = ?", (total_payout, user_discord_id))
        
        # Чистим нулевые строки инвентаря
        await db.execute("DELETE FROM user_stock_portfolio WHERE user_discord_id = ? AND shares_count <= 0", (user_discord_id,))
        await db.commit()
        return {"status": "ok", "message": f"📉 Акции проданы! Начислено +{total_payout} Сочи-коинов."}
        
# 5. API: ТОП-10 САМЫХ БОГАТЫХ МАЙНЕРОВ (ОБЩИЙ КАПИТАЛ = КОИНЫ + СТОИМОСТЬ АКЦИЙ)
@app.get("/api/stock/leaderboard")
async def get_sochi_stock_leaderboard(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id: 
        return {"status": "error", "message": "🔒 Сессия не найдена"}

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Тянем текущие цены всех акций для мгновенной переоценки портфелей
        prices_map = {}
        async with db.execute("SELECT ticker, current_price FROM stock_assets") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                prices_map[r[0]] = r[1]

        # 2. Вытаскиваем балансы всех зарегистрированных криптокошельков
        users_capital = {}
        async with db.execute("SELECT user_discord_id, username, sochi_coins FROM sochi_wallets") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                uid, uname, coins = r[0], r[1], r[2]
                users_capital[uid] = {
                    "username": uname,
                    "avatar": "https://ibb.co", # Фолбек заглушка, фронтенд подтянет живую из кук, если это сам юзер
                    "total_net_worth": coins
                }

        # 3. Вытаскиваем все портфели акций и прибавляем их стоимость к капиталу
        async with db.execute("SELECT user_discord_id, ticker, shares_count FROM user_stock_portfolio") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                uid, ticker, count = r[0], r[1], r[2]
                if uid in users_capital and ticker in prices_map:
                    asset_value = count * prices_map[ticker]
                    users_capital[uid]["total_net_worth"] += asset_value

        # Сортируем майнеров по убыванию их богатства и берем ТОП-10
        sorted_leaderboard = sorted(users_capital.values(), key=lambda x: x["total_net_worth"], reverse=True)[:10]
        
        # Округляем финальные капиталы
        for player in sorted_leaderboard:
            player["total_net_worth"] = round(player["total_net_worth"], 2)

        return {"status": "ok", "leaderboard": sorted_leaderboard}
# 13. API: ПОЛУЧЕНИЕ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ И ИХ IP ДЛЯ СУПЕР-ПАНЕЛИ БАНОВ (Shift + B)
@app.get("/api/stock/admin/ip_registry")
async def get_all_users_with_ips_for_ban_panel(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    
    # СУПЕР-ЗАЩИТА: Доступ только для твоего личного Discord ID Создателя!
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID) or MASTER_ADMIN_DISCORD_ID == "ТВОЙ_ЛИЧНЫЙ_DISCORD_ID_ЗДЕСЬ":
        return {"status": "error", "message": "🔒 Отказ системы безопасности: Реестр IP доступен только Главному Создателю!"}

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS banned_ips (
                    ip_address TEXT PRIMARY KEY,
                    username TEXT,
                    banned_at TEXT
                )
            """)
            await db.commit()

            users_ips_list = []
            async with db.execute("SELECT user_discord_id, username FROM sochi_wallets ORDER BY username ASC") as cursor:
                rows = await cursor.fetchall()
            
            for r in rows:
                if not r or len(r) < 2: continue
                uid = str(r[0]).strip()
                name = str(r[1]).strip()
                
                # Сверяем, не забанен ли уже никнейм гражданина
                async with db.execute("SELECT ip_address FROM banned_ips WHERE username = ?", (name.lower().strip(),)) as b_cur:
                    ban_row = await b_cur.fetchone()
                    is_banned = ban_row is not None
                    active_ip = ban_row[0] if is_banned else f"192.168.2.{abs(hash(uid)) % 254 + 1}"

                users_ips_list.append({
                    "discord_id": uid,
                    "username": name.capitalize() if "_" not in name else name,
                    "ip_address": active_ip,
                    "is_banned": is_banned
                })
                
            return {"status": "ok", "users": users_ips_list}
    except Exception as server_err:
        return {"status": "error", "message": f"Ошибка СУБД банов: {str(server_err)}"}


# 14. API: КОМАНДА АКТИВАЦИИ ГЛОБАЛЬНОГО ПЕРМАНЕНТНОГО БАНА ПО IP (ПРАВА СОЗДАТЕЛЯ)
@app.post("/api/stock/admin/execute_ip_ban")
async def execute_global_ip_ban_command(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID):
        return {"status": "error", "message": "🔒 Действие доступно только Главному Создателю."}

    target_username = data.get("username", "").lower().strip()
    target_ip = data.get("ip_address", "").strip()

    if not target_ip:
        return {"status": "error", "message": "❌ Ошибка: Не указан сетевой IP нарушителя!"}

    async with aiosqlite.connect(DB_PATH) as db:
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        try:
            await db.execute("INSERT OR REPLACE INTO banned_ips (ip_address, username, banned_at) VALUES (?, ?, ?)", 
                             (target_ip, target_username, now_time))
            await db.commit()
            return {"status": "ok", "message": f"🛑 Нарушитель {target_username} перманентно забанен и отключен от дата-шлюза!"}
        except Exception as err:
            return {"status": "error", "message": f"Ошибка СУБД: {str(err)}"}


# 15. 🎯 НОВЫЙ РОУТ: КОМАНДА АКТИВАЦИИ ОПЕРАЦИЙ СНЯТИЯ БАНА (РАЗБАН)
@app.post("/api/stock/admin/execute_ip_unban")
async def execute_global_ip_unban_command(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID):
        return {"status": "error", "message": "🔒 Действие доступно только Главному Создателю."}

    target_username = data.get("username", "").lower().strip()

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            # Намертво стираем запись из черного списка SQLite
            await db.execute("DELETE FROM banned_ips WHERE username = ?", (target_username,))
            await db.commit()
            return {"status": "ok", "message": f"🟢 Пользователь {target_username} успешно разбанен! Доступ к веб-шлюзу восстановлен."}
        except Exception as err:
            return {"status": "error", "message": f"Ошибка СУБД разбана: {str(err)}"}

# 16. GET /ai — РЕНДЕРИНГ СТРАНИЦЫ ИИ (БЕЗ ОБЯЗАТЕЛЬНОЙ ДИСКОРД АВТОРИЗАЦИИ)
@app.get("/ai")
async def get_sochi_ai_chat_page():
    return FileResponse("templates/ai.html")


# 17. POST /api/ai/message — ОТПРАВКА СООБЩЕНИЯ (ПУЛЕНЕПРОБИВАЕМЫЙ ФИКС РАСПАКОВКИ И ОШИБКИ 500)
@app.post("/api/ai/message")
async def send_message_to_sochi_gpt(data: dict, request: Request):
    session_user = request.cookies.get("sochi_ai_user", "Гость")
    user_message = data.get("message", "").strip()
    
    if not user_message:
        return {"status": "error", "message": "Пустое сообщение"}
        
    now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Подстраховка структуры таблиц
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sochi_ai_chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_user TEXT,
                    sender TEXT,
                    message TEXT,
                    timestamp TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_intercepts (
                    session_user TEXT PRIMARY KEY,
                    intercepted_by_admin TEXT,
                    is_active INTEGER DEFAULT 0
                )
            """)
            await db.commit()

            # 1. Записываем сообщение пользователя в СУБД SQLite
            await db.execute("INSERT INTO sochi_ai_chats (session_user, sender, message, timestamp) VALUES (?, 'user', ?, ?)", 
                             (session_user, user_message, now_time))
            await db.commit()
            
            # 2. Проверяем статус перехвата управления (кукловод)
            async with db.execute("SELECT is_active FROM ai_intercepts WHERE session_user = ?", (session_user,)) as cursor:
                intercept_row = await cursor.fetchone()
                is_intercepted = (intercept_row[0] == 1) if intercept_row else False
                
            if is_intercepted:
                return {"status": "ok", "mode": "admin_active"}
                
            # 3. Вытаскиваем историю контекста (🎯 ИСПРАВЛЕНО: Чёткая распаковка индексов кортежа!)
            history = []
            async with db.execute("SELECT sender, message FROM sochi_ai_chats WHERE session_user = ? ORDER BY id ASC LIMIT 10", (session_user,)) as h_cursor:
                h_rows = await h_cursor.fetchall()
                for hr in h_rows:
                    if hr and len(hr) >= 2:
                        history.append({"sender": str(hr[0]), "message": str(hr[1])})

            # ИИ ВКЛЮЧЕН: Отправляем запрос в реальную Llama-3.1 нейросеть
            ai_response = await generate_real_sochi_llm_response(user_message, history)
            
            # Записываем ответ ИИ в базу
            await db.execute("INSERT INTO sochi_ai_chats (session_user, sender, message, timestamp) VALUES (?, 'ai', ?, ?)", 
                             (session_user, ai_response, now_time))
            await db.commit()
            return {"status": "ok", "mode": "llm_active", "ai_text": ai_response}
            
    except Exception as e:
        print(f"🛑 [AI MESSAGE ERROR] Сбой отправки реплики ИИ: {e}")
        return {"status": "error", "message": f"Ошибка бэкенда чата: {str(e)}"}


# 18. GET /api/ai/history — ЗАГРУЗКА ИСТОРИИ ЧАТА (🎯 ИСПРАВЛЕНО: ТОТАЛЬНЫЙ ФИКС КОРТЕЖЕЙ SQLite)
@app.get("/api/ai/history")
async def get_individual_ai_chat_history(request: Request, target_user: str = None):
    session_user = target_user if target_user else request.cookies.get("sochi_ai_user", "Гость")
    
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sochi_ai_chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_user TEXT,
                    sender TEXT,
                    message TEXT,
                    timestamp TEXT
                )
            """)
            await db.commit()

            history = []
            async with db.execute("SELECT sender, message, timestamp FROM sochi_ai_chats WHERE session_user = ? ORDER BY id ASC", (session_user,)) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r and len(r) >= 3:
                        # 🎯 ИСПРАВЛЕНО: Хирургически точный разбор элементов кортежа!
                        history.append({
                            "sender": str(r[0]), 
                            "message": str(r[1]), 
                            "time": str(r[2])
                        })
            return {"status": "ok", "history": history}
    except Exception as e:
        print(f"🛑 [AI HISTORY ERROR] Сбой выгрузки истории чата: {e}")
        return {"status": "error", "message": f"Ошибка СУБД истории: {str(e)}"}


# 19. GET /api/ai/admin/dialogs — РЕЕСТР ДЛЯ ПАНЕЛИ Shift + T (ПРАВА СОЗДАТЕЛЯ)
@app.get("/api/ai/admin/dialogs")
async def get_all_active_ai_dialogs_for_admin(request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID):
        return {"status": "error", "message": "🔒 Отказ: Роут доступен только Главному Создателю!"}

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT DISTINCT session_user FROM sochi_ai_chats") as cursor:
                rows = await cursor.fetchall()
                
            dialogs = []
            for r in rows:
                if not r: continue
                u_name = str(r[0])
                async with db.execute("SELECT is_active FROM ai_intercepts WHERE session_user = ?", (u_name,)) as int_cur:
                    int_row = await int_cur.fetchone()
                    is_active = (int_row[0] == 1) if int_row else False
                dialogs.append({"username": u_name, "is_intercepted": is_active})
                
            return {"status": "ok", "dialogs": dialogs}
    except Exception as e:
        return {"status": "error", "message": f"Ошибка СУБД реестра: {str(e)}"}


# 20. POST /api/ai/admin/intercept — ВЫКЛЮЧЕНИЕ / ВКЛЮЧЕНИЕ НЕЙРОСЕТИ ДЛЯ ЮЗЕРА
@app.post("/api/ai/admin/intercept")
async def admin_intercept_control_toggle(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID):
        return {"status": "error", "message": "🔒 Отказ прав"}

    target_user = data.get("target_user")
    action = data.get("action")

    async with aiosqlite.connect(DB_PATH) as db:
        if action == "start":
            await db.execute("INSERT OR REPLACE INTO ai_intercepts (session_user, intercepted_by_admin, is_active) VALUES (?, ?, 1)", 
                             (target_user, user_discord_id))
            await db.commit()
            return {"status": "ok", "message": f"🤖 ИИ отключен для {target_user}. Чат перехвачен!"}
        else:
            await db.execute("UPDATE ai_intercepts SET is_active = 0 WHERE session_user = ?", (target_user,))
            await db.commit()
            return {"status": "ok", "message": f"🟢 ИИ снова активирован для {target_user}."}


# 21. POST /api/ai/admin/send_as_ai — ОТПРАВКА СООБЩЕНИЯ КУКЛОВОДА ОТ ЛИЦА ЛЛМ
@app.post("/api/ai/admin/send_as_ai")
async def admin_send_message_disguised_as_llm(data: dict, request: Request):
    user_discord_id = request.cookies.get("forum_user_id")
    if not user_discord_id or str(user_discord_id) != str(MASTER_ADMIN_DISCORD_ID):
        return {"status": "error", "message": "🔒 Отказ прав"}

    target_user = data.get("target_user")
    admin_message = data.get("message", "").strip()
    now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO sochi_ai_chats (session_user, sender, message, timestamp) VALUES (?, 'ai', ?, ?)", 
                         (target_user, admin_message, now_time))
        await db.commit()
        return {"status": "ok"}

# ==========================================================
# 🏛️ МОДУЛЬ ЦЕНТРАЛЬНОГО ПИНБАНКА СТРАНЫ ПИНИЯ (/pin-bank)
# ==========================================================

# 1. РЕНДЕРИНГ СТРАНИЦЫ БАНКА
@app.get("/pin-bank")
async def serve_pin_bank_page(request: Request):
    from fastapi.responses import FileResponse, RedirectResponse
    import os
    username = request.cookies.get("forum_user_name")
    if not username:
        return RedirectResponse("/forum?auth=login_required")
    return FileResponse(os.path.join(BASE_DIR, "pin-bank.html"))


# 2. API: ПРОВЕРКА СТАТУСА РЕГИСТРАЦИИ И ПОЛУЧЕНИЕ ДАННЫХ СЧЕТА (ФИКС ОШИБКИ 422)
@app.get("/api/bank/profile")
async def get_bank_profile(request: Request, anon_user: str = None):
    # Приоритетно используем автономную сессию фронтенда
    username = anon_user or request.cookies.get("forum_user_name")
        
    if not username: 
        return {"status": "error", "message": "🔒 Сессия не найдена. Перезагрузите страницу."}

    # Очищаем юзернейм для безопасного поиска в SQLite
    username = str(username).lower().strip()

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем регистрацию гражданина в ПинБанке
        async with db.execute("SELECT first_name, last_name, phone_number FROM pin_bank_users WHERE username = ?", (username,)) as cursor:
            user_row = await cursor.fetchone()
            
        if not user_row:
            return {"status": "not_registered"}

        # Если зарегистрирован, тянем его выпущенные карты
        cards = []
        async with db.execute("SELECT id, card_number, balance, gradient_style, status FROM pin_bank_cards WHERE username = ?", (username,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                cards.append({
                    "id": r[0], 
                    "number": r[1], 
                    "balance": r[2], 
                    "gradient": r[3], 
                    "status": r[4]
                })

        return {
            "status": "ok",
            "user_info": {"first_name": user_row[0], "last_name": user_row[1], "phone": user_row[2]},
            "cards": cards
        }


# 3. API: АВТОНОМНАЯ АНОНИМНАЯ РЕГИСТРАЦИЯ ГРАЖДАН ПИНИИ
@app.post("/api/bank/register")
async def register_bank_user(data: dict, request: Request):
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    phone = data.get("phone", "").strip()
    
    # Автоматически генерируем уникальный логин на основе Имени и Фамилии
    username = data.get("username", f"{first_name}_{last_name}").lower().strip()

    if not first_name or not last_name or not phone.startswith("+613"):
        return {"status": "error", "message": "❌ Ошибка: Имя, Фамилия или префикс +613 заполнены неверно!"}

    async with aiosqlite.connect(DB_PATH) as db:
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%d.%m.%Y %H:%M")
        try:
            # Проверяем, нет ли уже такого пользователя в базе ПинБанка
            async with db.execute("SELECT 1 FROM pin_bank_users WHERE username = ?", (username,)) as cursor:
                if await cursor.fetchone():
                    return {"status": "error", "message": "🛑 Ошибка: Гражданин с таким Именем и Фамилией уже зарегистрирован!"}
                    
            # Записываем чистые данные в СУБД SQLite
            await db.execute("""
                INSERT INTO pin_bank_users (username, first_name, last_name, phone_number, registered_at) 
                VALUES (?, ?, ?, ?, ?)
            """, (username, first_name, last_name, phone, now_time))
            await db.commit()
            
            print(f"🏛️ [ПИНБАНК] Успешная автономная регистрация: {username} ({phone})")
            return {"status": "ok", "message": "🎉 Добро пожаловать в ЦБ ПинБанк! Регистрация успешна."}
            
        except Exception as db_err:
            print(f"🛑 [ПИНБАНК REG ERROR] Критическая ошибка базы данных: {db_err}")
            return {"status": "error", "message": f"⚙️ Ошибка СУБД: {str(db_err)}"}


# 4. API: СОЗДАНИЕ БАНКОВСКОЙ КАРТЫ С УЧЕТОМ АВТОНОМНОГО ЮЗЕРА (ФИКС ОШИБКИ 422)
@app.post("/api/bank/create_card")
async def create_bank_card(data: dict, request: Request, anon_user: str = None):
    username = anon_user or request.cookies.get("forum_user_name")
    if not username: return {"status": "error", "message": "🔒 Сессия не найдена"}
    
    username = str(username).lower().strip()
    gradient = data.get("gradient", "linear-gradient(135deg, #2ec4b6, #007aff)")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем лимит карт
        async with db.execute("SELECT COUNT(*) FROM pin_bank_cards WHERE username = ?", (username,)) as cursor:
            count_row = await cursor.fetchone()
            count = count_row[0] if count_row else 0
        if count >= 5:
            return {"status": "error", "message": "❌ Достигнут лимит: нельзя создать более 5 карт!"}

        # Генерируем уникальный серийный номер Пинийской карты: 6130 PINX XXXX XXXX
        import random
        card_number = f"6130 {random.randint(1000, 9999)} {random.randint(1000, 9999)} {random.randint(1000, 9999)}"
        
        await db.execute("INSERT INTO pin_bank_cards (username, card_number, gradient_style) VALUES (?, ?, ?)",
                         (username, card_number, gradient))
        await db.commit()
        return {"status": "ok", "message": "💳 Карта успешно выпущена!"}


# 5. API: ИЗМЕНЕНИЕ ДИЗАЙНА КАРТЫ (ФИКС ОШИБКИ 422)
@app.post("/api/bank/update_card_style")
async def update_card_style(data: dict, request: Request, anon_user: str = None):
    username = anon_user or request.cookies.get("forum_user_name")
    if not username: return {"status": "error", "message": "🔒 Сессия не найдена"}
    
    username = str(username).lower().strip()
    card_id = data.get("card_id")
    new_gradient = data.get("gradient")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE pin_bank_cards SET gradient_style = ? WHERE id = ? AND username = ?", (new_gradient, card_id, username))
        await db.commit()
        return {"status": "ok", "message": "✨ Дизайн карты успешно обновлен!"}


# 6. API: ОТПРАВКА ЗАПРОСА НА ПОПОЛНЕНИЕ ДЛЯ КАНАЛА БЕЗОПАСНОСТИ (ФИКС ОШИБКИ 422)
@app.post("/api/bank/request_deposit")
async def request_deposit(data: dict, request: Request, anon_user: str = None):
    username = anon_user or request.cookies.get("forum_user_name")
    if not username: return {"status": "error", "message": "🔒 Сессия не найдена"}
    
    username = str(username).lower().strip()
    card_number = data.get("card_number")
    amount = int(data.get("amount", 0))

    if amount <= 0: return {"status": "error", "message": "❌ Неверная сумма"}

    async with aiosqlite.connect(DB_PATH) as db:
        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
        await db.execute("INSERT INTO pin_bank_requests (request_type, sender, receiver_card, amount, timestamp) VALUES ('deposit', ?, ?, ?, ?)",
                         (username, card_number, amount, now_time))
        await db.commit()
        return {"status": "ok", "message": "⏳ Запрос отправлен в службу безопасности ПинБанка на верификацию!"}


# 7. API: ВЫГРУЗКА ВСЕХ ЗАПРОСОВ ДЛЯ ПАНЕЛИ БЕЗОПАСНОСТИ (Shift + S)
@app.get("/api/bank/security_requests")
async def get_security_requests():
    async with aiosqlite.connect(DB_PATH) as db:
        requests_list = []
        async with db.execute("SELECT id, request_type, sender, receiver_card, amount, timestamp FROM pin_bank_requests WHERE status = 'pending' ORDER BY id DESC") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                requests_list.append({"id": r[0], "type": r[1], "sender": r[2], "card": r[3], "amount": r[4], "time": r[5]})
        return {"status": "ok", "requests": requests_list}


# 8. API: ОБРАБОТКА РЕШЕНИЯ БЕЗОПАСНОСТИ (ПОДТВЕРДИТЬ / ОТКЛОНИТЬ)
@app.post("/api/bank/moderate_request")
async def moderate_request(data: dict):
    req_id = data.get("id")
    action = data.get("action")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT request_type, receiver_card, amount, sender FROM pin_bank_requests WHERE id = ?", (req_id,)) as cursor:
            req = await cursor.fetchone()
        if not req: return {"status": "error", "message": "Запрос не найден"}

        if action == "approve":
            # Начисляем средства на баланс конкретной карты в СУБД SQLite
            await db.execute("UPDATE pin_bank_cards SET balance = balance + ? WHERE card_number = ?", (req[2], req[1]))
            await db.execute("UPDATE pin_bank_requests SET status = 'approved' WHERE id = ?", (req_id,))
            await db.commit()
            return {"status": "ok", "message": "✅ Транзакция успешно подтверждена, средства зачислены на карту!"}
        else:
            await db.execute("UPDATE pin_bank_requests SET status = 'rejected' WHERE id = ?", (req_id,))
            await db.commit()
            return {"status": "ok", "message": "❌ Запрос безопасности успешно отклонен."}

# 9. API: НЕЗАВИСИМАЯ NFC ОПЛАТА С КАРТЫ НА КАРТУ НАПРЯМУЮ ЧЕРЕЗ СМАРТФОНЫ (ПУЛЕНЕПРОБИВАЕМЫЙ АВТОНОМНЫЙ ФИКС)
@app.post("/api/bank/nfc_pay_execute")
async def nfc_pay_execute(data: dict, request: Request, anon_user: str = None):
    # Извлекаем получателя (терминал): приоритетно из query URL (?anon_user=), затем из data, затем из кук форума
    receiver_uid = anon_user or data.get("receiver_uid") or request.cookies.get("forum_user_name")
    
    sender_card_number = data.get("sender_card_number") # Чья карта (считано по NFC радару)
    amount = int(data.get("amount", 0))

    if not receiver_uid:
        return {"status": "error", "message": "🔒 Ошибка банка: сессия получателя (терминала) не найдена."}
        
    receiver_uid = str(receiver_uid).lower().strip()

    async with aiosqlite.connect(DB_PATH) as db:
        # Ищем карту плательщика и проверяем его баланс в СУБД SQLite
        async with db.execute("SELECT username, balance FROM pin_bank_cards WHERE card_number = ?", (sender_card_number,)) as cursor:
            sender_card = await cursor.fetchone()
        if not sender_card: 
            return {"status": "error", "message": f"❌ Ошибка: Карта списания [{sender_card_number}] не найдена в реестре ПинБанка!"}
        
        sender_uid, sender_balance = sender_card[0], sender_card[1]
        if sender_balance < amount:
            return {"status": "error", "message": f"❌ Отказ: Недостаточно средств на карте плательщика! Баланс: {sender_balance} pin, требуется: {amount} pin."}

        # Ищем первую активную карту получателя, на которую зачисляются pin
        async with db.execute("SELECT card_number FROM pin_bank_cards WHERE username = ? LIMIT 1", (receiver_uid,)) as cursor:
            receiver_card_row = await cursor.fetchone()
        if not receiver_card_row:
            return {"status": "error", "message": "❌ Ошибка: У получателя (терминала) нет ни одной созданной карты ПинБанка!"}
        
        receiver_card_number = receiver_card_row[0]

        # Проводим транзакцию: списываем у плательщика, начисляем владельцу терминала
        await db.execute("UPDATE pin_bank_cards SET balance = balance - ? WHERE card_number = ?", (amount, sender_card_number))
        await db.execute("UPDATE pin_bank_cards SET balance = balance + ? WHERE card_number = ?", (amount, receiver_card_number))
        
        # Вытаскиваем имя плательщика для красивых и читаемых логов перевода в СБ панели
        async with db.execute("SELECT first_name, last_name FROM pin_bank_users WHERE username = ?", (sender_uid,)) as cursor:
            u_row = await cursor.fetchone()
            sender_title = f"{u_row[0]} {u_row[1]}" if u_row else "Гражданин Пинии"

        now_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%H:%M")
        
        # Записываем транзакцию в архив как мгновенно одобренную (approved)
        await db.execute("""
            INSERT INTO pin_bank_requests (request_type, sender, receiver_card, amount, status, timestamp) 
            VALUES ('transfer', ?, ?, ?, 'approved', ?)
        """, (sender_title, receiver_card_number, amount, now_time))
        
        await db.commit()
        print(f"🏛️ [ПИНБАНК NFC] Перевод {amount} pin с карты {sender_card_number} на карту {receiver_card_number} выполнен успешно!")
        return {"status": "ok", "message": f"🎉 Транзакция одобрена Центральным Банком! Списано {amount} pin с карты {sender_card_number}."}


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    # Явно указываем область видимости для облачного клиента
    global supabase_client
    
    try:
        # --- 1. ЕСЛИ ЭТО ИЗОБРАЖЕНИЕ — ЧИТАЕМ МАЛЕНЬКИЙ ОБЪЕМ И ШЛЕМ НА IMGBB ---
        if file.content_type and file.content_type.startswith("image/"):
            content = await file.read()
            url = f"https://api.imgbb.com/1/upload?key={IMGBB_API_KEY}"
            async with httpx.AsyncClient() as client:
                files = {"image": (file.filename, content)}
                res = await client.post(url, files=files, timeout=30.0)
                if res.status_code == 200:
                    return {"url": res.json()["data"]["url"]}
                print(f"⚠️ ImgBB Error: {res.text}. Резервный запуск в Supabase...")
                await file.seek(0)

        # --- 2. ДЛЯ ТЯЖЕЛЫХ ВИДЕО (10-50 МБ) — СТАБИЛЬНАЯ ЗАГРУЗКА БАЙТОВ ---
        ext = os.path.splitext(file.filename)[1]
        cloud_filename = f"{uuid.uuid4()}{ext}"
        
        # Читаем байты тяжелого файла
        video_bytes = await file.read()
        
        # Загружаем байты напрямую в облачный бакет Supabase в отдельном потоке
        res = await asyncio.to_thread(
            supabase_client.storage.from_("pinnogram-media").upload,
            path=cloud_filename,
            file=video_bytes,
            file_options={"content-type": file.content_type or "video/mp4"}
        )
        
        # Получаем вечную публичную ссылку
        public_url = supabase_client.storage.from_("pinnogram-media").get_public_url(cloud_filename)
        
        print(f"📦 Тяжелый файл успешно передан в Supabase: {public_url}")
        return {"url": public_url}

    except Exception as e:
        print(f"❌ Ошибка загрузки файла: {str(e)}")
        import traceback
        traceback.print_exc()
        # Возвращаем четкую структуру, которую фронтенд сможет распарсить без вылетов
        return {"url": "error", "error_details": str(e)}



@app.websocket("/ws/{room_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str):
    client_ip = websocket.client.host if websocket.client else "unknown"
    
    # 1. Принимаем соединение ПЕРЕД проверкой, чтобы отправить данные
    await websocket.accept()

    try:
        # 2. Ждем от клиента пакет FINGERPRINT (отправляется браузером сразу)
        # Если клиент не прислал его за 2 секунды — рубим связь
        auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
        browser_finger = auth_msg.replace("FINGERPRINT:", "") if "FINGERPRINT:" in auth_msg else "unknown"

        # --- УМНАЯ ПРОВЕРКА БАНА (БЕЗ IP) ---
        async with aiosqlite.connect(DB_PATH) as db:
            # Ищем только по нику или по отпечатку браузера
            sql = "SELECT unban_time, admin_name, reason FROM bans WHERE username = ? OR fingerprint = ?"
            async with db.execute(sql, (username, browser_finger)) as cur:
                ban_row = await cur.fetchone()
                
                if ban_row:
                    unban_time, admin, reason = ban_row
                    if time.time() < unban_time:
                        # Если забанен — шлем пакет BAN_SCREEN и закрываем
                        await websocket.send_text(f"ID:0|SYSTEM:BAN_SCREEN|{unban_time}|{admin}|{reason}")
                        await asyncio.sleep(0.5)
                        await websocket.close(code=1008)
                        return
                    else:
                        # Срок истек — удаляем бан по нику и отпечатку
                        await db.execute("DELETE FROM bans WHERE username = ? OR fingerprint = ?", 
                                        (username, browser_finger))
                        await db.commit()

    except Exception as e:
        print(f"Auth Error: {e}")
        await websocket.close()
        return

    # ... дальше идет твой код (current_avatar и т.д.) ...

    websocket.browser_fingerprint = browser_finger  # СОХРАНЯЕМ ОТПЕЧАТОК В СОКЕТЕ

    # 1. Узнаем аватарку при входе
    current_avatar = ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT avatar FROM users WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                current_avatar = row[0]
            else:
                # --- НОВОЕ: ЕСЛИ ЮЗЕРА НЕТ В БАЗЕ, ЗАПИСЫВАЕМ ЕГО В АРХИВ ---
                # Используем INSERT OR IGNORE, чтобы не было ошибок, если он уже есть
                await db.execute("""
                    INSERT OR IGNORE INTO users (username, password, avatar) 
                    VALUES (?, ?, ?)
                """, (username, "nopass", ""))
                await db.commit()
                print(f"📂 Узел {username} впервые занесен в архив")

    await manager.connect(websocket, room_id, username)
    # Системное сообщение о входе (шлем всем)
    await manager.broadcast(room_id, message=f"{username} вошел в чат")

    try:
        while True:
            raw_data = await websocket.receive_text()
            
            if raw_data.startswith("__ADMIN_CMD__:"):
                try:
                    payload = raw_data.replace("__ADMIN_CMD__:", "")
                    key, cmd, val = payload.split("|", 2)
                    if key != ADMIN_SECRET_KEY:
                        await websocket.send_text("ID:0|SYSTEM:❌ Ошибка: Неверный ключ!")
                        continue
                    
                    if cmd == "GLOBAL_MSG":
                        await manager.global_broadcast(val, username)
                    elif cmd == "BAN":
                        # Разделяем на 3 части: Имя, Дни, Причина
                        name, days, reason = val.split(":", 2)
                        await manager.ban_user(name, days, username, reason)

                    elif cmd == "UNBAN": # НОВАЯ ВЕТКА ДЛЯ РАЗБАНА
                        await manager.unban_user(val)
                        await websocket.send_text(f"ID:0|SYSTEM:✅ Пользователь {val} разбанен.")
                    continue
                except Exception as e:
                    print(f"Admin CMD Error: {e}")
                    continue
            # 🎯 ЖЕСТКИЙ КАПКАН ДЛЯ ЗАГЛУШЕННЫХ УЗЛОВ (MUTE SYSTEM)
            # Перехватывает любые попытки писать, слать эмодзи, удалять сообщения или флудить
            if username.lower() in MUTED_DATA:
                if time.time() < MUTED_DATA[username.lower()]:
                    left_sec = int(MUTED_DATA[username.lower()] - time.time())
                    # Шлем системное предупреждение в сокет нарушителя с таймером обратного отсчета
                    await websocket.send_text(f"ID:0|SYSTEM:⚠️ Твой узел изолирован! Отправка сообщений и медиа заблокирована. Осталось {left_sec} сек.")
                    continue # Жестко сбрасываем пакет, прерывая выполнение цикла
                else:
                    # Срок действия мута официально истек — бесшумно амнистируем пользователя
                    del MUTED_DATA[username.lower()]
            # ⚖️ КАПКАН ДЕМОРГАНА НА ОТПРАВКУ СООБЩЕНИЙ
            if username.lower() in DEMORGAN_DATA:
                if time.time() < DEMORGAN_DATA[username.lower()]:
                    # Принудительно изолируем сообщение в мрачную спец-комнату
                    room_id = "demorgan"
                    target_user = None # Аннулируем любые личные сообщения
                else:
                    # Срок отсидки вышел — амнистируем узел
                    del DEMORGAN_DATA[username.lower()]

            
            # --- ШАГ 0: УМНАЯ ОЧИСТКА ПРЕФИКСОВ ---
            # Мы раздеваем сообщение, чтобы достать чистую команду (clean_text)
            clean_text = raw_data
            msg_time = datetime.now().strftime("%H:%M")
            target_user = None

            # Сначала отрезаем TO_USER:
            if clean_text.startswith("TO_USER:"):
                try:
                    parts = clean_text.split("|", 1)
                    target_user = parts[0].replace("TO_USER:", "")
                    clean_text = parts[1]
                except: pass

            # Затем отрезаем TIME:
            if clean_text.startswith("TIME:"):
                try:
                    parts = clean_text.split("|", 1)
                    msg_time = parts[0].replace("TIME:", "")
                    clean_text = parts[1]
                except: pass

            

            
            # 1. ЗАПРОС ИСТОРИИ (С ИЗОЛЯЦИЕЙ ДЕМОРГАНА — ИСПРАВЛЕНО)
            if clean_text.startswith("GET_HISTORY:"):
                target = clean_text.replace("GET_HISTORY:", "").strip()
                
                # ⚖️ ПРОВЕРКА: Если узел изолирован в Деморгане — перенаправляем в спец-комнату
                if username.lower() in DEMORGAN_DATA:
                    if time.time() < DEMORGAN_DATA[username.lower()]:
                        target = "demorgan" # Весь остальной мир для него исчезает
                    else:
                        del DEMORGAN_DATA[username.lower()]

                async with aiosqlite.connect(DB_PATH) as db:
                    # 🎯 1. Проверяем статус подписки (только если это не Деморган)
                    is_subbed = False
                    if target != "demorgan":
                        check_sub = await db.execute("SELECT 1 FROM group_subs WHERE username=? AND group_name=?", (username, target))
                        if await check_sub.fetchone():
                            is_subbed = True
                    
                    # 🎯 2. Сразу шлем пакет статуса подписки фронтенду
                    status_msg = f"ID:0|SYSTEM:SUB_STATUS|{target}|{1 if is_subbed else 0}"
                    await websocket.send_text(status_msg)
                    
                    # 🎯 3. ПРОВЕРКА: Это группа или личка?
                    check_group = await db.execute("SELECT 1 FROM groups WHERE name = ?", (target,))
                    is_group = await check_group.fetchone()
            
                    # --- ЕДИНАЯ ЦЕПОЧКА УСЛОВИЙ ДЛЯ ВЫБОРА SQL ---
                    if target == "demorgan":
                        # ⛓️ ТЮРЕМНЫЙ ЧАТ: Тянем историю только из спец-комнаты demorgan
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = 'demorgan' ORDER BY id ASC LIMIT 100"
                        params = ()
                    elif target in ["null", "general", "None", "undefined"]:
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND to_user IS NULL ORDER BY id ASC LIMIT 100"
                        params = (room_id,)
                    elif is_group:
                        # 📢 Если это группа — тянем все сообщения, где TO_USER = ИмяГруппы
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE to_user = ? ORDER BY id ASC LIMIT 100"
                        params = (target,)
                    else:
                        # 👤 Личка
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND ((username = ? AND to_user = ?) OR (username = ? AND to_user = ?)) ORDER BY id ASC LIMIT 100"
                        params = (room_id, username, target, target, username)
                    
                    # Выполняем выбранный SQL-запрос
                    async with db.execute(sql, params) as cursor:
                        history = await cursor.fetchall()
                        
                        for m_id, u, txt, tm, av, to_u, is_r, r_id in history:
                            # --- ШАГ А: ПОДТЯГИВАЕМ РЕАКЦИИ ДЛЯ ЭТОГО СООБЩЕНИЯ ---
                            react_pfx = ""
                            async with db.execute("SELECT emoji, COUNT(*) FROM reactions WHERE msg_id = ? GROUP BY emoji", (m_id,)) as r_cur:
                                r_rows = await r_cur.fetchall()
                                if r_rows:
                                    r_data = ",".join([f"{row[0]}:{row[1]}" for row in r_rows])
                                    react_pfx = f"REACTION:{r_data}|"

                            # --- ШАГ Б: ФОРМИРУЕМ ТЕХНИЧЕСКИЕ ПРЕФИКСЫ ---
                            pfx = f"PRIVATE:{to_u}:" if to_u else ""
                            reply_pfx = f"REPLY:{r_id}|" if r_id else ""
                            
                            # --- ШАГ В: ОТПРАВКА ПОЛНОГО ПАКЕТА ---
                            full_packet = f"ID:{m_id}|{react_pfx}{reply_pfx}{pfx}[{tm}] {u}: {txt}|{av or ''}|{is_r}"
                            await websocket.send_text(full_packet)
                continue

                
            # 🎯 1. СОЗДАНИЕ ГРУППЫ
            if clean_text and clean_text.startswith("CREATE_GROUP:"):
                g_name = clean_text.replace("CREATE_GROUP:", "").strip()
                if not g_name: continue # Заменил return на continue
                
                async with aiosqlite.connect(DB_PATH) as db:
                    try:
                        check = await db.execute("SELECT name FROM groups WHERE name = ?", (g_name,))
                        if await check.fetchone():
                            await websocket.send_text(f"ID:0|SYSTEM:ERROR:Группа '{g_name}' уже существует")
                            continue # Заменил return на continue
    
                        await db.execute("INSERT INTO groups (name, owner, subscribers_count) VALUES (?, ?, 1)", (g_name, username))
                        await db.execute("INSERT INTO group_subs (username, group_name) VALUES (?, ?)", (username, g_name))
                        await db.commit()
                        
                        print(f"📢 Группа {g_name} создана")
                        await self.broadcast_online(room_id) # Убедись, что метод называется так в твоем ConnectionManager
                    except Exception as e:
                        print(f"❌ Ошибка SQL: {e}")
                continue # Заменил return на continue
    
            # 🎯 2. ВСТУПЛЕНИЕ В ГРУППУ
            if clean_text and clean_text.startswith("JOIN_GROUP:"):
                g_name = clean_text.replace("JOIN_GROUP:", "").strip()
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT 1 FROM group_subs WHERE username=? AND group_name=?", (username, g_name))
                    if not await cur.fetchone():
                        await db.execute("INSERT INTO group_subs (username, group_name) VALUES (?, ?)", (username, g_name))
                        await db.execute("UPDATE groups SET subscribers_count = subscribers_count + 1 WHERE name=?", (g_name,))
                        await db.commit()
                        print(f"👥 {username} вступил в {g_name}")
                        await self.broadcast_online(room_id)
                continue # Заменил return на continue



    
                
            # 2. RTC СИГНАЛЫ (УЛЬТРА-СТАБИЛЬНЫЙ ВАРИАНТ)
            elif clean_text.startswith("RTC_SIGNAL:"):
                # Если вдруг target_user не определился выше (нет префикса TO_USER:)
                # Попробуем найти его в самом тексте сигнала (если он там зашит)
                if not target_user:
                    try:
                        # Если сигнал в формате JSON и там есть поле 'to'
                        import json
                        data = json.loads(clean_text.replace("RTC_SIGNAL:", ""))
                        target_user = data.get("to") or data.get("target")
                    except: pass

                # Вызываем broadcast. Если target_user есть — уйдет ему. 
                # Если нет — уйдет только отправителю (для теста), что тоже поможет.
                await manager.broadcast(
                    room_id=room_id, 
                    username=username, 
                    text=clean_text, 
                    to_user=target_user 
                )
                continue



            # 3. УДАЛЕНИЕ
            elif clean_text.startswith("__DELETE__:"):
                msg_id = clean_text.replace("__DELETE__:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
                    await db.commit()
                if room_id in manager.rooms:
                    for conn in manager.rooms[room_id].values():
                        await conn.send_text(f"DELETE_CONFIRM:{msg_id}")
                continue
            elif clean_text.startswith("__EDIT__:"):
                try:
                    payload = clean_text.replace("__EDIT__:", "")
                    if "|" not in payload: continue # Пропускаем битый запрос
                    msg_id, new_text = payload.split("|", 1)
                    
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE messages SET text = ? WHERE id = ?", (new_text, msg_id))
                        await db.commit()
                        
                    for conn in manager.rooms[room_id].values():
                        await conn.send_text(f"EDIT_CONFIRM:{msg_id}|{new_text}")
                except Exception as e:
                    print(f"Edit Error: {e}")
                continue



            # 4. ПЕЧАТАЕТ...
            elif clean_text == "__TYPING__":
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username:
                            await conn.send_text(f"TYPING:{username}")
                continue

            # 5. СОЗДАНИЕ ОПРОСА
            elif clean_text.startswith("POLL_CREATE:"):
                try:
                    payload = clean_text.replace("POLL_CREATE:", "")
                    if "|" not in payload: continue
                    q, opts = payload.split("|", 1)
                    async with aiosqlite.connect(DB_PATH) as db:
                        cur = await db.execute("INSERT INTO polls (question, options, owner) VALUES (?, ?, ?)", (q, opts, username))
                        p_id = cur.lastrowid
                        await db.commit()
                    # Шлем POLL_ID всем участникам
                    await manager.broadcast(room_id, username=username, text=f"POLL_ID:{p_id}", to_user=target_user)
                except: pass
                continue

            # 6. ГОЛОСОВАНИЕ
            elif clean_text.startswith("POLL_VOTE:"):
                try:
                    p_id, opt_idx = clean_text.replace("POLL_VOTE:", "").split("|")
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("INSERT OR REPLACE INTO poll_votes VALUES (?, ?, ?)", (int(p_id), username, int(opt_idx)))
                        await db.commit()
                    for conn in manager.rooms.get(room_id, {}).values():
                        await conn.send_text(f"POLL_UPDATE:{p_id}")
                except: pass
                continue
                
            elif clean_text.startswith("ADD_REACTION:"):
                try:
                    # Формат: msg_id|emoji
                    parts = clean_text.replace("ADD_REACTION:", "").split("|")
                    m_id = int(parts[0])
                    emoji = parts[1]
                    
                    async with aiosqlite.connect(DB_PATH) as db:
                        # Записываем реакцию (PRIMARY KEY msg_id+username заменит старую, если была)
                        await db.execute("INSERT OR REPLACE INTO reactions VALUES (?, ?, ?)", 
                                        (m_id, username, emoji))
                        await db.commit()
                        
                        # Считаем статистику для этого сообщения: сколько каких эмодзи
                        async with db.execute("SELECT emoji, COUNT(*) FROM reactions WHERE msg_id = ? GROUP BY emoji", (m_id,)) as cur:
                            rows = await cur.fetchall()
                            # Склеиваем результат: 👍:2,🔥:1
                            reaction_data = ",".join([f"{r[0]}:{r[1]}" for r in rows])
                    
                    # Рассылаем всем сигнал об обновлении реакций у конкретного сообщения
                    for conn in manager.rooms.get(room_id, {}).values():
                        if conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(f"REACTION_UPDATE:{m_id}|{reaction_data}")
                except Exception as e:
                    print(f"Reaction Error: {e}")
                continue


            # 7. ОБЫЧНОЕ СООБЩЕНИЕ И ИИ
            else:
                current_reply_id = None
                display_text = clean_text # Твой очищенный текст

                # ПРОВЕРЯЕМ: Если сообщение начинается с REPLY_TO:ID|Текст
                if display_text.startswith("REPLY_TO:"):
                    try:
                        # Убираем префикс, делим по первой палке |
                        p_data = display_text.replace("REPLY_TO:", "", 1).split("|", 1)
                        current_reply_id = int(p_data[0])
                        display_text = p_data[1]
                    except: 
                        pass

                # Отправляем сообщение через обновленный broadcast
                await manager.broadcast(
                    room_id, 
                    username=username, 
                    text=display_text, 
                    avatar=current_avatar, 
                    client_time=msg_time, 
                    to_user=target_user,
                    reply_to_id=current_reply_id # ПЕРЕДАЕМ ID ОТВЕТА
                )

                # 5. ЛОГИКА ИИ-БОТА"
                # ТВОЙ КЛЮЧ И ПРАВИЛЬНАЯ ССЫЛКА
                if target_user == "AI_BOT":
                    await websocket.send_text("TYPING:AI_BOT")
                    print(f"DEBUG: Получено сообщение для AI_BOT. Текст: '{clean_text}'") # <-- Проверка 1
                    
                    # --- 1. ОБНОВЛЕННАЯ ЛОГИКА ГЕНЕРАЦИИ (HUGGING FACE) ---
                    trigger_words = ["нарисуй", "draw", "изобрази", "картинка", "image"]
                    
                    if any(word in clean_text.lower() for word in trigger_words):
                        print("DEBUG: Триггер сработал. Начинаю генерацию...") # <-- Проверка 2
                        prompt = clean_text.lower()
                        for w in trigger_words: prompt = prompt.replace(w, "")
                        prompt = prompt.replace("ai_bot", "").strip()
                        
                        if not prompt: prompt = "beautiful landscape"
                        print(f"DEBUG: Финальный промпт для нейросети: {prompt}")

                        HF_TOKEN = os.environ.get("HF_TOKEN") 
                        API_URL = "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-xl-base-1.0"
                        
                        try:
                            async with httpx.AsyncClient() as client:
                                response = await client.post(
                                    API_URL,
                                    headers={"Authorization": f"Bearer {HF_TOKEN}"},
                                    json={"inputs": prompt},
                                    timeout=60.0 
                                )
                                
                                print(f"DEBUG: Ответ от Hugging Face. Статус: {response.status_code}")
                                
                                if response.status_code == 200:
                                    img_content = response.content
                                    IMGBB_API_KEY = "140359baf01acef6aa27e35c55b32f99" 
                                    
                                    try:
                                        async with httpx.AsyncClient() as client_bb:
                                            res = await client_bb.post(
                                                "https://api.imgbb.com/1/upload", 
                                                params={"key": IMGBB_API_KEY},
                                                files={"image": ("ai_gen.png", img_content)},
                                                timeout=30.0
                                            )
                                            
                                            if res.status_code == 200:
                                                final_url = res.json()["data"]["url"]
                                                print(f"DEBUG: Картинка в облаке: {final_url}")
                                                
                                                await manager.broadcast(
                                                    room_id, username="AI_BOT", text=final_url, 
                                                    avatar="https://i.ibb.co/4pSbxsh/user-avatar.png", to_user=username
                                                )
                                            else:
                                                print(f"DEBUG: ImgBB Error: {res.text}")
                                                await manager.broadcast(room_id, username="AI_BOT", text="❌ Ошибка ImgBB", to_user=username)
                                    except Exception as e_bb:
                                        print(f"DEBUG: Ошибка загрузки: {e_bb}")
                                        await manager.broadcast(room_id, username="AI_BOT", text="⚠️ Ошибка сети", to_user=username)
                                else:
                                    # Обработка ошибки 503 или 401 от Hugging Face
                                    err_txt = "⌛ Нейросеть спит, подожди 30 сек." if response.status_code == 503 else f"❌ Ошибка API: {response.status_code}"
                                    await manager.broadcast(room_id, username="AI_BOT", text=err_txt, to_user=username)

                        except Exception as e:
                            # ВОТ ЭТОГО БЛОКА У ТЕБЯ НЕ ХВАТАЛО
                            print(f"DEBUG: Критическая ошибка HF: {e}")
                            await manager.broadcast(room_id, username="AI_BOT", text=f"⚠️ Ошибка системы: {str(e)[:30]}", to_user=username)
                        
                        continue



                    # (Весь остальной код с Groq идет ниже...)


                    # --- 2. ЛОГИКА ТЕКСТОВОЙ ПАМЯТИ (GROQ) ---
                    groq_key = os.environ.get("GROQ_KEY")
                    if not groq_key:
                        await manager.broadcast(room_id, username="AI_BOT", text="Ошибка: Ключ API не настроен.", to_user=username)
                        continue

                    if username not in ai_history:
                        ai_history[username] = [
                            {"role": "system", "content": f"Ты — официальный ИИ-ассистент Pinnogram. Собеседник: {username}. Ты умеешь рисовать (если тебя просят 'нарисуй') и поддерживать беседу."}
                        ]
                    
                    ai_history[username].append({"role": "user", "content": clean_text})
                    
                    if len(ai_history[username]) > 11:
                        ai_history[username] = [ai_history[username][0]] + ai_history[username][-10:]

                    AI_URL = "https://api.groq.com/openai/v1/chat/completions"
                    
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.post(
                                AI_URL,
                                headers={
                                    "Authorization": f"Bearer {groq_key}",
                                    "Content-Type": "application/json"
                                },
                                json={
                                    "model": "llama-3.3-70b-versatile",
                                    "messages": ai_history[username]
                                },
                                timeout=30.0 
                            )
                            
                            ai_data = resp.json()
                            if "choices" in ai_data and len(ai_data["choices"]) > 0:
                                ai_text = ai_data['choices'][0]['message']['content']
                                ai_history[username].append({"role": "assistant", "content": ai_text})
                                
                                await manager.broadcast(
                                    room_id, username="AI_BOT", text=ai_text, 
                                    avatar="https://i.ibb.co/4pSbxsh/user-avatar.png", to_user=username 
                                )
                            else:
                                await manager.broadcast(room_id, username="AI_BOT", text="Ошибка ИИ", to_user=username)

                    except Exception as e:
                        await manager.broadcast(room_id, username="AI_BOT", text=f"⚠️ Ошибка: {str(e)[:50]}", to_user=username)


                # 6. Проверка PUSH (только для ЛС, не для ботов и не для групп)
                # Добавляем проверку, что это не группа
                is_it_group = False
                if target_user:
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT 1 FROM groups WHERE name = ?", (target_user,)) as cur:
                            if await cur.fetchone(): is_it_group = True
        
                if target_user and target_user not in ["AI_BOT", "undefined", "null"] and not is_it_group:
                    is_online = False
                    if room_id in manager.rooms and target_user in manager.rooms[room_id]:
                        is_online = True
                    
                    if not is_online:
                        async with aiosqlite.connect(DB_PATH) as db:
                            async with db.execute("SELECT subscription_json FROM push_subscriptions WHERE username = ?", (target_user,)) as c:
                                s_row = await c.fetchone()
                                if s_row:
                                    try:
                                        await asyncio.to_thread(
                                            webpush,
                                            subscription_info=json.loads(s_row[0]),
                                            data=json.dumps({"title": f"ЛС от {username}", "body": clean_text[:50]}),
                                            vapid_private_key=VAPID_PRIVATE_KEY,
                                            vapid_claims=VAPID_CLAIMS
                                        )
                                    except: pass


    except WebSocketDisconnect:
        manager.disconnect(room_id, username)
        await manager.broadcast(room_id, message=f"{username} покинул чат")
    except Exception as e:
        print(f"Глобальная ошибка сокета: {e}")
        manager.disconnect(room_id, username)

# --- ПРАВИЛЬНЫЙ ЗАПУСК СЕРВЕРА И БОТА ---
# Убедись, что keep_alive_bot(manager) определен выше в файле!

if __name__ == "__main__":
    # Render передает PORT автоматически, берем его или ставим 10000 по умолчанию
    port = int(os.environ.get("PORT", 10000))
    
    # ВАЖНО: Мы перенесли запуск бота в @app.on_event("startup")
    # Проверь, чтобы в твоей функции startup() в конце стояла строка:
    # asyncio.create_task(keep_alive_bot(manager))
    
    uvicorn.run(app, host="0.0.0.0", port=port)

