import json
import re
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from database import AsyncSessionLocal, init_db
from passlib.context import CryptContext
import aiohttp

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def get_password_hash(password): return pwd_context.hash(password)
def verify_password(plain, hashed): 
    try: return pwd_context.verify(plain, hashed)
    except: return False

class AuthModel(BaseModel): 
    username: str; password: str; real_name: str = ""; birth_date: str = ""

class ProfileUpdateModel(BaseModel): 
    username: str; bio: str; avatar_url: str; wallpaper: str = ""; real_name: str = ""; location: str = ""; birth_date: str = ""; social_link: str = ""

class FriendRequestModel(BaseModel): sender: str; receiver: str
class RespondRequestModel(BaseModel): request_id: int; action: str 
class CreateGroupModel(BaseModel): name: str; owner: str
class AddMemberModel(BaseModel): group_id: int; username: str
class PinMessageModel(BaseModel): message_id: int; channel: str; username: str
class ForwardMessageModel(BaseModel): message_id: int; target_channel: str; username: str
class VoiceChannelModel(BaseModel): name: str; group_id: int = None; created_by: str
class JoinVoiceModel(BaseModel): channel_id: int; username: str
class UpdateStatusModel(BaseModel): username: str; status: str; custom_status: str = None
class UpdateThemeModel(BaseModel): username: str; theme: str

@app.on_event("startup")
async def startup():
    await init_db()

class ConnectionManager:
    def __init__(self): self.active_connections: dict[str, WebSocket] = {}
    async def connect(self, websocket: WebSocket, username: str):
        # При подключении сразу шлём список уже онлайн-юзеров,
        # чтобы клиент мог подсветить статусы, как в Discord/Telegram.
        await websocket.accept()
        try:
            await websocket.send_text(json.dumps({
                "type": "initial_status",
                "users": list(self.active_connections.keys())
            }))
        except Exception:
            # Если по какой-то причине не получилось отправить — не рвём подключение
            pass

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
        if (await session.execute(text("SELECT id FROM users WHERE username=:u"), {"u":user.username})).scalar(): raise HTTPException(400, "Ник занят")
        await session.execute(text("INSERT INTO users (username, password, bio, is_admin, wallpaper, real_name, location, birth_date, social_link) VALUES (:u, :p, 'Новичок', :a, '', :rn, '', :bd, '')"), {"u":user.username, "p":get_password_hash(user.password), "a":False, "rn":user.real_name, "bd":user.birth_date})
        exists = (await session.execute(text("SELECT id FROM dms WHERE user1=:u AND user2=:u"), {"u":user.username})).scalar()
        if not exists: await session.execute(text("INSERT INTO dms (user1, user2) VALUES (:u, :u)"), {"u":user.username})
        await session.commit()
    return {"message": "Success"}

@app.post("/login")
async def login(user: AuthModel):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT password, avatar_url, bio, is_admin, real_name, location, birth_date, social_link, wallpaper FROM users WHERE username=:u"), {"u":user.username})).fetchone()
        if not row or not verify_password(user.password, row[0]): raise HTTPException(400, "Неверный логин или пароль")
    return {"message": "Success", "avatar_url": row[1], "bio": row[2], "is_admin": row[3], "real_name": row[4] or "", "location": row[5] or "", "birth_date": row[6] or "", "social_link": row[7] or "", "wallpaper": row[8] or ""}

@app.post("/update_profile")
async def update_profile(data: ProfileUpdateModel):
    async with AsyncSessionLocal() as session:
        q = "UPDATE users SET avatar_url=:a, bio=:b, real_name=:rn, location=:l, birth_date=:bd, social_link=:sl, wallpaper=:w WHERE username=:u"
        await session.execute(text(q), {"a":data.avatar_url, "b":data.bio, "u":data.username, "rn":data.real_name, "l":data.location, "bd":data.birth_date, "sl":data.social_link, "w":data.wallpaper})
        await session.commit()
        # Берём свежие данные профиля, чтобы отдать фронтенду полный объект,
        # который сразу подойдёт для updateMyUI (аватар, обои, био, админ и т.д.).
        row = (
            await session.execute(
                text(
                    "SELECT username, bio, avatar_url, is_admin, real_name, location, birth_date, social_link, wallpaper "
                    "FROM users WHERE username=:u"
                ),
                {"u": data.username},
            )
        ).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        profile = {
            "username": row[0],
            "bio": row[1] or "",
            "avatar_url": row[2] or "",
            "is_admin": row[3] or False,
            "real_name": row[4] or "",
            "location": row[5] or "",
            "birth_date": row[6] or "",
            "social_link": row[7] or "",
            "wallpaper": row[8] or "",
        }
    
    # !!! ВАЖНО: Моментальное уведомление всем, что профиль обновился !!!
    await manager.broadcast({
        "type": "profile_update",
        "username": data.username,
        "avatar_url": data.avatar_url,
        "bio": data.bio
    })
    # Возвращаем полный профиль для текущего пользователя
    return profile

