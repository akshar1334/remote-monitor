"""
Remote Monitoring Portal – Backend
FastAPI + WebSockets + SQLite (via SQLAlchemy)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-use-random-256bit-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./monitor.db")

# ── Database ──────────────────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

log = logging.getLogger("uvicorn.error")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="user")  # admin | user
    created_at = Column(DateTime, default=datetime.utcnow)
    assigned_agents = relationship("AgentAssignment", back_populates="user")


class AgentRecord(Base):
    __tablename__ = "agents"
    id = Column(String, primary_key=True)  # UUID from agent config
    hostname = Column(String)
    token = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime)
    online = Column(Boolean, default=False)
    last_data = Column(Text)  # JSON snapshot
    assignments = relationship("AgentAssignment", back_populates="agent")


class AgentAssignment(Base):
    __tablename__ = "agent_assignments"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    user = relationship("User", back_populates="assigned_agents")
    agent = relationship("AgentRecord", back_populates="assignments")


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbDep = Annotated[Session, Depends(get_db)]

# ── Auth helpers ──────────────────────────────────────────────────────────────
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def get_current_user_from_token(token: str, db: Session) -> User:
    try:
        payload = decode_token(token)
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_current_user(request: Request, db: DbDep, access_token: str | None = Cookie(default=None)) -> User:
    token = access_token
    if not token:
        # Try Authorization header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return get_current_user_from_token(token, db)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(current_user: CurrentUser) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]

# ── Seed admin user ───────────────────────────────────────────────────────────


def seed_admin():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.role == "admin").first():
            admin = User(
                id=str(uuid.uuid4()),
                username="admin",
                hashed_password=hash_password("Admin@1234"),
                role="admin",
            )
            db.add(admin)
            db.commit()
            log.info("Default admin created: admin / Admin@1234  ← CHANGE THIS!")
    finally:
        db.close()


seed_admin()

# ── WebSocket connection managers ─────────────────────────────────────────────


class AgentConnectionManager:
    """Manages live WebSocket connections from agents."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}  # agent_id -> ws

    async def connect(self, agent_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[agent_id] = ws

    def disconnect(self, agent_id: str):
        self._connections.pop(agent_id, None)

    async def send(self, agent_id: str, data: dict):
        ws = self._connections.get(agent_id)
        if ws:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                self.disconnect(agent_id)

    def online_ids(self) -> list[str]:
        return list(self._connections.keys())


class PortalConnectionManager:
    """Manages live WebSocket connections from browser clients."""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, data: dict):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            self.disconnect(ws)


agent_mgr = AgentConnectionManager()
portal_mgr = PortalConnectionManager()

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Remote Monitoring Portal", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "portal", "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "portal", "static")), name="static")


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/agent/{agent_id}", response_class=HTMLResponse)
async def agent_detail_page(request: Request, agent_id: str):
    return templates.TemplateResponse("agent_detail.html", {"request": request, "agent_id": agent_id})


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    return templates.TemplateResponse("admin_users.html", {"request": request})


