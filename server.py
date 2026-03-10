import os, uuid, aiosqlite, uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import FileResponse 
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
import aiofiles
import httpx

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

# Строим абсолютные пути (это надежнее)
DB_PATH = os.path.join(BASE_DIR, "pinnogram.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

# Создаем папку, если её нет
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.api_route("/", methods=["GET", "HEAD"])
async def get_index():
    return FileResponse('index.html')

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

@app.on_event("startup")
@app.on_event("startup")
async def startup():
    async with aiosqlite.connect(DB_PATH) as db:
        # Добавляем сразу обе колонки на случай обновления старой базы
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN room_id TEXT DEFAULT 'general'")
            await db.execute("ALTER TABLE messages ADD COLUMN avatar TEXT DEFAULT ''")
            await db.commit()
        except: pass 
        
        # В CREATE TABLE обязательно добавляем avatar
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, text TEXT, timestamp TEXT, 
                room_id TEXT DEFAULT 'general',
                avatar TEXT DEFAULT ''
            )
        """)
        # Таблица пользователей для аватарок и паролей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT,
                avatar TEXT DEFAULT ''
            )
        """)
        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.rooms = {}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][username] = websocket
        
        async with aiosqlite.connect(DB_PATH) as db:
            # В истории тоже подтягиваем аватарку
            async with db.execute(
                "SELECT id, username, text, timestamp, avatar FROM messages WHERE room_id = ? ORDER BY id ASC LIMIT 50", 
                (room_id,)
            ) as cursor:
                history = await cursor.fetchall()
                for msg_id, user, text, time, av in history:
                    await websocket.send_text(f"ID:{msg_id}|[{time}] {user}: {text}|{av or ''}")
        await self.broadcast_online(room_id)

    def disconnect(self, room_id: str, username: str):
        if room_id in self.rooms and username in self.rooms[room_id]:
            del self.rooms[room_id][username]
        import asyncio
        asyncio.create_task(self.broadcast_online(room_id))

    async def broadcast_online(self, room_id: str):
        if room_id in self.rooms:
            users = list(self.rooms[room_id].keys())
            msg = f"ID:0|SYSTEM:ONLINE_LIST:{','.join(users)}"
            for ws in self.rooms[room_id].values():
                try: await ws.send_text(msg)
                except: continue

    # ИСПРАВЛЕН ОТСТУП (ровно под async def выше)
    async def broadcast(self, room_id: str, message: str = "", username: str = None, text: str = None, avatar: str = "", client_time: str = None):
        now = client_time if client_time else datetime.now().strftime("%H:%M")
        final_msg = f"ID:0|SYSTEM: {message}"
        
        if username and text:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO messages (username, text, timestamp, room_id, avatar) VALUES (?, ?, ?, ?, ?)", 
                    (username, text, now, room_id, avatar)
                )
                cursor = await db.execute("SELECT last_insert_rowid()")
                msg_id = (await cursor.fetchone())[0]
                await db.commit()
                final_msg = f"ID:{msg_id}|[{now}] {username}: {text}|{avatar}"
    
        if room_id in self.rooms:
            for ws in self.rooms[room_id].values():
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
    # 1. Сначала узнаем аватарку пользователя из БД
    current_avatar = ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT avatar FROM users WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            if row: current_avatar = row[0]

    await manager.connect(websocket, room_id, username)
    # ПРАВИЛЬНЫЙ ПОРЯДОК: room_id ПЕРВЫЙ
    await manager.broadcast(room_id, message=f"{username} вошел в чат")

    try:
        while True:
            data = await websocket.receive_text()
            
            # RTC сигналы (оставляем как есть)
            if data.startswith("RTC_SIGNAL:"):
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username and conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(data)
                continue

            # Удаление
            if data.startswith("__DELETE__:"):
                msg_id = data.replace("__DELETE__:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
                    await db.commit()
                if room_id in manager.rooms:
                    for conn in manager.rooms[room_id].values():
                        await conn.send_text(f"DELETE_CONFIRM:{msg_id}")
            
            # Печать
            elif data == "__TYPING__":
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username:
                            await conn.send_text(f"TYPING:{username}")
            
            # ОБЫЧНОЕ СООБЩЕНИЕ (с обработкой времени)
            else:
                display_text = data
                msg_time = datetime.now().strftime("%H:%M") # Заглушка
                
                # Распаковка времени из JS: "TIME:14:30|Привет"
                if data.startswith("TIME:"):
                    try:
                        parts = data.split("|", 1)
                        msg_time = parts[0].replace("TIME:", "")
                        display_text = parts[1]
                    except: pass

                # Шлем всем в чат (room_id — первый!)
                await manager.broadcast(
                    room_id, 
                    username=username, 
                    text=display_text, 
                    avatar=current_avatar, 
                    client_time=msg_time
                )
                
    except WebSocketDisconnect:
        manager.disconnect(room_id, username)
        await manager.broadcast(room_id, message=f"{username} покинул чат")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)