@app.get("/get_profile")
async def get_profile(username: str):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT username, bio, avatar_url, is_admin, real_name, location, birth_date, social_link, wallpaper FROM users WHERE username=:u"), {"u":username})).fetchone()
        if not row: raise HTTPException(404, "User not found")
        return {"username": row[0], "bio": row[1], "avatar_url": row[2], "is_admin": row[3], "real_name": row[4] or "", "location": row[5] or "", "birth_date": row[6] or "", "social_link": row[7] or "", "wallpaper": row[8] or ""}

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
    # Уведомляем получателя моментально
    await manager.send_personal_message({"type": "new_request", "sender": data.sender}, data.receiver)
    return {"message": "Sent"}

@app.get("/get_requests")
async def get_requests(username: str):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id, sender FROM friend_requests WHERE receiver=:u AND status='pending'"), {"u":username})
        return [{"id": r[0], "sender": r[1]} for r in res.fetchall()]

@app.post("/respond_request")
async def respond_request(data: RespondRequestModel):
    async with AsyncSessionLocal() as session:
        req = (await session.execute(text("SELECT sender, receiver FROM friend_requests WHERE id=:id"), {"id":data.request_id})).fetchone()
        if not req: raise HTTPException(404, "Заявка не найдена")
        sender, receiver = req[0], req[1]
        if data.action == "accept":
            u1, u2 = sorted([sender, receiver])
            await session.execute(text("INSERT INTO dms (user1, user2) VALUES (:u1, :u2)"), {"u1":u1, "u2":u2})
            # Уведомляем обоих, что они теперь друзья (для обновления списка)
            await manager.send_personal_message({"type": "request_accepted", "friend": receiver}, sender)
            await manager.send_personal_message({"type": "request_accepted", "friend": sender}, receiver)
        await session.execute(text("DELETE FROM friend_requests WHERE id=:id"), {"id":data.request_id})
        await session.commit()
    return {"message": "Done"}

@app.get("/get_dms")
async def get_dms(username: str):
    async with AsyncSessionLocal() as session:
        dms = []
        res = await session.execute(text("SELECT user1, user2 FROM dms WHERE user1=:u OR user2=:u"), {"u":username})
        for r in res.fetchall():
            if r[0] == username and r[1] == username: dms.append("Избранное")
            else: dms.append(r[1] if r[0] == username else r[0])
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
        res = await session.execute(text("SELECT g.id, g.name FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username=:u"), {"u":username})
        return [{"id": r[0], "name": r[1]} for r in res.fetchall()]

@app.get("/search")
async def search_messages(channel: str, query: str):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id, username, content, created_at FROM messages WHERE channel=:ch AND content LIKE :q ORDER BY id DESC"), {"ch": channel, "q": f"%{query}%"})
        results = []
        for r in res.fetchall(): results.append({"id": r[0], "username": r[1], "content": r[2], "created_at": r[3]})
        return results

@app.post("/pin_message")
async def pin_message(data: PinMessageModel):
    async with AsyncSessionLocal() as session:
        # Проверяем, что сообщение существует
        msg = (await session.execute(text("SELECT username FROM messages WHERE id=:id"), {"id":data.message_id})).fetchone()
        if not msg: raise HTTPException(404, "Message not found")
        # Проверяем, не закреплено ли уже
        existing = (await session.execute(text("SELECT id FROM pinned_messages WHERE message_id=:id"), {"id":data.message_id})).scalar()
        if existing: raise HTTPException(400, "Уже закреплено")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        await session.execute(text("INSERT INTO pinned_messages (message_id, channel, pinned_by, pinned_at) VALUES (:mid, :ch, :by, :at)"), {"mid":data.message_id, "ch":data.channel, "by":data.username, "at":now})
        await session.execute(text("UPDATE messages SET is_pinned=TRUE WHERE id=:id"), {"id":data.message_id})
        await session.commit()
    await manager.broadcast({"type": "message_pinned", "message_id": data.message_id, "channel": data.channel})
    return {"message": "Pinned"}

