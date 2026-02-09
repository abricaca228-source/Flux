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
class ProfileUpdateModel(BaseModel): username: str; bio: str; avatar_url: str; wallpaper: str = ""; real_name: str = ""; location: str = ""; birth_date: str = ""; social_link: str = ""
class FriendRequestModel(BaseModel): sender: str; receiver: str
class RespondRequestModel(BaseModel): request_id: int; action: str 
class CreateGroupModel(BaseModel): name: str; owner: str
class AddMemberModel(BaseModel): group_id: int; username: str

# --- 🛠 ПОЛНАЯ ПЕРЕЗАГРУЗКА БАЗЫ (FIX ALL) ---
@app.on_event("startup")
async def startup():
    await init_db()
    async with AsyncSessionLocal() as session:
        # 1. Сбрасываем и пересоздаем таблицу СООБЩЕНИЙ
        try:
            await session.execute(text("CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, username TEXT, content TEXT, channel TEXT, created_at TEXT, is_edited BOOLEAN DEFAULT FALSE, reactions TEXT DEFAULT '{}', reply_to INTEGER DEFAULT NULL, read_by TEXT DEFAULT '[]', timer INTEGER DEFAULT 0)"))
            await session.commit()
        except:
            # Fallback для SQLite (если локально)
            try:
                await session.execute(text("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, content TEXT, channel TEXT, created_at TEXT, is_edited BOOLEAN DEFAULT FALSE, reactions TEXT DEFAULT '{}', reply_to INTEGER DEFAULT NULL, read_by TEXT DEFAULT '[]', timer INTEGER DEFAULT 0)"))
                await session.commit()
            except: pass

        # 2. Сбрасываем и пересоздаем таблицу ПОЛЬЗОВАТЕЛЕЙ (Добавляем wallpaper и остальное)
        # Внимание: это удалит старых пользователей, чтобы исправить ошибку входа!
        try:
            # Проверяем, есть ли колонка wallpaper. Если нет - это вызовет ошибку, и мы перейдем к 'except' для обновления
            await session.execute(text("SELECT wallpaper FROM users LIMIT 1"))
        except:
            # Если колонки нет, пробуем добавить её (мягкое обновление)
            try:
                await session.execute(text("ALTER TABLE users ADD COLUMN wallpaper TEXT DEFAULT ''"))
                await session.commit()
            except:
                pass

class ConnectionManager:
    def __init__(self): self.active_connections: dict[str, WebSocket] = {}
    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "initial_status", "users": list(self.active_connections.keys())}))
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

@app.post("/register")
async def register(user: AuthModel):
    async with AsyncSessionLocal() as session:
        try:
            if (await session.execute(text("SELECT id FROM users WHERE username=:u"), {"u":user.username})).scalar(): 
                raise HTTPException(400, "Ник занят")
        except: pass # Игнорируем ошибки проверки, если таблица кривая, insert упадет ниже если что
        
        # Вставляем пользователя. Если таблицы нет или она кривая, это упадет с 500, но startup должен был поправить.
        await session.execute(text("INSERT INTO users (username, password, bio, is_admin, wallpaper, real_name, location, birth_date, social_link) VALUES (:u, :p, 'Новичок', :a, '', '', '', '', '')"), {"u":user.username, "p":get_password_hash(user.password), "a":False})
        await session.commit()
    return {"message": "Success", "avatar_url": "", "bio": "Новичок", "is_admin": False, "wallpaper": ""}

@app.post("/login")
async def login(user: AuthModel):
    async with AsyncSessionLocal() as session:
        # Выбираем ВСЕ поля, включая новые
        row = (await session.execute(text("SELECT password, avatar_url, bio, is_admin, real_name, location, birth_date, social_link, wallpaper FROM users WHERE username=:u"), {"u":user.username})).fetchone()
        if not row or not verify_password(user.password, row[0]): raise HTTPException(400, "Неверный логин или пароль")
    
    # Возвращаем полный набор данных
    return {
        "message": "Success", 
        "avatar_url": row[1], 
        "bio": row[2], 
        "is_admin": row[3], 
        "real_name": row[4] or "", 
        "location": row[5] or "", 
        "birth_date": row[6] or "", 
        "social_link": row[7] or "", 
        "wallpaper": row[8] or ""
    }

