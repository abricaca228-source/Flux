import json
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from database import AsyncSessionLocal, init_db
from passlib.context import CryptContext

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def get_password_hash(password): return pwd_context.hash(password)
def verify_password(plain, hashed): 
    try: return pwd_context.verify(plain, hashed)
    except: return False

class AuthModel(BaseModel): username: str; password: str
class ProfileUpdateModel(BaseModel): username: str; bio: str; avatar_url: str; real_name: str = ""; location: str = ""; birth_date: str = ""; social_link: str = ""
class FriendRequestModel(BaseModel): sender: str; receiver: str
class RespondRequestModel(BaseModel): request_id: int; action: str 
class CreateGroupModel(BaseModel): name: str; owner: str
class AddMemberModel(BaseModel): group_id: int; username: str

@app.on_event("startup")
async def startup():
    await init_db()
    # "Миграция" - добавляем колонку is_edited, если её нет
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("ALTER TABLE messages ADD COLUMN is_edited BOOLEAN DEFAULT FALSE"))
            await session.commit()
        except:
            pass # Колонка уже есть

class ConnectionManager:
    def __init__(self): self.active_connections: dict[str, WebSocket] = {}
    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        online_users = list(self.active_connections.keys())
        await websocket.send_text(json.dumps({"type": "initial_status", "users": online_users}))
        self.active_connections[username] = websocket
        await self.broadcast({"type": "status", "username": username, "status": "online"})
    def disconnect(self, username: str):
        if username in self.active_connections: del self.active_connections[username]
    async def broadcast(self, data: dict):
        for connection in list(self.active_connections.values()):
            try: await connection.send_text(json.dumps(data))
            except: pass 
    async def send_personal_message(self, message: dict, username: str):
        if username in self.active_connections:
            try: await self.active_connections[username].send_text(json.dumps(message))
            except: pass
    async def kick_user(self, username: str):
        if username in self.active_connections:
            try: await self.active_connections[username].send_text(json.dumps({"type": "ban"})); await self.active_connections[username].close()
            except: pass
            del self.active_connections[username]

manager = ConnectionManager()

@app.get("/")
async def get(request: Request): return templates.TemplateResponse("index.html", {"request": request})

@app.get("/secret_reset_database_123")
async def reset_database():
    async with AsyncSessionLocal() as session:
        for table in ["group_members", "groups", "friend_requests", "dms", "messages", "users"]:
            try: await session.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            except: pass
        await session.commit()
    await init_db()
    return {"status": "RESET DONE"}

@app.post("/register")
async def register(user: AuthModel):
    async with AsyncSessionLocal() as session:
        if (await session.execute(text("SELECT id FROM users WHERE username=:u"), {"u":user.username})).scalar(): raise HTTPException(400, "Ник занят")
        await session.execute(text("INSERT INTO users (username, password, bio, is_admin) VALUES (:u, :p, 'Новичок', :a)"), {"u":user.username, "p":get_password_hash(user.password), "a":False})
        await session.commit()
    return {"message": "Success", "avatar_url": "", "bio": "Новичок", "is_admin": False}

@app.post("/login")
async def login(user: AuthModel):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT password, avatar_url, bio, is_admin, real_name, location, birth_date, social_link FROM users WHERE username=:u"), {"u":user.username})).fetchone()
        if not row or not verify_password(user.password, row[0]): raise HTTPException(400, "Ошибка входа")
    return {"message": "Success", "avatar_url": row[1], "bio": row[2], "is_admin": row[3], "real_name": row[4] or "", "location": row[5] or "", "birth_date": row[6] or "", "social_link": row[7] or ""}