@app.post("/unpin_message")
async def unpin_message(data: dict):
    message_id = data.get("message_id")
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM pinned_messages WHERE message_id=:id"), {"id":message_id})
        await session.execute(text("UPDATE messages SET is_pinned=FALSE WHERE id=:id"), {"id":message_id})
        await session.commit()
    return {"message": "Unpinned"}

@app.get("/get_pinned")
async def get_pinned(channel: str):
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT pm.message_id, pm.pinned_by, pm.pinned_at, m.username, m.content, m.created_at FROM pinned_messages pm JOIN messages m ON pm.message_id = m.id WHERE pm.channel=:ch ORDER BY pm.pinned_at DESC"), {"ch":channel})
        pinned = []
        for r in res.fetchall():
            pinned.append({"message_id": r[0], "pinned_by": r[1], "pinned_at": r[2], "username": r[3], "content": r[4], "created_at": r[5]})
        return pinned

@app.post("/forward_message")
async def forward_message(data: ForwardMessageModel):
    async with AsyncSessionLocal() as session:
        # Получаем оригинальное сообщение
        orig = (await session.execute(text("SELECT username, content, created_at FROM messages WHERE id=:id"), {"id":data.message_id})).fetchone()
        if not orig: raise HTTPException(404, "Message not found")
        now = datetime.now().strftime("%H:%M")
        # Создаём пересланное сообщение
        nid = (await session.execute(text("INSERT INTO messages (username, content, channel, created_at, is_edited, reactions, reply_to, read_by, timer, viewed_at, forwarded_from) VALUES (:u, :c, :ch, :t, FALSE, '{}', NULL, '[]', 0, NULL, :fw) RETURNING id"), {"u":data.username, "c":orig[1], "ch":data.target_channel, "t":now, "fw":orig[0]})).scalar()
        await session.commit()
        res_u = await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username=:u"), {"u":data.username})
        u_row = res_u.fetchone()
    forwarded_msg = {'id':nid, 'username':data.username, 'content':orig[1], 'channel':data.target_channel, 'created_at':now, 'avatar_url':u_row[0] or "", 'bio':u_row[1] or "", 'is_admin':u_row[2] or False, 'forwarded_from':orig[0], 'is_edited':False, 'reactions':{}, 'read_by':[], 'timer':0}
    await manager.broadcast(forwarded_msg)
    return {"message": "Forwarded", "message_id": nid}

# --- НОВЫЕ ФУНКЦИИ ---

@app.post("/update_status")
async def update_status(data: UpdateStatusModel):
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE users SET status=:s, custom_status=:cs WHERE username=:u"), {"s":data.status, "cs":data.custom_status or None, "u":data.username})
        await session.commit()
    await manager.broadcast({"type": "status_update", "username": data.username, "status": data.status, "custom_status": data.custom_status})
    return {"message": "Updated"}

@app.post("/update_theme")
async def update_theme(data: UpdateThemeModel):
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE users SET theme=:t WHERE username=:u"), {"t":data.theme, "u":data.username})
        await session.commit()
    return {"message": "Theme updated", "theme": data.theme}

