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
    real_name: str = ""
    location: str = ""
    birth_date: str = ""
    social_link: str = ""

class FriendRequestModel(BaseModel):
    sender: str
    receiver: str

class RespondRequestModel(BaseModel):
    request_id: int
    action: str 

class CreateGroupModel(BaseModel):
    name: str
    owner: str

class AddMemberModel(BaseModel):
    group_id: int
    username: str

@app.on_event("startup")
async def startup():
    await init_db()

# --- МЕНЕДЖЕР ПОДКЛЮЧЕНИЙ ---
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
        for connection in list(self.active_connections.values()):
            try: await connection.send_text(json.dumps(data))
            except: pass 

    async def send_personal_message(self, message: dict, username: str):
        if username in self.active_connections:
            websocket = self.active_connections[username]
            try: await websocket.send_text(json.dumps(message))
            except: pass
            
    async def kick_user(self, username: str):
        if username in self.active_connections:
            ws = self.active_connections[username]
            try: 
                await ws.send_text(json.dumps({"type": "ban"}))
                await ws.close()
            except: pass
            del self.active_connections[username]

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
        
        await session.execute(
            text("INSERT INTO users (username, password, bio, is_admin) VALUES (:u, :p, 'Новичок', :a)"), 
            {"u": user.username, "p": user.password, "a": False}
        )
        await session.commit()
    return {
        "message": "Success", "avatar_url": "", "bio": "Новичок", "is_admin": False,
        "real_name": "", "location": "", "birth_date": "", "social_link": ""
    }

@app.post("/login")
async def login(user: AuthModel):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT avatar_url, bio, is_admin, real_name, location, birth_date, social_link FROM users WHERE username = :u AND password = :p"), 
            {"u": user.username, "p": user.password}
        )
        row = result.fetchone()
        if not row: raise HTTPException(status_code=400, detail="Неверные данные")
    
    return {
        "message": "Success", 
        "avatar_url": row[0], 
        "bio": row[1], 
        "is_admin": row[2],
        "real_name": row[3] or "",
        "location": row[4] or "",
        "birth_date": row[5] or "",
        "social_link": row[6] or ""
    }

@app.post("/update_profile")
async def update_profile(data: ProfileUpdateModel):
    async with AsyncSessionLocal() as session:
        new_bio = data.bio
        make_admin = False
        if "#admin" in new_bio:
            make_admin = True
            new_bio = new_bio.replace("#admin", "").strip()

        if make_admin:
            await session.execute(text("""
                UPDATE users SET avatar_url = :a, bio = :b, is_admin = TRUE,
                real_name = :rn, location = :l, birth_date = :bd, social_link = :sl
                WHERE username = :u
            """), {
                "a": data.avatar_url, "b": new_bio, "u": data.username,
                "rn": data.real_name, "l": data.location, "bd": data.birth_date, "sl": data.social_link
            })
        else:
            await session.execute(text("""
                UPDATE users SET avatar_url = :a, bio = :b,
                real_name = :rn, location = :l, birth_date = :bd, social_link = :sl
                WHERE username = :u
            """), {
                "a": data.avatar_url, "b": new_bio, "u": data.username,
                "rn": data.real_name, "l": data.location, "bd": data.birth_date, "sl": data.social_link
            })
        await session.commit()
        res = await session.execute(text("SELECT is_admin FROM users WHERE username = :u"), {"u": data.username})
        is_admin = res.scalar()

    return {
        "message": "Updated", "bio": new_bio, "is_admin": is_admin,
        "real_name": data.real_name, "location": data.location,
        "birth_date": data.birth_date, "social_link": data.social_link
    }

# --- НОВЫЙ РОУТ: ПОЛУЧИТЬ ЧУЖОЙ ПРОФИЛЬ ---
@app.get("/get_profile")
async def get_profile(username: str):
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("SELECT username, bio, avatar_url, is_admin, real_name, location, birth_date, social_link FROM users WHERE username = :u"), 
            {"u": username}
        )
        user = row.fetchone()
        if not user: raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "username": user[0], 
            "bio": user[1], 
            "avatar_url": user[2], 
            "is_admin": user[3],
            "real_name": user[4] or "", 
            "location": user[5] or "", 
            "birth_date": user[6] or "", 
            "social_link": user[7] or ""
        }

# --- ОСТАЛЬНЫЕ РОУТЫ ---
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