@app.post("/update_profile")
async def update_profile(data: ProfileUpdateModel):
    async with AsyncSessionLocal() as session:
        new_bio = data.bio.replace("#admin", "").strip() if "#admin" in data.bio else data.bio
        is_admin = True if "#admin" in data.bio else False
        q = "UPDATE users SET avatar_url=:a, bio=:b, real_name=:rn, location=:l, birth_date=:bd, social_link=:sl" + (", is_admin=TRUE" if is_admin else "") + " WHERE username=:u"
        await session.execute(text(q), {"a":data.avatar_url, "b":new_bio, "u":data.username, "rn":data.real_name, "l":data.location, "bd":data.birth_date, "sl":data.social_link})
        await session.commit()
        admin_status = (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":data.username})).scalar()
    return {"message": "Updated", "bio": new_bio, "is_admin": admin_status}

@app.get("/get_profile")
async def get_profile(username: str):
    async with AsyncSessionLocal() as session:
        user = (await session.execute(text("SELECT username, bio, avatar_url, is_admin, real_name, location, birth_date, social_link FROM users WHERE username=:u"), {"u":username})).fetchone()
        if not user: raise HTTPException(404, "User not found")
        return {"username": user[0], "bio": user[1], "avatar_url": user[2], "is_admin": user[3], "real_name": user[4] or "", "location": user[5] or "", "birth_date": user[6] or "", "social_link": user[7] or ""}

@app.post("/send_request")
async def send_request(data: FriendRequestModel):
    async with AsyncSessionLocal() as session:
        if not (await session.execute(text("SELECT id FROM users WHERE username=:u"), {"u":data.receiver})).scalar(): raise HTTPException(404, "User not found")
        if data.sender == data.receiver: raise HTTPException(400, "Нельзя добавить себя")
        u1, u2 = sorted([data.sender, data.receiver])
        if (await session.execute(text("SELECT id FROM dms WHERE user1=:u1 AND user2=:u2"), {"u1":u1, "u2":u2})).scalar(): raise HTTPException(400, "Уже друзья")
        if (await session.execute(text("SELECT id FROM friend_requests WHERE sender=:s AND receiver=:r"), {"s":data.sender, "r":data.receiver})).scalar(): raise HTTPException(400, "Заявка уже есть")
        await session.execute(text("INSERT INTO friend_requests (sender, receiver, status) VALUES (:s, :r, 'pending')"), {"s":data.sender, "r":data.receiver})
        await session.commit()
    await manager.send_personal_message({"type": "new_request", "sender": data.sender}, data.receiver)
    return {"message": "Sent"}

@app.get("/get_requests")
async def get_requests(username: str):
    async with AsyncSessionLocal() as session:
        return [{"id": r[0], "sender": r[1]} for r in (await session.execute(text("SELECT id, sender FROM friend_requests WHERE receiver=:u AND status='pending'"), {"u":username})).fetchall()]

@app.post("/respond_request")
async def respond_request(data: RespondRequestModel):
    async with AsyncSessionLocal() as session:
        req = (await session.execute(text("SELECT sender, receiver FROM friend_requests WHERE id=:id"), {"id":data.request_id})).fetchone()
        if not req: raise HTTPException(404, "Заявка не найдена")
        if data.action == "accept":
            u1, u2 = sorted([req[0], req[1]])
            await session.execute(text("INSERT INTO dms (user1, user2) VALUES (:u1, :u2)"), {"u1":u1, "u2":u2})
            await manager.send_personal_message({"type": "request_accepted", "friend": req[1]}, req[0])
        await session.execute(text("DELETE FROM friend_requests WHERE id=:id"), {"id":data.request_id})
        await session.commit()
    return {"message": "Done"}

@app.get("/get_dms")
async def get_dms(username: str):
    async with AsyncSessionLocal() as session:
        dms = []
        for r in (await session.execute(text("SELECT user1, user2 FROM dms WHERE user1=:u OR user2=:u"), {"u":username})).fetchall():
            dms.append(r[1] if r[0] == username else r[0])
        return dms

@app.post("/create_group")
async def create_group(data: CreateGroupModel):
    async with AsyncSessionLocal() as session:
        gid = (await session.execute(text("INSERT INTO groups (name, owner) VALUES (:n, :o) RETURNING id"), {"n":data.name, "o":data.owner})).scalar()
        await session.execute(text("INSERT INTO group_members (group_id, username) VALUES (:gid, :u)"), {"gid":gid, "u":data.owner})
        await session.commit()
    return {"message": "Created", "group_id": gid, "name": data.name}

@app.post("/add_member")
async def add_member(data: AddMemberModel):
    async with AsyncSessionLocal() as session:
        if not (await session.execute(text("SELECT id FROM users WHERE username=:u"), {"u":data.username})).scalar(): raise HTTPException(404, "User not found")
        try:
            await session.execute(text("INSERT INTO group_members (group_id, username) VALUES (:gid, :u)"), {"gid":data.group_id, "u":data.username})
            await session.commit()
        except: raise HTTPException(400, "Уже в группе")
    return {"message": "Added"}

@app.get("/get_my_groups")
async def get_my_groups(username: str):
    async with AsyncSessionLocal() as session:
        return [{"id": r[0], "name": r[1]} for r in (await session.execute(text("SELECT g.id, g.name FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username=:u"), {"u":username})).fetchall()]

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            
            # --- WEB RTC СИГНАЛЫ (ЗВОНКИ) ---
            if data.get("type") in ["call_offer", "call_answer", "new_ice_candidate", "hang_up"]:
                target = data.get("target")
                await manager.send_personal_message(data, target)

            # --- ОБЫЧНЫЙ ЧАТ ---
            elif data.get("type") == "history":
                async with AsyncSessionLocal() as session:
                    # Загружаем историю (теперь с пометкой is_edited)
                    res = await session.execute(text("SELECT m.id, m.username, m.content, m.channel, m.created_at, u.avatar_url, u.bio, u.is_admin, m.is_edited FROM messages m LEFT JOIN users u ON m.username = u.username WHERE m.channel=:ch ORDER BY m.id DESC LIMIT 50"), {"ch":data.get("channel")})
                    history = []
                    for r in res.fetchall():
                        history.append({
                            "id": r[0], "username": r[1], "content": r[2], "channel": r[3], 
                            "created_at": r[4], "avatar_url": r[5], "bio": r[6], 
                            "is_admin": r[7], "is_edited": r[8] if len(r) > 8 else False 
                        })
                    await websocket.send_text(json.dumps(history))

            elif data.get("type") == "message":
                now = datetime.now().strftime("%H:%M")
                async with AsyncSessionLocal() as session:
                    nid = (await session.execute(text("INSERT INTO messages (username, content, channel, created_at, is_edited) VALUES (:u, :c, :ch, :t, FALSE) RETURNING id"), {"u":data['username'], "c":data['content'], "ch":data['channel'], "t":now})).scalar()
                    await session.commit()
                    u_row = (await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username=:u"), {"u":data['username']})).fetchone()
                data.update({'id':nid, 'created_at':now, 'avatar_url':u_row[0] or "", 'bio':u_row[1] or "", 'is_admin':u_row[2] or False, 'is_edited': False})
                await manager.broadcast(data)

            # --- НОВАЯ ФУНКЦИЯ: РЕДАКТИРОВАНИЕ ---
            elif data.get("type") == "edit_message":
                async with AsyncSessionLocal() as session:
                    # Проверяем, автор ли это сообщения или админ
                    msg = (await session.execute(text("SELECT username FROM messages WHERE id=:id"), {"id":data.get("message_id")})).fetchone()
                    is_admin = (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})).scalar()
                    
                    if msg and (msg[0] == username or is_admin):
                        await session.execute(text("UPDATE messages SET content=:c, is_edited=TRUE WHERE id=:id"), {"c":data.get("new_content"), "id":data.get("message_id")})
                        await session.commit()
                        # Рассылаем всем обновленную версию
                        await manager.broadcast({
                            "type": "edit_update", 
                            "message_id": data.get("message_id"), 
                            "new_content": data.get("new_content")
                        })

            elif data.get("type") == "delete":
                async with AsyncSessionLocal() as session:
                    if (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})).scalar() or (await session.execute(text("SELECT username FROM messages WHERE id=:id"), {"id":data.get("message_id")})).scalar() == username:
                        await session.execute(text("DELETE FROM messages WHERE id=:id"), {"id":data.get("message_id")})
                        await session.commit()
                        await manager.broadcast(data)

            elif data.get("type") == "typing": await manager.broadcast(data)
            elif data.get("type") == "ban_user":
                async with AsyncSessionLocal() as session:
                    if (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})).scalar():
                        t = data.get("target")
                        for q in ["DELETE FROM users WHERE username=:t", "DELETE FROM messages WHERE username=:t", "DELETE FROM messages WHERE channel LIKE :p", "DELETE FROM dms WHERE user1=:t OR user2=:t", "DELETE FROM group_members WHERE username=:t"]:
                            await session.execute(text(q), {"t":t, "p":f"%_{t}%"})
                        await session.commit()
                        await manager.kick_user(t)
                        await manager.broadcast({"type": "system", "content": f"Пользователь {t} был забанен!"})

    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast({"type": "status", "username": username, "status": "offline"})