@app.get("/get_link_preview")
async def get_link_preview(url: str):
    """Получает превью ссылки (title, description, image)"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return {"error": "Failed to fetch"}
                html = await resp.text()
                # Простой парсинг meta тегов
                title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
                og_title = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                og_desc = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                og_image = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                
                title = (og_title.group(1) if og_title else None) or (title_match.group(1).strip() if title_match else None) or "Ссылка"
                desc = og_desc.group(1) if og_desc else None
                image = og_image.group(1) if og_image else None
                
                # YouTube специальная обработка
                if "youtube.com" in url or "youtu.be" in url:
                    video_id = re.search(r'(?:v=|/)([0-9A-Za-z_-]{11})', url)
                    if video_id:
                        image = f"https://img.youtube.com/vi/{video_id.group(1)}/maxresdefault.jpg"
                
                return {"title": title[:100], "description": desc[:200] if desc else None, "image": image, "url": url}
    except Exception as e:
        return {"error": str(e)}

@app.post("/create_voice_channel")
async def create_voice_channel(data: VoiceChannelModel):
    async with AsyncSessionLocal() as session:
        vid = (await session.execute(text("INSERT INTO voice_channels (name, group_id, created_by) VALUES (:n, :gid, :by) RETURNING id"), {"n":data.name, "gid":data.group_id, "by":data.created_by})).scalar()
        await session.commit()
    await manager.broadcast({"type": "voice_channel_created", "channel_id": vid, "name": data.name, "group_id": data.group_id})
    return {"message": "Created", "channel_id": vid}

@app.post("/join_voice")
async def join_voice(data: JoinVoiceModel):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("INSERT INTO voice_channel_members (channel_id, username, joined_at) VALUES (:cid, :u, :at)"), {"cid":data.channel_id, "u":data.username, "at":now})
            await session.commit()
        except:
            pass  # Уже в канале
    await manager.broadcast({"type": "voice_joined", "channel_id": data.channel_id, "username": data.username})
    return {"message": "Joined"}

@app.post("/leave_voice")
async def leave_voice(channel_id: int, username: str):
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM voice_channel_members WHERE channel_id=:cid AND username=:u"), {"cid":channel_id, "u":username})
        await session.commit()
    await manager.broadcast({"type": "voice_left", "channel_id": channel_id, "username": username})
    return {"message": "Left"}

@app.get("/get_voice_channels")
async def get_voice_channels(group_id: int = None):
    async with AsyncSessionLocal() as session:
        if group_id:
            res = await session.execute(text("SELECT vc.id, vc.name, vc.group_id, vc.created_by, COUNT(vcm.username) as members FROM voice_channels vc LEFT JOIN voice_channel_members vcm ON vc.id = vcm.channel_id WHERE vc.group_id=:gid GROUP BY vc.id"), {"gid":group_id})
        else:
            res = await session.execute(text("SELECT vc.id, vc.name, vc.group_id, vc.created_by, COUNT(vcm.username) as members FROM voice_channels vc LEFT JOIN voice_channel_members vcm ON vc.id = vcm.channel_id GROUP BY vc.id"))
        channels = []
        for r in res.fetchall():
            channels.append({"id": r[0], "name": r[1], "group_id": r[2], "created_by": r[3], "members": r[4]})
        return channels

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
                    res = await session.execute(text("SELECT m.id, m.username, m.content, m.channel, m.created_at, u.avatar_url, u.bio, u.is_admin, m.is_edited, m.reactions, m.reply_to, m.read_by, m.timer, m.viewed_at, m.mentions, m.forwarded_from, m.is_pinned, m.link_preview FROM messages m LEFT JOIN users u ON m.username = u.username WHERE m.channel=:ch ORDER BY m.id DESC LIMIT 50"), {"ch":data.get("channel")})
                    history = []
                    for r in res.fetchall():
                        reply_content = None
                        if r[10]: 
                            res_p = await session.execute(text("SELECT username, content FROM messages WHERE id=:pid"), {"pid":r[10]})
                            parent = res_p.fetchone()
                            if parent: reply_content = {"username": parent[0], "content": parent[1]}
                        mentions_list = json.loads(r[14]) if r[14] else []
                        link_preview_obj = json.loads(r[17]) if r[17] else None
                        history.append({"id": r[0], "username": r[1], "content": r[2], "channel": r[3], "created_at": r[4], "avatar_url": r[5], "bio": r[6], "is_admin": r[7], "is_edited": r[8] if len(r) > 8 else False, "reactions": json.loads(r[9]) if r[9] else {}, "reply_to": r[10], "reply_preview": reply_content, "read_by": json.loads(r[11]) if r[11] else [], "timer": r[12] or 0, "viewed_at": r[13], "mentions": mentions_list, "forwarded_from": r[15] or None, "is_pinned": r[16] or False, "link_preview": link_preview_obj})
                    await websocket.send_text(json.dumps(history))

            elif data.get("type") == "message":
                now = datetime.now().strftime("%H:%M")
                content = data['content']
                
                # Обработка команд ботов
                if content.startswith('/'):
                    cmd_parts = content.split(' ', 1)
                    cmd = cmd_parts[0].lower()
                    args = cmd_parts[1] if len(cmd_parts) > 1 else ""
                    
                    bot_response = None
                    if cmd == '/gif':
                        # Используем Giphy API (можно заменить на свой ключ)
                        try:
                            async with aiohttp.ClientSession() as sess:
                                async with sess.get(f"https://api.giphy.com/v1/gifs/random?api_key=dc6zaTOxFJmzC&tag={args or 'funny'}") as resp:
                                    gif_data = await resp.json()
                                    if gif_data.get('data', {}).get('images', {}).get('original', {}).get('url'):
                                        bot_response = f"🎬 {gif_data['data']['images']['original']['url']}"
                        except:
                            bot_response = "❌ Ошибка загрузки GIF"
                    
                    elif cmd == '/weather':
                        if not args:
                            bot_response = "🌤️ Использование: /weather <город>"
                        else:
                            try:
                                async with aiohttp.ClientSession() as sess:
                                    async with sess.get(f"http://wttr.in/{args}?format=3") as resp:
                                        weather = await resp.text()
                                        bot_response = f"🌡️ {weather.strip()}"
                            except:
                                bot_response = f"❌ Не удалось получить погоду для {args}"
                    
                    elif cmd == '/joke':
                        jokes = [
                            "Почему программисты не любят природу? Там слишком много багов!",
                            "Что говорит один байт другому? Мы встретимся на мегабайте!",
                            "Почему Python не может летать? Потому что это змея!",
                            "Как называется программист, который не пьет кофе? Сонный.",
                            "Почему JavaScript разработчики носят очки? Потому что не могут C#!"
                        ]
                        import random
                        bot_response = f"😄 {random.choice(jokes)}"
                    
                    elif cmd == '/help':
                        bot_response = """📋 Доступные команды:
