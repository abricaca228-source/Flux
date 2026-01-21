import json
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from database import AsyncSessionLocal, init_db

app = FastAPI()

templates = Jinja2Templates(directory="templates")

# --- МОДЕЛИ ---
class AuthModel(BaseModel):
    username: str
    password: str

class ProfileUpdateModel(BaseModel):
    username: str
    bio: str
    avatar_url: str

class FriendRequestModel(BaseModel):
    sender: str
    receiver: str

class RespondRequestModel(BaseModel):
    request_id: int
    action: str 

@app.on_event("startup")
async def startup():
    await init_db()

# --- МЕНЕДЖЕР ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def broadcast(self, data: dict):
        for connection in self.active_connections.values():
            try: await connection.send_text(json.dumps(data))
            except: pass 

    async def send_personal_message(self, message: dict, username: str):
        if username in self.active_connections:
            websocket = self.active_connections[username]
            try: await websocket.send_text(json.dumps(message))
            except: pass

manager = ConnectionManager()

# --- РОУТЫ ---
@app.get("/")
async def get(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/register")
async def register(user: AuthModel):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id FROM users WHERE username = :u"), {"u": user.username})
        if result.scalar(): raise HTTPException(status_code=400, detail="Ник занят!")
        await session.execute(text("INSERT INTO users (username, password, bio) VALUES (:u, :p, 'Новичок')"), {"u": user.username, "p": user.password})
        await session.commit()
    return {"message": "Success", "avatar_url": "", "bio": "Новичок"}

@app.post("/login")
async def login(user: AuthModel):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT avatar_url, bio FROM users WHERE username = :u AND password = :p"), {"u": user.username, "p": user.password})
        row = result.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Неверные данные")
    return {"message": "Success", "avatar_url": row[0], "bio": row[1]}

@app.post("/update_profile")
async def update_profile(data: ProfileUpdateModel):
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE users SET avatar_url = :a, bio = :b WHERE username = :u"), {"a": data.avatar_url, "b": data.bio, "u": data.username})
        await session.commit()
    return {"message": "Updated"}

@app.post("/send_request")
async def send_request(data: FriendRequestModel):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id FROM users WHERE username = :u"), {"u": data.receiver})
        if not res.scalar(): raise HTTPException(status_code=404, detail="Пользователь не найден")
        if data.sender == data.receiver: raise HTTPException(status_code=400, detail="Нельзя добавить себя")

        u1, u2 = sorted([data.sender, data.receiver])
        friends = await session.execute(text("SELECT id FROM dms WHERE user1=:u1 AND user2=:u2"), {"u1": u1, "u2": u2})
        if friends.scalar(): raise HTTPException(status_code=400, detail="Вы уже друзья!")

        existing = await session.execute(text("SELECT id FROM friend_requests WHERE sender=:s AND receiver=:r"), {"s": data.sender, "r": data.receiver})
        if existing.scalar(): raise HTTPException(status_code=400, detail="Заявка уже отправлена")

        await session.execute(text("INSERT INTO friend_requests (sender, receiver, status) VALUES (:s, :r, 'pending')"), {"s": data.sender, "r": data.receiver})
        await session.commit()
    
    await manager.send_personal_message({"type": "new_request", "sender": data.sender}, data.receiver)
    return {"message": "Sent"}

@app.get("/get_requests")
async def get_requests(username: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, sender FROM friend_requests WHERE receiver = :u AND status = 'pending'"), {"u": username})
        return [{"id": row[0], "sender": row[1]} for row in result.fetchall()]

@app.post("/respond_request")
async def respond_request(data: RespondRequestModel):
    sender_name = ""
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT sender, receiver FROM friend_requests WHERE id = :id"), {"id": data.request_id})
        req = res.fetchone()
        if not req: raise HTTPException(status_code=404, detail="Заявка не найдена")
        sender, receiver = req[0], req[1]
        sender_name = sender

        if data.action == "accept":
            u1, u2 = sorted([sender, receiver])
            await session.execute(text("INSERT INTO dms (user1, user2) VALUES (:u1, :u2)"), {"u1": u1, "u2": u2})
            await session.execute(text("DELETE FROM friend_requests WHERE id = :id"), {"id": data.request_id})
        else:
            await session.execute(text("DELETE FROM friend_requests WHERE id = :id"), {"id": data.request_id})
        await session.commit()

    if data.action == "accept":
        await manager.send_personal_message({"type": "request_accepted", "friend": receiver}, sender_name)
    return {"message": "Done"}

@app.get("/get_dms")
async def get_dms(username: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT user1, user2 FROM dms WHERE user1 = :u OR user2 = :u"), {"u": username})
        dms = []
        for row in result:
            friend = row[1] if row[0] == username else row[0]
            dms.append(friend)
        return dms

# --- WEBSOCKET ---
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)

            if data.get("type") == "history":
                async with AsyncSessionLocal() as session:
                    query = text("""
                        SELECT m.id, m.username, m.content, m.channel, m.created_at, u.avatar_url, u.bio
                        FROM messages m
                        LEFT JOIN users u ON m.username = u.username
                        WHERE m.channel = :ch
                        ORDER BY m.id DESC LIMIT 50
                    """)
                    result = await session.execute(query, {"ch": data.get("channel")})
                    history = [{"id": row[0], "username": row[1], "content": row[2], "channel": row[3], "created_at": row[4], "avatar_url": row[5], "bio": row[6]} for row in result.fetchall()]
                    await websocket.send_text(json.dumps(history))

            elif data.get("type") == "message":
                now = datetime.now().strftime("%H:%M")
                async with AsyncSessionLocal() as session:
                    res = await session.execute(text("INSERT INTO messages (username, content, channel, created_at) VALUES (:u, :c, :ch, :t) RETURNING id"), 
                        {"u": data['username'], "c": data['content'], "ch": data['channel'], "t": now})
                    new_id = res.scalar()
                    await session.commit()
                    user_res = await session.execute(text("SELECT avatar_url, bio FROM users WHERE username = :u"), {"u": data['username']})
                    user_row = user_res.fetchone()
                
                data['id'] = new_id
                data['created_at'] = now
                data['avatar_url'] = user_row[0] if user_row else ""
                data['bio'] = user_row[1] if user_row else ""
                await manager.broadcast(data)
            
            # --- НОВОЕ: ОБРАБОТКА "ПЕЧАТАЕТ..." ---
            elif data.get("type") == "typing":
                # Просто пересылаем всем: "Гоша печатает в канале general"
                await manager.broadcast(data)

            elif data.get("type") == "delete":
                async with AsyncSessionLocal() as session:
                    await session.execute(text("DELETE FROM messages WHERE id = :id"), {"id": data.get("message_id")})
                    await session.commit()
                await manager.broadcast(data)

    except WebSocketDisconnect:
        manager.disconnect(username)