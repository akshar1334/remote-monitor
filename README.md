# Remote Monitoring Portal

A Python 3-based remote monitoring system consisting of a Windows agent, a FastAPI backend, and a real-time web portal with role-based access control.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                  Web Browser                    │
│   (Dashboard, Agent Detail, Admin Pages)        │
└────────────────┬────────────────────────────────┘
                 │ HTTPS / WSS /ws/portal
┌────────────────▼────────────────────────────────┐
│            FastAPI Backend (main.py)            │
│  ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │ REST API │ │ WS Portal│ │  WS Agent      │  │
│  │ /api/*   │ │ Manager  │ │  Manager       │  │
│  └──────────┘ └──────────┘ └────────┬───────┘  │
│        │             │              │           │
│  ┌─────▼─────────────▼──────────────▼───────┐  │
│  │           SQLite (SQLAlchemy)             │  │
│  │  Users | Agents | AgentAssignments        │  │
│  └───────────────────────────────────────────┘  │
└────────────────────────────────┬────────────────┘
                                 │ WSS /ws/agent
┌────────────────────────────────▼────────────────┐
│          Windows Agent (agent.py / .exe)        │
│  ┌─────────────────────────────────────────┐    │
│  │ psutil  │ winreg  │ socket  │ platform  │    │
│  └─────────────────────────────────────────┘    │
│   Collects: system info, processes, storage,    │
│   network, users, installed apps                │
└─────────────────────────────────────────────────┘
```

### Component Summary

| Component | Technology | Location |
|-----------|-----------|----------|
| Agent | Python 3 + psutil + websockets → PyInstaller .exe | `agent/` |
| Backend | FastAPI + SQLAlchemy + python-jose | `backend/` |
| Portal (templates) | Jinja2 HTML + Vanilla JS | `portal/templates/` |
| Portal (static) | CSS + JS | `portal/static/` |

---

## Directory Structure

```
remote-monitor/
├── agent/
│   ├── agent.py           # Agent application source
│   ├── agent.spec         # PyInstaller build spec
│   ├── config.json        # Agent configuration template
│   └── requirements.txt   # Agent Python dependencies
├── backend/
│   ├── main.py            # FastAPI application (all-in-one)
│   └── requirements.txt   # Backend Python dependencies
├── portal/
│   ├── templates/
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── dashboard.html
│   │   ├── agent_detail.html
│   │   ├── admin_users.html
│   │   └── admin_agents.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
└── README.md
```

---

## Setup Instructions

### Prerequisites

- Python 3.9 or higher
- pip
- (Windows only, for agent build) PyInstaller

### 1. Backend Setup

```bash
cd backend
pip install -r requirements.txt

# Generate a secure secret key
python -c "import secrets; print(secrets.token_hex(32))"
# Copy the output and set it as SECRET_KEY

# Start the server
SECRET_KEY=<your-key> python main.py
# Or with uvicorn directly:
SECRET_KEY=<your-key> uvicorn main:app --host 0.0.0.0 --port 8000
```

The server will:
- Create `monitor.db` (SQLite database)
- Seed a default admin user: **admin / Admin@1234** ← **change immediately**
- Serve the portal at `http://localhost:8000`

### 2. Agent Setup

**Install dependencies:**
```bash
cd agent
pip install -r requirements.txt
```

**Configure the agent:**
1. Log in to the portal as admin
2. Go to Admin → Agents → Register Agent
3. Copy the generated token
4. Edit `config.json`:
   ```json
   {
     "server_url": "ws://YOUR_SERVER_IP:8000/ws/agent",
     "agent_token": "PASTE_TOKEN_HERE",
     "agent_id": "UNIQUE_UUID"
   }
   ```
5. Run the agent: `python agent.py`

**Build the Windows .exe:**
```bash
cd agent
pip install pyinstaller
pyinstaller agent.spec
# Output: dist/RemoteMonitorAgent.exe
```
Copy `dist/RemoteMonitorAgent.exe` and `config.json` to the target Windows machine.

### 3. First Login

1. Open `http://localhost:8000`
2. Login with `admin` / `Admin@1234`
3. **Immediately change the password** via Admin → Users

---

## Role-Based Access Control

| Feature | Admin | User |
|---------|-------|------|
| View all agents | ✅ | ❌ |
| View assigned agents | ✅ | ✅ |
| Send commands to agents | ✅ | ❌ |
| Kill processes | ✅ | ❌ |
| Manage users | ✅ | ❌ |
| Register/delete agents | ✅ | ❌ |
| Assign agents to users | ✅ | ❌ |
| Read-only agent dashboard | ✅ | ✅ |

---

## API Documentation

### Authentication

All API endpoints require the `access_token` cookie (set on login) or an `Authorization: Bearer <token>` header.

#### POST `/api/auth/login`
Authenticate and receive a session cookie.
- **Body (form-data):** `username`, `password`
- **Response:** `{ message, role, username }`
- **Sets:** `access_token` HTTP-only cookie

#### POST `/api/auth/logout`
Clear the session cookie.

#### GET `/api/auth/me`
Return current user info: `{ id, username, role }`

---

### Users (Admin only)

#### GET `/api/users`
List all users.

#### POST `/api/users`
Create a user.
- **Body:** `username`, `password`, `role` (admin|user)

#### DELETE `/api/users/{user_id}`
Delete a user (cannot delete yourself).

---

### Agents

#### GET `/api/agents`
List agents. Admins see all; users see only assigned agents.

#### POST `/api/agents` (Admin)
Register a new agent. Returns a one-time token.
- **Body:** `hostname`
- **Response:** `{ id, hostname, token }`

#### DELETE `/api/agents/{agent_id}` (Admin)
Delete an agent record.

#### POST `/api/agents/{agent_id}/assign` (Admin)
Assign an agent to a user.
- **Body:** `user_id`

#### GET `/api/agents/{agent_id}/data`
Get the latest cached data snapshot for an agent.

#### POST `/api/agents/{agent_id}/command` (Admin)
Send a command to a connected agent.
- **Body:** `action` (refresh|kill_process|ping), `pid` (for kill_process)

---

### WebSocket Endpoints

#### `ws://host/ws/agent`
Agent connection endpoint.
- **Headers:** `Authorization: Bearer <agent_token>`, `X-Agent-ID: <uuid>`
- **Agent → Server messages:**
  - `{ type: "register", agent_id, hostname }`
  - `{ type: "data", payload: { system_info, processes, ... } }`
  - `{ type: "command_result", action, success, ... }`
  - `{ type: "pong", timestamp }`
- **Server → Agent messages:**
  - `{ action: "refresh" }` — request data refresh
  - `{ action: "kill_process", pid: 1234 }` — kill a process
  - `{ action: "ping" }` — connectivity check

#### `ws://host/ws/portal?token=<jwt>`
Browser client connection.
- **Query param:** `token` (JWT from login)
- **Server → Client messages:**
  - `{ type: "agent_snapshot", agents: [...] }` — full list on connect
  - `{ type: "agent_status", agent_id, online, hostname }` — connect/disconnect
  - `{ type: "agent_data", agent_id, data: {...} }` — live data update
  - `{ type: "command_result", agent_id, data }` — command response
- **Client → Server messages (Admin only):**
  - Any command object forwarded to the target agent

---

## Database Schema

```
┌──────────────────────────────────────────────────────┐
│ users                                                │
├──────────────────────────────────────────────────────┤
│ id            VARCHAR  PK                            │
│ username      VARCHAR  UNIQUE NOT NULL               │
│ hashed_password VARCHAR NOT NULL                     │
│ role          VARCHAR  (admin|user)                  │
│ created_at    DATETIME                               │
└──────────────────────────────┬───────────────────────┘
                               │ 1:N
┌──────────────────────────────▼───────────────────────┐
│ agent_assignments                                    │
├──────────────────────────────────────────────────────┤
│ id            VARCHAR  PK                            │
│ user_id       VARCHAR  FK → users.id                 │
│ agent_id      VARCHAR  FK → agents.id                │
└──────────────────────────────────────────────────────┘
                               │ N:1
┌──────────────────────────────▼───────────────────────┐
│ agents                                               │
├──────────────────────────────────────────────────────┤
│ id            VARCHAR  PK (UUID from agent)          │
│ hostname      VARCHAR                                │
│ token         VARCHAR  UNIQUE (bearer token)         │
│ created_at    DATETIME                               │
│ last_seen     DATETIME                               │
│ online        BOOLEAN                                │
│ last_data     TEXT     (JSON snapshot)               │
└──────────────────────────────────────────────────────┘
```

---

## Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Password storage | bcrypt hashing via passlib |
| Session tokens | JWT with expiry (HS256), HTTP-only cookies |
| Agent authentication | Per-agent UUID bearer tokens stored in DB |
| Role enforcement | Server-side checks on every request, not just UI |
| Input validation | FastAPI/Pydantic validates all form inputs |
| SQL injection | SQLAlchemy ORM – no raw queries |
| WebSocket auth | JWT validated before accepting portal WS; agent token validated before accepting agent WS |
| Process kill | Admin role required; confirmation on client |
| SECRET_KEY | Must be set via environment variable; never hardcoded in production |

### Production Checklist

- [ ] Set a strong `SECRET_KEY` environment variable
- [ ] Change the default admin password immediately
- [ ] Use HTTPS/WSS with a valid TLS certificate (nginx reverse proxy recommended)
- [ ] Restrict agent token exposure (tokens shown only once at creation)
- [ ] Run backend with a non-root system user
- [ ] Consider using PostgreSQL instead of SQLite for production
- [ ] Enable firewall: allow port 8000 only from trusted IPs

---

## Agent Data Collected

| Category | Fields |
|----------|--------|
| System Info | hostname, IP, OS, version, architecture, processor, CPU count/freq/usage, RAM total/used, swap, boot time, uptime |
| Processes | PID, name, username, status, CPU%, RAM%, create time, cmdline |
| Storage | device, mountpoint, fstype, total/used/free GB, percent |
| Users | username, terminal, host, login time |
| Network | connections (local/remote addr, status, PID), interface addresses, I/O counters |
| Installed Apps | name, version (Windows registry; empty on other platforms) |

## Agent Commands

| Command | Description |
|---------|-------------|
| `refresh` | Request immediate full data snapshot |
| `kill_process` | Terminate a process by PID |
| `ping` | Check connectivity (agent replies with `pong`) |
