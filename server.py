import os, uuid, aiosqlite, uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import FileResponse 
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
import aiofiles

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Умные пути для сервера и локального ПК
if os.path.exists("/data"):
    DB_PATH = "/data/pinnogram.db"
    UPLOAD_DIR = "/data/uploads"
else:
    DB_PATH = "pinnogram.db"
    UPLOAD_DIR = "uploads"

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

@app.api_route("/", methods=["GET", "HEAD"])
async def get_index():
    return FileResponse('index.html')

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

@app.on_event("startup")
async def startup():
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN room_id TEXT DEFAULT 'general'")
            await db.commit()
        except: pass 
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT, text TEXT, timestamp TEXT, room_id TEXT DEFAULT 'general'
            )
        """)
        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.rooms = {} # Структура: {room_id: {username: websocket}}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        # Сохраняем сокет конкретного пользователя
        self.rooms[room_id][username] = websocket
        
        # 1. Отправляем историю сообщений (как и было)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, username, text, timestamp FROM messages WHERE room_id = ? ORDER BY id ASC LIMIT 50", 
                (room_id,)
            ) as cursor:
                history = await cursor.fetchall()
                for msg_id, user, text, time in history:
                    await websocket.send_text(f"ID:{msg_id}|[{time}] {user}: {text}")
        
        # 2. Рассылаем всем новый список онлайн
        await self.broadcast_online(room_id)

    def disconnect(self, room_id: str, username: str):
        if room_id in self.rooms and username in self.rooms[room_id]:
            del self.rooms[room_id][username]
        # Запускаем обновление списка онлайн для оставшихся
        import asyncio
        asyncio.create_task(self.broadcast_online(room_id))

    async def broadcast_online(self, room_id: str):
        if room_id in self.rooms:
            users = list(self.rooms[room_id].keys())
            # Тот самый формат ONLINE_LIST для твоего JS
            msg = f"ID:0|SYSTEM:ONLINE_LIST:{','.join(users)}"
            for ws in self.rooms[room_id].values():
                try: await ws.send_text(msg)
                except: continue

    async def broadcast(self, message: str, room_id: str, username: str = None, text: str = None):
        now = datetime.now().strftime("%H:%M")
        final_msg = f"ID:0|SYSTEM: {message}"
        
        if username and text:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "INSERT INTO messages (username, text, timestamp, room_id) VALUES (?, ?, ?, ?)", 
                    (username, text, now, room_id)
                )
                msg_id = cursor.lastrowid
                await db.commit()
                final_msg = f"ID:{msg_id}|[{now}] {username}: {text}"

        if room_id in self.rooms:
            for ws in self.rooms[room_id].values():
                if ws.client_state == WebSocketState.CONNECTED:
                    try: await ws.send_text(final_msg)
                    except: continue

manager = ConnectionManager()

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1]
    name = f"{uuid.uuid4()}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    async with aiofiles.open(path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            await buffer.write(content)
    return {"url": f"{str(request.base_url).rstrip('/')}/files/{name}"}

@app.websocket("/ws/{room_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str):
    # Теперь передаем username при коннекте
    await manager.connect(websocket, room_id, username)
    await manager.broadcast(f"{username} вошел в чат", room_id)
    try:
        while True:
            data = await websocket.receive_text()
            
            if data.startswith("RTC_SIGNAL:"):
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        # Не шлем сигнал самому себе
                        if name != username and conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(data)
                continue
            
            if data.startswith("__DELETE__:"):
                msg_id = data.replace("__DELETE__:", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
                    await db.commit()
                if room_id in manager.rooms:
                    for conn in manager.rooms[room_id].values():
                        await conn.send_text(f"DELETE_CONFIRM:{msg_id}")
            elif data == "__TYPING__":
                if room_id in manager.rooms:
                    for name, conn in manager.rooms[room_id].items():
                        if name != username:
                            await conn.send_text(f"TYPING:{username}")
            else:
                await manager.broadcast("", room_id, username=username, text=data)
    except WebSocketDisconnect:
        # Передаем параметры для корректного удаления из словаря
        manager.disconnect(room_id, username)
        await manager.broadcast(f"{username} покинул чат", room_id)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