@app.get("/admin/agents", response_class=HTMLResponse)
async def admin_agents_page(request: Request):
    return templates.TemplateResponse("admin_agents.html", {"request": request})


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(
    db: DbDep,
    username: str = Form(...),
    password: str = Form(...),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(
        {"sub": user.username, "role": user.role},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    response = JSONResponse({"message": "Login successful", "role": user.role, "username": user.username})
    response.set_cookie("access_token", token, httponly=False, samesite="lax", max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie("access_token")
    return response


@app.get("/api/auth/me")
async def me(current_user: CurrentUser):
    return {"username": current_user.username, "role": current_user.role, "id": current_user.id}


# ── User management (Admin only) ──────────────────────────────────────────────

@app.get("/api/users")
async def list_users(db: DbDep, _: AdminUser):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role, "created_at": str(u.created_at)} for u in users]


@app.post("/api/users", status_code=201)
async def create_user(
    db: DbDep,
    _: AdminUser,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
):
    if role not in ("admin", "user"):
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(400, "Username already exists")
    user = User(username=username, hashed_password=hash_password(password), role=role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, db: DbDep, current: AdminUser):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == current.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(user)
    db.commit()
    return {"message": "User deleted"}


# ── Agent management (Admin only) ─────────────────────────────────────────────

@app.get("/api/agents")
async def list_agents(db: DbDep, current_user: CurrentUser):
    if current_user.role == "admin":
        agents = db.query(AgentRecord).all()
    else:
        assignments = db.query(AgentAssignment).filter(AgentAssignment.user_id == current_user.id).all()
        agent_ids = [a.agent_id for a in assignments]
        agents = db.query(AgentRecord).filter(AgentRecord.id.in_(agent_ids)).all()

    live_ids = agent_mgr.online_ids()
    return [
        {
            "id": ag.id,
            "hostname": ag.hostname,
            "online": ag.id in live_ids,
            "last_seen": str(ag.last_seen) if ag.last_seen else None,
            "created_at": str(ag.created_at),
        }
        for ag in agents
    ]


@app.post("/api/agents", status_code=201)
async def create_agent(db: DbDep, _: AdminUser, hostname: str = Form("")):
    token = str(uuid.uuid4())
    agent = AgentRecord(
        id=str(uuid.uuid4()),
        hostname=hostname or "unknown",
        token=token,
    )
    db.add(agent)
    db.commit()
    return {"id": agent.id, "hostname": agent.hostname, "token": token}


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str, db: DbDep, _: AdminUser):
    agent = db.query(AgentRecord).filter(AgentRecord.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    db.delete(agent)
    db.commit()
    return {"message": "Agent deleted"}


@app.post("/api/agents/{agent_id}/assign")
async def assign_agent(agent_id: str, user_id: str = Form(...), db: Session = Depends(get_db), _: User = Depends(require_admin)):
    agent = db.query(AgentRecord).filter(AgentRecord.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    existing = db.query(AgentAssignment).filter_by(user_id=user_id, agent_id=agent_id).first()
    if not existing:
        db.add(AgentAssignment(user_id=user_id, agent_id=agent_id))
        db.commit()
    return {"message": "Assigned"}


@app.get("/api/agents/{agent_id}/data")
async def get_agent_data(agent_id: str, db: DbDep, current_user: CurrentUser):
    if current_user.role != "admin":
        assignment = db.query(AgentAssignment).filter_by(user_id=current_user.id, agent_id=agent_id).first()
        if not assignment:
            raise HTTPException(403, "Access denied")
    agent = db.query(AgentRecord).filter(AgentRecord.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    data = json.loads(agent.last_data) if agent.last_data else {}
    return data


# ── Commands (Admin only) ─────────────────────────────────────────────────────

@app.post("/api/agents/{agent_id}/command")
async def send_command(agent_id: str, db: DbDep, _: AdminUser, action: str = Form(...), pid: int = Form(None)):
    agent = db.query(AgentRecord).filter(AgentRecord.id == agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    cmd: dict = {"action": action}
    if pid is not None:
        cmd["pid"] = pid
    await agent_mgr.send(agent_id, cmd)
    return {"message": f"Command '{action}' sent to agent {agent_id}"}


# ── WebSocket: Agent ──────────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket, db: Session = Depends(get_db)):
    # Authenticate via Authorization header
    auth_header = websocket.headers.get("Authorization", "")
    agent_id_header = websocket.headers.get("X-Agent-ID", "")

    if not auth_header.startswith("Bearer "):
        await websocket.close(code=4001, reason="Missing token")
        return

    token = auth_header[7:]
    agent = db.query(AgentRecord).filter(AgentRecord.token == token).first()
    if not agent:
        await websocket.close(code=4003, reason="Invalid agent token")
        return

    # Update agent ID if provided
    if agent_id_header and agent_id_header != agent.id:
        agent.id = agent_id_header

    agent.online = True
    agent.last_seen = datetime.utcnow()
    db.commit()

    await agent_mgr.connect(agent.id, websocket)
    await portal_mgr.broadcast({"type": "agent_status", "agent_id": agent.id, "online": True, "hostname": agent.hostname})

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type in ("data", "register"):
                payload = msg.get("payload", msg)
                agent.last_data = json.dumps(payload)
                agent.last_seen = datetime.utcnow()
                db.commit()
                # Push to all connected portal clients
                await portal_mgr.broadcast({"type": "agent_data", "agent_id": agent.id, "data": payload})

            elif msg_type in ("command_result", "pong"):
                await portal_mgr.broadcast({"type": msg_type, "agent_id": agent.id, "data": msg})

    except WebSocketDisconnect:
        pass
    finally:
        agent_mgr.disconnect(agent.id)
        db2 = SessionLocal()
        try:
            ag = db2.query(AgentRecord).filter(AgentRecord.id == agent.id).first()
            if ag:
                ag.online = False
                db2.commit()
        finally:
            db2.close()
        await portal_mgr.broadcast({"type": "agent_status", "agent_id": agent.id, "online": False})


# ── WebSocket: Portal browser client ─────────────────────────────────────────

@app.websocket("/ws/portal")
async def ws_portal(websocket: WebSocket, db: Session = Depends(get_db)):
    # Read token from query param (browsers can't set WS headers easily)
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    try:
        payload = decode_token(token)
        username = payload.get("sub")
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise ValueError("user not found")
    except Exception:
        await websocket.close(code=4003, reason="Invalid token")
        return

    await portal_mgr.connect(websocket)
    # Send current agent statuses on connect
    live_ids = agent_mgr.online_ids()
    agents = db.query(AgentRecord).all()
    snapshot = [
        {"id": ag.id, "hostname": ag.hostname, "online": ag.id in live_ids, "last_seen": str(ag.last_seen)}
        for ag in agents
    ]
    await portal_mgr.send_to(websocket, {"type": "agent_snapshot", "agents": snapshot})

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            # Portal → server commands (admin only)
            if user.role == "admin":
                action = msg.get("action")
                target_agent = msg.get("agent_id")
                if action and target_agent:
                    await agent_mgr.send(target_agent, msg)
    except WebSocketDisconnect:
        pass
    finally:
        portal_mgr.disconnect(websocket)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