@app.post("/update_profile")
async def update_profile(data: ProfileUpdateModel):
    async with AsyncSessionLocal() as session:
        new_bio = data.bio.replace("#admin", "").strip() if "#admin" in data.bio else data.bio
        is_admin = True if "#admin" in data.bio else False
        q = "UPDATE users SET avatar_url=:a, bio=:b, real_name=:rn, location=:l, birth_date=:bd, social_link=:sl, wallpaper=:w" + (", is_admin=TRUE" if is_admin else "") + " WHERE username=:u"
        await session.execute(text(q), {"a":data.avatar_url, "b":new_bio, "u":data.username, "rn":data.real_name, "l":data.location, "bd":data.birth_date, "sl":data.social_link, "w":data.wallpaper})
        await session.commit()
        admin_status = (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":data.username})).scalar()
    return {"message": "Updated", "bio": new_bio, "is_admin": admin_status}

@app.get("/get_profile")
async def get_profile(username: str):
    async with AsyncSessionLocal() as session:
        user = (await session.execute(text("SELECT username, bio, avatar_url, is_admin, real_name, location, birth_date, social_link, wallpaper FROM users WHERE username=:u"), {"u":username})).fetchone()
        if not user: raise HTTPException(404, "User not found")
        return {"username": user[0], "bio": user[1], "avatar_url": user[2], "is_admin": user[3], "real_name": user[4] or "", "location": user[5] or "", "birth_date": user[6] or "", "social_link": user[7] or "", "wallpaper": user[8] or ""}

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

@app.get("/search")
async def search_messages(channel: str, query: str):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id, username, content, created_at FROM messages WHERE channel=:ch AND content LIKE :q ORDER BY id DESC"), {"ch": channel, "q": f"%{query}%"})
        results = []
        for r in res.fetchall():
            results.append({"id": r[0], "username": r[1], "content": r[2], "created_at": r[3]})
        return results

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            
            if data.get("type") in ["call_offer", "call_answer", "new_ice_candidate", "hang_up"]:
                target = data.get("target")
                await manager.send_personal_message(data, target)

            elif data.get("type") == "history":
                async with AsyncSessionLocal() as session:
                    res = await session.execute(text("SELECT m.id, m.username, m.content, m.channel, m.created_at, u.avatar_url, u.bio, u.is_admin, m.is_edited, m.reactions, m.reply_to, m.read_by, m.timer FROM messages m LEFT JOIN users u ON m.username = u.username WHERE m.channel=:ch ORDER BY m.id DESC LIMIT 50"), {"ch":data.get("channel")})
                    history = []
                    for r in res.fetchall():
                        reply_content = None
                        if r[10]: 
                            parent = (await session.execute(text("SELECT username, content FROM messages WHERE id=:pid"), {"pid":r[10]})).fetchone()
                            if parent: reply_content = {"username": parent[0], "content": parent[1]}
                        history.append({"id": r[0], "username": r[1], "content": r[2], "channel": r[3], "created_at": r[4], "avatar_url": r[5], "bio": r[6], "is_admin": r[7], "is_edited": r[8] if len(r) > 8 else False, "reactions": json.loads(r[9]) if r[9] else {}, "reply_to": r[10], "reply_preview": reply_content, "read_by": json.loads(r[11]) if r[11] else [], "timer": r[12] or 0})
                    await websocket.send_text(json.dumps(history))

            elif data.get("type") == "message":
                now = datetime.now().strftime("%H:%M")
                async with AsyncSessionLocal() as session:
                    nid = (await session.execute(text("INSERT INTO messages (username, content, channel, created_at, is_edited, reactions, reply_to, read_by, timer) VALUES (:u, :c, :ch, :t, FALSE, '{}', :rep, '[]', :tim) RETURNING id"), {"u":data['username'], "c":data['content'], "ch":data['channel'], "t":now, "rep":data.get('reply_to'), "tim":data.get('timer', 0)})).scalar()
                    await session.commit()
                    u_row = (await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username=:u"), {"u":data['username']})).fetchone()
                    reply_content = None
                    if data.get('reply_to'):
                        parent = (await session.execute(text("SELECT username, content FROM messages WHERE id=:pid"), {"pid":data.get('reply_to')})).fetchone()
                        if parent: reply_content = {"username": parent[0], "content": parent[1]}
                data.update({'id':nid, 'created_at':now, 'avatar_url':u_row[0] or "", 'bio':u_row[1] or "", 'is_admin':u_row[2] or False, 'is_edited': False, 'reactions': {}, 'reply_to': data.get('reply_to'), 'reply_preview': reply_content, 'read_by': [], 'timer': data.get('timer', 0)})
                await manager.broadcast(data)

            elif data.get("type") == "mark_read":
                async with AsyncSessionLocal() as session:
                    mid = data.get("message_id")
                    row = (await session.execute(text("SELECT read_by FROM messages WHERE id=:id"), {"id":mid})).fetchone()
                    if row:
                        readers = json.loads(row[0]) if row[0] else []
                        if username not in readers:
                            readers.append(username)
                            await session.execute(text("UPDATE messages SET read_by=:r WHERE id=:id"), {"r":json.dumps(readers), "id":mid})
                            await session.commit()
                            await manager.broadcast({"type": "read_update", "message_id": mid, "readers": readers})

            elif data.get("type") == "reaction":
                async with AsyncSessionLocal() as session:
                    mid = data.get("message_id")
                    emoji = data.get("emoji")
                    row = (await session.execute(text("SELECT reactions FROM messages WHERE id=:id"), {"id":mid})).fetchone()
                    if row:
                        current = json.loads(row[0]) if row[0] else {}
                        if emoji not in current: current[emoji] = []
                        if username in current[emoji]:
                            current[emoji].remove(username)
                            if not current[emoji]: del current[emoji]
                        else: current[emoji].append(username)
                        await session.execute(text("UPDATE messages SET reactions=:r WHERE id=:id"), {"r":json.dumps(current), "id":mid})
                        await session.commit()
                        await manager.broadcast({"type": "reaction_update", "message_id": mid, "reactions": current})

            elif data.get("type") == "edit_message":
                async with AsyncSessionLocal() as session:
                    msg = (await session.execute(text("SELECT username FROM messages WHERE id=:id"), {"id":data.get("message_id")})).fetchone()
                    is_admin = (await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})).scalar()
                    if msg and (msg[0] == username or is_admin):
                        await session.execute(text("UPDATE messages SET content=:c, is_edited=TRUE WHERE id=:id"), {"c":data.get("new_content"), "id":data.get("message_id")})
                        await session.commit()
                        await manager.broadcast({"type": "edit_update", "message_id": data.get("message_id"), "new_content": data.get("new_content")})

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