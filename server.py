import asyncio 
import os, uuid, aiosqlite, uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
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

# Берем ключ из настроек Render (в коде его не будет видно)
ADMIN_SECRET_KEY = os.environ.get("ADMIN_KEY", "admin123")
BANNED_DATA = {} # { "IP": timestamp_unban }


app = FastAPI()
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

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

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
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Тянем ВСЕХ из таблицы users и сопоставляем с таблицей bans
        sql = """
            SELECT u.username, u.avatar, b.unban_time, b.ip 
            FROM users u 
            LEFT JOIN bans b ON u.username = b.username
        """
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
            # Формируем список: если unban_time есть и оно больше текущего — юзер забанен
            return [
                {
                    "name": r[0], 
                    "avatar": r[1], 
                    "banned": r[2] is not None and r[2] > time.time(), 
                    "ip": r[3]
                } for r in rows
            ]


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
    # Фильтр для локальных и тестовых адресов
    if not ip or ip in ["127.0.0.1", "localhost", "::1", "testclient"]: 
        return {"city": "Local", "org": "Internal", "country": "un", "tz": "UTC", "asn": "LAN"}
    
    try:
        async with httpx.AsyncClient() as client:
            # ИСПРАВЛЕНО: Добавлен / после .co
            res = await client.get(f"https://ipapi.co/{ip}/json/", timeout=2.0)
            if res.status_code == 200:
                data = res.json()
                info = {
                    "city": data.get("city", "Unknown"),
                    "org": data.get("org", "Unknown ISP"),
                    "country": data.get("country_code", "un").lower(),
                    "tz": data.get("timezone", "UTC/Unknown"),
                    "asn": data.get("asn", "Unknown") 
                }
                if info["country"] == "su": info["country"] = "ru"
                admin_data_cache[ip] = info
                return info
    except Exception as e: 
        print(f"Geo Error: {e}")
    return {"city": "Unknown", "org": "Unknown", "country": "un", "tz": "UTC", "asn": "Unknown"}

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
        unban_time = time.time() + (int(days) * 86400)
        
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
            # Чистим всё: по имени или по IP (это удалит и привязанный fingerprint)
            await db.execute("DELETE FROM bans WHERE username = ? OR ip = ?", (target, target))
            await db.commit()
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
            users_info = []
            for name, ws in self.rooms[room_id].items():
                ip = ws.client.host if ws.client else "unknown"
                info = await get_user_info(ip) 
                users_info.append(f"{name}|{ip}|{info['country']}|{info['city']}|{info['org']}|{info['tz']}|{info['asn']}")
            
            msg = f"ID:0|SYSTEM:ONLINE_LIST:{','.join(users_info)}"
            for ws in self.rooms[room_id].values():
                if ws.client_state == WebSocketState.CONNECTED:
                    try: await ws.send_text(msg)
                    except: continue

    async def broadcast(self, room_id: str, message: str = "", username: str = None, text: str = None, avatar: str = "", client_time: str = None, to_user: str = None, reply_to_id: int = None):
        now = client_time if client_time else datetime.now().strftime("%H:%M")        
        
        if username and text:        
            async with aiosqlite.connect(DB_PATH) as db:        
                cursor = await db.execute(        
                    "INSERT INTO messages (username, text, timestamp, room_id, avatar, to_user, is_read, reply_to_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",         
                    (username, text, now, room_id, avatar, to_user, reply_to_id)        
                )        
                msg_id = cursor.lastrowid
                await db.commit()        
    
                prefix = f"PRIVATE:{to_user}:" if to_user else ""
                reply_info = f"REPLY:{reply_to_id}|" if reply_to_id else ""
                final_msg = f"ID:{msg_id}|{reply_info}{prefix}[{now}] {username}: {text}|{avatar}|0"
      
                if to_user:        
                    room_users = self.rooms.get(room_id, {})
                    for name in [username, to_user]:        
                        if name in room_users:        
                            try: 
                                await room_users[name].send_text(final_msg)        
                            except: 
                                continue        
                        
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
                    for ws in self.rooms.get(room_id, {}).values():        
                        if ws.client_state == WebSocketState.CONNECTED:        
                            try: 
                                await ws.send_text(final_msg)        
                            except: 
                                continue        
        else:        
            final_msg = f"ID:0|SYSTEM: {message}"        
            for ws in self.rooms.get(room_id, {}).values():        
                if ws.client_state == WebSocketState.CONNECTED:        
                    try: 
                        await ws.send_text(final_msg)        
                    except: 
                        continue

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
                fingerprint TEXT,  -- НОВОЕ ПОЛЕ
                unban_time REAL,
                admin_name TEXT,
                reason TEXT
            )
        """)



        
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
        print("🚀 Pinnogram Engine: База готова, бот-будильник запущен!")


# Вставь свой ключ тут
IMGBB_API_KEY = "140359baf01acef6aa27e35c55b32f99"

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    try:
        # Читаем содержимое файла в память
        content = await file.read()
        
        # 1. Если это ИЗОБРАЖЕНИЕ — отправляем на ImgBB (навечно)
        if file.content_type and file.content_type.startswith("image/"):
            url = f"https://api.imgbb.com/1/upload?key={IMGBB_API_KEY}"
            async with httpx.AsyncClient() as client:
                files = {"image": (file.filename, content)}
                res = await client.post(url, files=files, timeout=30.0)
                
                if res.status_code == 200:
                    # Возвращаем прямую ссылку из облака ImgBB
                    return {"url": res.json()["data"]["url"]}
                else:
                    print(f"ImgBB Error: {res.text}")
                    # Если облако выдало ошибку, пробуем сохранить локально как запасной вариант

        # 2. Если это ВИДЕО (кружок) или ошибка облака — сохраняем локально на Render
        # (Эти файлы удалятся через 15 минут простоя, но они будут работать в моменте)
        ext = os.path.splitext(file.filename)[1]
        name = f"{uuid.uuid4()}{ext}"
        path = os.path.join(UPLOAD_DIR, name)
        
        async with aiofiles.open(path, "wb") as buffer:
            await buffer.write(content)
            
        # Формируем ссылку на локальный файл
        return {"url": f"{str(request.base_url).rstrip('/')}/files/{name}"}

    except Exception as e:
        print(f"Upload Exception: {e}")
        return {"url": "error"}



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

        # 3. ЖЕСТКАЯ ПРОВЕРКА ПО ВСЕМ ФРОНТАМ (Имя, IP или Браузер)
@app.websocket("/ws/{room_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str):
    client_ip = websocket.client.host if websocket.client else "unknown"
    
    await websocket.accept()

    try:
        # Ждем паспорт браузера
        auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
        browser_finger = auth_msg.replace("FINGERPRINT:", "") if "FINGERPRINT:" in auth_msg else "unknown"

        async with aiosqlite.connect(DB_PATH) as db:
            # --- ИСПРАВЛЕНО: УБРАЛИ IP ИЗ ПРОВЕРКИ ---
            sql = "SELECT unban_time, admin_name, reason FROM bans WHERE username = ? OR fingerprint = ?"
            async with db.execute(sql, (username, browser_finger)) as cur:
                ban_row = await cur.fetchone()
                
                if ban_row:
                    unban_time, admin, reason = ban_row
                    if time.time() < unban_time:
                        await websocket.send_text(f"ID:0|SYSTEM:BAN_SCREEN|{unban_time}|{admin}|{reason}")
                        await asyncio.sleep(0.5)
                        await websocket.close(code=1008)
                        return
                    else:
                        # Чистим базу только по нику и железу
                        await db.execute("DELETE FROM bans WHERE username = ? OR fingerprint = ?", 
                                        (username, browser_finger))
                        await db.commit()
    except Exception as e:
        print(f"Auth Error: {e}")
        await websocket.close()
        return
 
    
    # ... дальше твой код с аватаркой ...


    # ... дальше идет твой код (current_avatar и т.д.) ...

    websocket.browser_fingerprint = browser_finger  # СОХРАНЯЕМ ОТПЕЧАТОК В СОКЕТЕ

    # 1. Узнаем аватарку при входе
    current_avatar = ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT avatar FROM users WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            if row: current_avatar = row[0]

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

            # --- ТЕПЕРЬ ПРОВЕРЯЕМ КОМАНДЫ ПО ЧИСТОМУ ТЕКСТУ (clean_text) ---
            

            
            # 1. ЗАПРОС ИСТОРИИ (ИСПРАВЛЕНО)
            # 1. ЗАПРОС ИСТОРИИ (С ПОДДЕРЖКОЙ ОТВЕТОВ И РЕАКЦИЙ)
            if clean_text.startswith("GET_HISTORY:"):
                target = clean_text.replace("GET_HISTORY:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    # Определяем, какую историю тянуть (общую или личку)
                    if target in ["null", "general", "None", "undefined"]:
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND to_user IS NULL ORDER BY id ASC LIMIT 100"
                        params = (room_id,)
                    else:
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND ((username = ? AND to_user = ?) OR (username = ? AND to_user = ?)) ORDER BY id ASC LIMIT 100"
                        params = (room_id, username, target, target, username)
                    
                    async with db.execute(sql, params) as cursor:
                        history = await cursor.fetchall()
                        
                        for m_id, u, txt, tm, av, to_u, is_r, r_id in history:
                            # --- ШАГ А: ПОДТЯГИВАЕМ РЕАКЦИИ ДЛЯ ЭТОГО СООБЩЕНИЯ ---
                            react_pfx = ""
                            async with db.execute("SELECT emoji, COUNT(*) FROM reactions WHERE msg_id = ? GROUP BY emoji", (m_id,)) as r_cur:
                                r_rows = await r_cur.fetchall()
                                if r_rows:
                                    # Склеиваем в формат: 👍:2,🔥:1
                                    r_data = ",".join([f"{row[0]}:{row[1]}" for row in r_rows])
                                    react_pfx = f"REACTION:{r_data}|"

                            # --- ШАГ Б: ФОРМИРУЕМ ТЕХНИЧЕСКИЕ ПРЕФИКСЫ ---
                            pfx = f"PRIVATE:{to_u}:" if to_u else ""
                            reply_pfx = f"REPLY:{r_id}|" if r_id else ""
                            
                            # --- ШАГ В: ОТПРАВКА ПОЛНОГО ПАКЕТА ---
                            # Формат: ID | РЕАКЦИИ | ОТВЕТ | ПРИВАТ | [ВРЕМЯ] ИМЯ: ТЕКСТ | АВАТАР | ПРОЧИТАНО
                            full_packet = f"ID:{m_id}|{react_pfx}{reply_pfx}{pfx}[{tm}] {u}: {txt}|{av or ''}|{is_r}"
                            await websocket.send_text(full_packet)
                continue


                

            # 2. RTC СИГНАЛЫ
            elif clean_text.startswith("RTC_SIGNAL:"):
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username and conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(clean_text)
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





                # 6. Проверка PUSH (если это не бот, а обычный юзер оффлайн)
                elif target_user and target_user != "AI_BOT":
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

























