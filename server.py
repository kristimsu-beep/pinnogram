import os, shutil, uuid, aiosqlite, uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import FileResponse 
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

app = FastAPI()

# 1. CORS ДОЛЖЕН БЫТЬ В САМОМ НАЧАЛЕ (сразу после app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
DB_PATH = "pinnogram.db"

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# 2. Роуты
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
        except:
            pass 

        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                text TEXT,
                timestamp TEXT,
                room_id TEXT DEFAULT 'general'
            )
        """)
        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.rooms: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept() # Важнейшая строка
        if room_id not in self.rooms:
            self.rooms[room_id] = []
        self.rooms[room_id].append(websocket)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, username, text, timestamp FROM messages WHERE room_id = ? ORDER BY id ASC LIMIT 50", 
                (room_id,)
            ) as cursor:
                history = await cursor.fetchall()
                for msg_id, user, text, time in history:
                    await websocket.send_text(f"ID:{msg_id}|[{time}] {user}: {text}")

    def disconnect(self, websocket: WebSocket, room_id: str):
        if room_id in self.rooms:
            try:
                self.rooms[room_id].remove(websocket)
            except ValueError:
                pass
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast(self, message: str, room_id: str, username: str = None, text: str = None):
        now = datetime.now().strftime("%H:%M")
        
        if username and text:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "INSERT INTO messages (username, text, timestamp, room_id) VALUES (?, ?, ?, ?)", 
                    (username, text, now, room_id)
                )
                msg_id = cursor.lastrowid
                await db.execute("""
                    DELETE FROM messages WHERE room_id = ? AND id NOT IN 
                    (SELECT id FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT 100)
                """, (room_id, room_id))
                await db.commit()
                final_msg = f"ID:{msg_id}|[{now}] {username}: {text}"
        else:
            final_msg = f"SYSTEM: {message}"

        if room_id in self.rooms:
            for connection in self.rooms[room_id]:
                if connection.client_state == WebSocketState.CONNECTED:
                    try:
                        await connection.send_text(final_msg)
                    except:
                        continue

manager = ConnectionManager()

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1]
    name = f"{uuid.uuid4()}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/files/{name}"}

@app.websocket("/ws/{room_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str):
    await manager.connect(websocket, room_id)
    await manager.broadcast(f"{username} вошел в чат", room_id)
    
    try:
        while True:
            data = await websocket.receive_text()
            
            if data.startswith("__DELETE__:"):
                msg_id = data.replace("__DELETE__:", "") # Более надежное получение ID
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
                    await db.commit()
                
                if room_id in manager.rooms:
                    for connection in manager.rooms[room_id]:
                        if connection.client_state == WebSocketState.CONNECTED:
                            await connection.send_text(f"DELETE_CONFIRM:{msg_id}")
            
            elif data == "__TYPING__":
                if room_id in manager.rooms:
                    for connection in manager.rooms[room_id]:
                        if connection != websocket and connection.client_state == WebSocketState.CONNECTED:
                            await connection.send_text(f"TYPING:{username}")
            else:
                await manager.broadcast("", room_id, username=username, text=data)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
        await manager.broadcast(f"{username} покинул чат", room_id)

if __name__ == "__main__":
    # На Render порт берется из переменной среды
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