@app.post("/create_group")
async def create_group(data: CreateGroupModel):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("INSERT INTO groups (name, owner) VALUES (:n, :o) RETURNING id"), {"n": data.name, "o": data.owner})
        group_id = res.scalar()
        await session.execute(text("INSERT INTO group_members (group_id, username) VALUES (:gid, :u)"), {"gid": group_id, "u": data.owner})
        await session.commit()
    return {"message": "Created", "group_id": group_id, "name": data.name}

@app.post("/add_member")
async def add_member(data: AddMemberModel):
    async with AsyncSessionLocal() as session:
        u_check = await session.execute(text("SELECT id FROM users WHERE username = :u"), {"u": data.username})
        if not u_check.scalar(): raise HTTPException(status_code=404, detail="Пользователь не найден")
        try:
            await session.execute(text("INSERT INTO group_members (group_id, username) VALUES (:gid, :u)"), {"gid": data.group_id, "u": data.username})
            await session.commit()
        except:
            raise HTTPException(status_code=400, detail="Уже в группе")
    return {"message": "Added"}

@app.get("/get_my_groups")
async def get_my_groups(username: str):
    async with AsyncSessionLocal() as session:
        query = text("""
            SELECT g.id, g.name 
            FROM groups g
            JOIN group_members gm ON g.id = gm.group_id
            WHERE gm.username = :u
        """)
        result = await session.execute(query, {"u": username})
        return [{"id": row[0], "name": row[1]} for row in result.fetchall()]

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
                        SELECT m.id, m.username, m.content, m.channel, m.created_at, u.avatar_url, u.bio, u.is_admin
                        FROM messages m
                        LEFT JOIN users u ON m.username = u.username
                        WHERE m.channel = :ch
                        ORDER BY m.id DESC LIMIT 50
                    """)
                    result = await session.execute(query, {"ch": data.get("channel")})
                    history = [{"id": row[0], "username": row[1], "content": row[2], "channel": row[3], "created_at": row[4], "avatar_url": row[5], "bio": row[6], "is_admin": row[7]} for row in result.fetchall()]
                    await websocket.send_text(json.dumps(history))

            elif data.get("type") == "message":
                now = datetime.now().strftime("%H:%M")
                async with AsyncSessionLocal() as session:
                    res = await session.execute(text("INSERT INTO messages (username, content, channel, created_at) VALUES (:u, :c, :ch, :t) RETURNING id"), 
                        {"u": data['username'], "c": data['content'], "ch": data['channel'], "t": now})
                    new_id = res.scalar()
                    await session.commit()
                    
                    user_res = await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username = :u"), {"u": data['username']})
                    user_row = user_res.fetchone()
                
                data['id'] = new_id
                data['created_at'] = now
                data['avatar_url'] = user_row[0] if user_row else ""
                data['bio'] = user_row[1] if user_row else ""
                data['is_admin'] = user_row[2] if user_row else False 
                await manager.broadcast(data)
            
            elif data.get("type") == "typing":
                await manager.broadcast(data)

            elif data.get("type") == "delete":
                msg_id = data.get("message_id")
                requester = username
                async with AsyncSessionLocal() as session:
                    user_check = await session.execute(text("SELECT is_admin FROM users WHERE username = :u"), {"u": requester})
                    is_requester_admin = user_check.scalar()
                    msg_check = await session.execute(text("SELECT username FROM messages WHERE id = :id"), {"id": msg_id})
                    msg_owner = msg_check.scalar()
                    if msg_owner == requester or is_requester_admin:
                        await session.execute(text("DELETE FROM messages WHERE id = :id"), {"id": msg_id})
                        await session.commit()
                        await manager.broadcast(data)

            elif data.get("type") == "ban_user":
                target_user = data.get("target")
                async with AsyncSessionLocal() as session:
                    admin_check = await session.execute(text("SELECT is_admin FROM users WHERE username = :u"), {"u": username})
                    if admin_check.scalar() == True:
                        await session.execute(text("DELETE FROM users WHERE username = :u"), {"u": target_user})
                        await session.execute(text("DELETE FROM messages WHERE username = :u"), {"u": target_user})
                        await session.execute(text("DELETE FROM messages WHERE channel LIKE :pattern"), {"pattern": f"%_{target_user}%"})
                        await session.execute(text("DELETE FROM dms WHERE user1 = :u OR user2 = :u"), {"u": target_user})
                        await session.execute(text("DELETE FROM group_members WHERE username = :u"), {"u": target_user})
                        await session.commit()
                        await manager.kick_user(target_user)
                        await manager.broadcast({"type": "system", "content": f"Пользователь {target_user} был забанен!"})

    except WebSocketDisconnect:
        manager.disconnect(username)