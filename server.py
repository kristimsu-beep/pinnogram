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
        # 1. Создаем основные таблицы
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, text TEXT, timestamp TEXT, 
                room_id TEXT DEFAULT 'general',
                to_user TEXT DEFAULT NULL, 
                avatar TEXT DEFAULT '',
                is_read INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, password TEXT, avatar TEXT DEFAULT ''
            )
        """)
        
        # НОВАЯ ТАБЛИЦА: Для хранения подписок на Push-уведомления
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                username TEXT PRIMARY KEY, 
                subscription_json TEXT
            )
        """)

        # 2. ФИКСЫ ДЛЯ СТАРОЙ БАЗЫ (Добавляем то, чего может не хватать)
        columns = [
            ("messages", "avatar", "TEXT DEFAULT ''"),
            ("messages", "to_user", "TEXT DEFAULT NULL"),
            ("messages", "is_read", "INTEGER DEFAULT 0") # Колонки для галочек
        ]
        
        for table, col, definition in columns:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except:
                pass # Если колонка уже есть, SQLite выдаст ошибку, и мы её пропустим
        
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
        import asyncio
        asyncio.create_task(self.broadcast_online(room_id))

    async def broadcast_online(self, room_id: str):
        if room_id in self.rooms:
            users_with_ips = []
            for name, ws in self.rooms[room_id].items():
                ip = ws.client.host if ws.client else "unknown"
                users_with_ips.append(f"{name} ({ip})")
            
            msg = f"ID:0|SYSTEM:ONLINE_LIST:{','.join(users_with_ips)}"
            for ws in self.rooms[room_id].values():
                if ws.client_state == WebSocketState.CONNECTED:
                    try: await ws.send_text(msg)
                    except: continue

    # ИСПРАВЛЕНО: Ровный отступ и безопасное получение ID
    async def broadcast(self, room_id: str, message: str = "", username: str = None, text: str = None, avatar: str = "", client_time: str = None, to_user: str = None):        
        now = client_time if client_time else datetime.now().strftime("%H:%M")        
        
        if username and text:        
            # 1. Открываем БД ОДИН раз (экономим ресурсы)
            async with aiosqlite.connect(DB_PATH) as db:        
                cursor = await db.execute(        
                    "INSERT INTO messages (username, text, timestamp, room_id, avatar, to_user, is_read) VALUES (?, ?, ?, ?, ?, ?, 0)",         
                    (username, text, now, room_id, avatar, to_user)        
                )        
                msg_id = cursor.lastrowid # Так быстрее получать ID
                await db.commit()        
    
                prefix = f"PRIVATE:{to_user}:" if to_user else ""        
                final_msg = f"ID:{msg_id}|{prefix}[{now}] {username}: {text}|{avatar}|0"        
                    
                if to_user:        
                    is_online = False        
                    room_users = self.rooms.get(room_id, {})
                    
                    for name in [username, to_user]:        
                        if name in room_users:        
                            if name == to_user: is_online = True        
                            try: 
                                await room_users[name].send_text(final_msg)        
                            except: 
                                continue        
                        
                    # 2. Исправленный PUSH (не блокирует сервер)
                    if not is_online:        
                        async with db.execute("SELECT subscription_json FROM push_subscriptions WHERE username = ?", (to_user,)) as c:        
                            sub_row = await c.fetchone()        
                            if sub_row:        
                                try:
                                    # Запускаем в отдельном потоке, чтобы чат не завис на 1 секунду
                                    await asyncio.to_thread(
                                        webpush,
                                        subscription_info=json.loads(sub_row[0]),        
                                        data=json.dumps({"title": f"ЛС от {username}", "body": text[:50]}),        
                                        vapid_private_key=VAPID_PRIVATE_KEY,        
                                        vapid_claims=VAPID_CLAIMS        
                                    )        
                                except Exception as e: 
                                    print(f"Push Error: {e}")        
                else:        
                    # Обычный бродкаст
                    for ws in self.rooms.get(room_id, {}).values():        
                        if ws.client_state == WebSocketState.CONNECTED:        
                            try: await ws.send_text(final_msg)        
                            except: continue        
        else:        
            # Системные сообщения        
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
            data = await websocket.receive_text()
            
            # --- БЛОК 1: ЗАПРОС ИСТОРИИ ---
            # --- БЛОК 1: ЗАПРОС ИСТОРИИ (С ГАЛОЧКАМИ) ---
            if data.startswith("GET_HISTORY:"):
                target = data.replace("GET_HISTORY:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    if target in ["null", "general", "None", "undefined"]:
                        # Добавили is_read в SELECT
                        sql = "SELECT id, username, text, timestamp, avatar, to_user, is_read FROM messages WHERE room_id = ? AND to_user IS NULL ORDER BY id ASC LIMIT 100"
                        params = (room_id,)
                    else:
                        # Добавили is_read в SELECT для ЛС
                        sql = """
                            SELECT id, username, text, timestamp, avatar, to_user, is_read 
                            FROM messages 
                            WHERE room_id = ? 
                            AND ((username = ? AND to_user = ?) OR (username = ? AND to_user = ?)) 
                            ORDER BY id ASC LIMIT 100
                        """
                        params = (room_id, username, target, target, username)
                    
                    async with db.execute(sql, params) as cursor:
                        history = await cursor.fetchall()
                        for msg_id, user, text, time, av, to_u, is_read in history:
                            prefix = f"PRIVATE:{to_u}:" if to_u else ""
                            # Теперь отправляем статус прочтения в самом конце через новый разделитель |
                            await websocket.send_text(f"ID:{msg_id}|{prefix}[{time}] {user}: {text}|{av or ''}|{is_read}")
                continue


            # --- БЛОК 2: RTC СИГНАЛЫ (ЗВОНКИ) ---
            elif data.startswith("RTC_SIGNAL:"):
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username and conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(data)
                continue

            # --- БЛОК 3: УДАЛЕНИЕ ---
            elif data.startswith("__DELETE__:"):
                msg_id = data.replace("__DELETE__:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
                    await db.commit()
                if room_id in manager.rooms:
                    for conn in manager.rooms[room_id].values():
                        await conn.send_text(f"DELETE_CONFIRM:{msg_id}")
                continue

            # --- БЛОК 4: ПЕЧАТАЕТ... ---
            elif data == "__TYPING__":
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username:
                            await conn.send_text(f"TYPING:{username}")
                continue

            # --- БЛОК 5: ОТПРАВКА СООБЩЕНИЯ (ОБЩЕЕ ИЛИ ЛС) ---
                       # --- БЛОК 5: ОТПРАВКА СООБЩЕНИЯ ---
            # --- БЛОК 5: ИСПРАВЛЕННЫЙ РАЗБОР СООБЩЕНИЯ ---
            else:
                display_text = data
                msg_time = datetime.now().strftime("%H:%M")
                target_user = None

                # 1. Сначала отрезаем ЛС (TO_USER:имя|...)
                if display_text.startswith("TO_USER:"):
                    try:
                        parts = display_text.split("|", 1)
                        target_user = parts[0].replace("TO_USER:", "")
                        display_text = parts[1]
                    except: pass

                # 2. Теперь отрезаем время (TIME:17:32|...), чтобы оно не лезло в текст сообщения
                if display_text.startswith("TIME:"):
                    try:
                        parts = display_text.split("|", 1)
                        msg_time = parts[0].replace("TIME:", "")
                        display_text = parts[1] # ВОТ ТУТ мы берем только текст сообщения
                    except: pass

                # 3. Обновляем аватарку отправителя перед рассылкой
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT avatar FROM users WHERE username = ?", (username,)) as c:
                        row = await c.fetchone()
                        if row: current_avatar = row[0]

                # 4. Проверяем оффлайн для PUSH
                is_online = False
                if target_user and room_id in manager.rooms:
                    if target_user in manager.rooms[room_id]:
                        is_online = True

                if target_user and not is_online:
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT subscription_json FROM push_subscriptions WHERE username = ?", (target_user,)) as c:
                            sub_row = await c.fetchone()
                            if sub_row:
                                try:
                                    import asyncio
                                    # Отправляем в потоке, чтобы чат не лагал
                                    await asyncio.to_thread(
                                        webpush,
                                        subscription_info=json.loads(sub_row[0]),
                                        data=json.dumps({"title": f"ЛС от {username}", "body": display_text[:50]}),
                                        vapid_private_key=VAPID_PRIVATE_KEY,
                                        vapid_claims=VAPID_CLAIMS
                                    )
                                except: pass

                # 5. Отправляем в broadcast ЧИСТЫЙ текст и ЧИСТОЕ время
                await manager.broadcast(
                    room_id, 
                    username=username, 
                    text=display_text, 
                    avatar=current_avatar, 
                    client_time=msg_time,
                    to_user=target_user
                )

    except WebSocketDisconnect:
        manager.disconnect(room_id, username)
        await manager.broadcast(room_id, message=f"{username} покинул чат")

# ЗАПУСК
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)