/gif [тег] - случайный GIF
/weather [город] - погода
/joke - случайная шутка
/help - эта справка"""
                    
                    if bot_response:
                        # Отправляем ответ бота как сообщение
                        async with AsyncSessionLocal() as session:
                            nid = (await session.execute(text("INSERT INTO messages (username, content, channel, created_at, is_edited, reactions, reply_to, read_by, timer, viewed_at, mentions) VALUES (:u, :c, :ch, :t, FALSE, '{}', NULL, '[]', 0, NULL, '[]') RETURNING id"), {"u":"🤖 Bot", "c":bot_response, "ch":data['channel'], "t":now})).scalar()
                            await session.commit()
                            res_u = await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username=:u"), {"u":"🤖 Bot"})
                            u_row = res_u.fetchone() or ("", "Бот", False)
                        bot_msg = {'id':nid, 'username':"🤖 Bot", 'content':bot_response, 'channel':data['channel'], 'created_at':now, 'avatar_url':"", 'bio':"Бот", 'is_admin':False, 'is_edited':False, 'reactions':{}, 'read_by':[], 'timer':0, 'mentions':[]}
                        await manager.broadcast(bot_msg)
                        continue  # Не обрабатываем команду как обычное сообщение
                
                # Парсим упоминания @username из текста
                mentions = re.findall(r'@(\w+)', content)
                mentions_json = json.dumps(list(set(mentions)))  # Убираем дубликаты
                
                # Парсим ссылки для превью
                link_preview = None
                url_pattern = re.compile(r'https?://[^\s]+')
                urls = url_pattern.findall(content)
                if urls:
                    try:
                        preview = await get_link_preview(urls[0])
                        if preview.get('title'):
                            link_preview = json.dumps(preview)
                    except:
                        pass
                
                async with AsyncSessionLocal() as session:
                    nid = (await session.execute(text("INSERT INTO messages (username, content, channel, created_at, is_edited, reactions, reply_to, read_by, timer, viewed_at, mentions, forwarded_from, link_preview) VALUES (:u, :c, :ch, :t, FALSE, '{}', :rep, '[]', :tim, NULL, :ment, :fw, :lp) RETURNING id"), {"u":data['username'], "c":content, "ch":data['channel'], "t":now, "rep":data.get('reply_to'), "tim":data.get('timer', 0), "ment":mentions_json, "fw":data.get('forwarded_from'), "lp":link_preview})).scalar()
                    await session.commit()
                    res_u = await session.execute(text("SELECT avatar_url, bio, is_admin FROM users WHERE username=:u"), {"u":data['username']})
                    u_row = res_u.fetchone()
                    reply_content = None
                    if data.get('reply_to'):
                        res_p = await session.execute(text("SELECT username, content FROM messages WHERE id=:pid"), {"pid":data.get('reply_to')})
                        parent = res_p.fetchone()
                        if parent: reply_content = {"username": parent[0], "content": parent[1]}
                    
                    # Отправляем уведомления упомянутым пользователям
                    for mentioned_user in mentions:
                        if mentioned_user != data['username']:  # Не упоминаем себя
                            await manager.send_personal_message({
                                "type": "mention",
                                "message_id": nid,
                                "channel": data['channel'],
                                "from": data['username'],
                                "content": data['content'][:50] + "..." if len(data['content']) > 50 else data['content']
                            }, mentioned_user)
                
                link_preview_obj = json.loads(link_preview) if link_preview else None
                data.update({'id':nid, 'created_at':now, 'avatar_url':u_row[0] or "", 'bio':u_row[1] or "", 'is_admin':u_row[2] or False, 'is_edited': False, 'reactions': {}, 'reply_to': data.get('reply_to'), 'reply_preview': reply_content, 'read_by': [], 'timer': data.get('timer', 0), 'viewed_at': None, 'mentions': mentions, 'link_preview': link_preview_obj})
                await manager.broadcast(data)

            elif data.get("type") == "spy_viewed":
                async with AsyncSessionLocal() as session:
                    view_time = datetime.now().timestamp()
                    await session.execute(text("UPDATE messages SET viewed_at=:vt WHERE id=:id AND viewed_at IS NULL"), {"vt":str(view_time), "id":data.get("message_id")})
                    await session.commit()
                    await manager.broadcast({"type": "spy_start", "message_id": data.get("message_id"), "start_time": view_time})

            elif data.get("type") == "mark_read":
                async with AsyncSessionLocal() as session:
                    mid = data.get("message_id")
                    res = await session.execute(text("SELECT read_by FROM messages WHERE id=:id"), {"id":mid})
                    row = res.fetchone()
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
                    res = await session.execute(text("SELECT reactions FROM messages WHERE id=:id"), {"id":mid})
                    row = res.fetchone()
                    if row:
                        current = json.loads(row[0]) if row[0] else {}
                        if emoji not in current: current[emoji] = []
                        if username in current[emoji]: current[emoji].remove(username); 
                        else: current[emoji].append(username)
                        if not current[emoji]: del current[emoji]
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
                    res_a = await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})
                    is_admin = res_a.scalar()
                    res_m = await session.execute(text("SELECT username FROM messages WHERE id=:id"), {"id":data.get("message_id")})
                    msg_author = res_m.scalar()
                    if is_admin or msg_author == username:
                        await session.execute(text("DELETE FROM messages WHERE id=:id"), {"id":data.get("message_id")})
                        await session.commit()
                        await manager.broadcast(data)

            elif data.get("type") == "typing": await manager.broadcast(data)
            elif data.get("type") == "ban_user":
                async with AsyncSessionLocal() as session:
                    res = await session.execute(text("SELECT is_admin FROM users WHERE username=:u"), {"u":username})
                    if res.scalar():
                        t = data.get("target")
                        for q in ["DELETE FROM users WHERE username=:t", "DELETE FROM messages WHERE username=:t", "DELETE FROM messages WHERE channel LIKE :p", "DELETE FROM dms WHERE user1=:t OR user2=:t", "DELETE FROM group_members WHERE username=:t"]:
                            await session.execute(text(q), {"t":t, "p":f"%_{t}%"})
                        await session.commit()
                        await manager.kick_user(t)
                        await manager.broadcast({"type": "system", "content": f"Пользователь {t} был забанен!"})

    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast({"type": "status", "username": username, "status": "offline"})