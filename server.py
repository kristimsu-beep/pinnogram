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

app = FastAPI()

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


# 1. Роут для регистрации и входа
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
async def get_country_code(ip):
    # 1. Проверка кэша (чтобы не тратить лимиты API)
    if ip in country_cache: return country_cache[ip]
    
    # 2. Игнорируем локальные и пустые IP
    if not ip or ip in ["127.0.0.1", "localhost", "unknown", "::1", "testclient"]: 
        return "un"
    
    try:
        async with httpx.AsyncClient() as client:
            # Делаем запрос к API
            res = await client.get(f"https://ipapi.co/{ip}/json/", timeout=2.0)
            if res.status_code == 200:
                data = res.json()
                # Если IP не найден в базе API, оно может вернуть поле error
                if data.get("error"): return "un"
                
                code = data.get("country_code", "un").lower()

                # --- КРИТИЧЕСКИЙ ФИКС ДЛЯ РОССИИ ---
                # Если провайдер отдает старый код SU (Soviet Union), меняем на RU
                if code == "su": code = "ru"
                
                country_cache[ip] = code
                return code
    except Exception as e:
        print(f"GeoIP Error: {e}") # Для отладки в логах Render
    
    return "un"


class ConnectionManager:
    def __init__(self):
        self.rooms = {}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][username] = websocket
        await self.broadcast_online(room_id)

    def disconnect(self, room_id: str, username: str):
        if room_id in self.rooms and username in self.rooms[room_id]:
            del self.rooms[room_id][username]
        # Создаем задачу на обновление списка онлайн
        asyncio.create_task(self.broadcast_online(room_id))

    async def broadcast_online(self, room_id: str):
        if room_id in self.rooms:
            users_info = []
            for name, ws in self.rooms[room_id].items():
                ip = ws.client.host if ws.client else "unknown"
                # Вызываем функцию получения флага
                code = await get_country_code(ip) 
                users_info.append(f"{name}|{ip}|{code}")
            
            # ВАЖНО: используем запятую для объединения списка
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
                    is_online = False        
                    room_users = self.rooms.get(room_id, {})
                    
                    for name in [username, to_user]:        
                        if name in room_users:        
                            if name == to_user: is_online = True        
                            try: await room_users[name].send_text(final_msg)        
                            except: continue        
                        
                    if not is_online:        
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
                            try: await ws.send_text(final_msg)        
                            except: continue        
        else:        
            final_msg = f"ID:0|SYSTEM: {message}"        
            for ws in self.rooms.get(room_id, {}).values():        
                if ws.client_state == WebSocketState.CONNECTED:        
                    try: await ws.send_text(final_msg)        
                    except: continue

manager = ConnectionManager()

    

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
            if clean_text.startswith("GET_HISTORY:"):
                target = clean_text.replace("GET_HISTORY:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    if target in ["null", "general", "None", "undefined"]:
                        # ДОБАВИЛИ ЗАПЯТУЮ И reply_to_id
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND to_user IS NULL ORDER BY id ASC LIMIT 100"
                        params = (room_id,)
                    else:
                        # ДОБАВИЛИ ЗАПЯТУЮ И reply_to_id
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read, reply_to_id FROM messages WHERE room_id = ? AND ((username = ? AND to_user = ?) OR (username = ? AND to_user = ?)) ORDER BY id ASC LIMIT 100"
                        params = (room_id, username, target, target, username)
                    
                    async with db.execute(sql, params) as cursor:
                        history = await cursor.fetchall()
                        # ДОБАВИЛИ r_id В РАСПАКОВКУ
                        for m_id, u, txt, tm, av, to_u, is_r, r_id in history:
                            pfx = f"PRIVATE:{to_u}:" if to_u else ""
                            # Добавляем префикс ответа, если он есть, чтобы фронтенд нарисовал цитату
                            reply_pfx = f"REPLY:{r_id}|" if r_id else ""
                            await websocket.send_text(f"ID:{m_id}|{reply_pfx}{pfx}[{tm}] {u}: {txt}|{av or ''}|{is_r}")
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
                msg_id, new_text = clean_text.replace("__EDIT__:", "").split("|", 1)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET text = ? WHERE id = ?", (new_text, msg_id))
                    await db.commit()
                # Рассылаем сигнал обновления
                for conn in manager.rooms[room_id].values():
                    await conn.send_text(f"EDIT_CONFIRM:{msg_id}|{new_text}")
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
                # --- ЛОГИКА ИИ БОТА (GROQ - LLAMA 3) ---
                     # 5. ЛОГИКА ИИ БОТА (GROQ - LLAMA 3)
                        # 5. ЛОГИКА ИИ БОТА (GROQ - LLAMA 3)
                if target_user == "AI_BOT":
                    await websocket.send_text("TYPING:AI_BOT")
                    
                    groq_key = os.environ.get("GROQ_KEY")
                    
                    if not groq_key:
                        await manager.broadcast(room_id, username="AI_BOT", text="Ошибка: Ключ API не настроен в Render.", to_user=username)
                        continue

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
                                    "messages": [
                                        {"role": "system", "content": f"Ты — официальный ИИ-ассистент мессенджера Pinnogram. Твоего собеседника зовут {username}. Будь вежливым и помогай ему."},
                                        {"role": "user", "content": clean_text}
                                    ]
                                },
                                timeout=30.0
                            ) # <--- Скобка должна закрывать post
                            
                            ai_data = resp.json()

                            
                            if "choices" in ai_data and len(ai_data["choices"]) > 0:
                                ai_text = ai_data['choices'][0]['message']['content']
                                
                                await manager.broadcast(
                                    room_id, 
                                    username="AI_BOT", 
                                    text=ai_text, 
                                    avatar="https://i.ibb.co/4pSbxsh/user-avatar.png", 
                                    to_user=username 
                                )
                            else:
                                err = ai_data.get("error", {}).get("message", "Ошибка ИИ")
                                await manager.broadcast(room_id, username="AI_BOT", text=f"Groq Error: {err}", to_user=username)

                    except Exception as e:
                        # Теперь бот сам скажет, что именно сломалось!
                        error_details = str(e)
                        print(f"AI Global Error: {error_details}")
                        await manager.broadcast(room_id, username="AI_BOT", text=f"⚠️ Системная ошибка: {error_details}", to_user=username)


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

# START
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)


























