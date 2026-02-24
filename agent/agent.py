"""
Remote Monitoring Agent
Collects system information and communicates with the server via WebSocket.
"""

import asyncio
import json
import logging
import os
import platform
import socket
import sys
import time
import uuid
from datetime import datetime

import psutil
import websockets
from websockets.exceptions import ConnectionClosed

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Configuration (override via environment variables or config.json) ─────────
DEFAULT_CONFIG = {
    "server_url": "ws://localhost:8000/ws/agent",
    "agent_token": "",          # Set via env AGENT_TOKEN or config.json
    "agent_id": str(uuid.uuid4()),
    "reconnect_interval": 10,   # seconds
    "heartbeat_interval": 30,   # seconds
    "data_refresh_interval": 60 # seconds
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    config_path = os.path.join(os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg.update(json.load(f))
    # Environment overrides
    if os.environ.get("SERVER_URL"):
        cfg["server_url"] = os.environ["SERVER_URL"]
    if os.environ.get("AGENT_TOKEN"):
        cfg["agent_token"] = os.environ["AGENT_TOKEN"]
    if os.environ.get("AGENT_ID"):
        cfg["agent_id"] = os.environ["AGENT_ID"]
    return cfg


# ── System Data Collection ────────────────────────────────────────────────────

def get_system_info() -> dict:
    """Collect general system information."""
    boot_time = datetime.fromtimestamp(psutil.boot_time()).isoformat()
    cpu_freq = psutil.cpu_freq()
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "hostname": socket.gethostname(),
        "ip_address": socket.gethostbyname(socket.gethostname()),
        "os": platform.system(),
        "os_version": platform.version(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "boot_time": boot_time,
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "cpu_freq_mhz": cpu_freq.current if cpu_freq else None,
        "cpu_percent": psutil.cpu_percent(interval=1),
        "ram_total_mb": round(ram.total / 1024 / 1024, 2),
        "ram_used_mb": round(ram.used / 1024 / 1024, 2),
        "ram_percent": ram.percent,
        "swap_total_mb": round(swap.total / 1024 / 1024, 2),
        "swap_used_mb": round(swap.used / 1024 / 1024, 2),
        "swap_percent": swap.percent,
    }


def get_processes() -> list:
    """Collect running processes with resource usage."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "status", "cpu_percent", "memory_percent", "create_time", "cmdline"]):
        try:
            info = p.info
            procs.append({
                "pid": info["pid"],
                "name": info["name"],
                "username": info["username"],
                "status": info["status"],
                "cpu_percent": round(info["cpu_percent"] or 0, 2),
                "memory_percent": round(info["memory_percent"] or 0, 2),
                "create_time": datetime.fromtimestamp(info["create_time"]).isoformat() if info["create_time"] else None,
                "cmdline": " ".join(info["cmdline"])[:200] if info["cmdline"] else "",
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(procs, key=lambda x: x["cpu_percent"], reverse=True)


def get_storage() -> list:
    """Collect disk partition information."""
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / 1024 ** 3, 2),
                "used_gb": round(usage.used / 1024 ** 3, 2),
                "free_gb": round(usage.free / 1024 ** 3, 2),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            pass
    return partitions


def get_users() -> list:
    """Collect logged-in users."""
    users = []
    for u in psutil.users():
        users.append({
            "name": u.name,
            "terminal": u.terminal,
            "host": u.host,
            "started": datetime.fromtimestamp(u.started).isoformat() if u.started else None,
        })
    return users


def get_network() -> dict:
    """Collect network connections and interface stats."""
    connections = []
    for conn in psutil.net_connections(kind="inet"):
        try:
            connections.append({
                "fd": conn.fd,
                "family": str(conn.family),
                "type": str(conn.type),
                "laddr": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                "raddr": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "",
                "status": conn.status,
                "pid": conn.pid,
            })
        except Exception:
            pass

    interfaces = {}
    for name, addrs in psutil.net_if_addrs().items():
        interfaces[name] = [
            {"family": str(a.family), "address": a.address, "netmask": a.netmask}
            for a in addrs
        ]

    io = psutil.net_io_counters()
    return {
        "connections": connections[:100],  # cap at 100
        "interfaces": interfaces,
        "bytes_sent": io.bytes_sent,
        "bytes_recv": io.bytes_recv,
        "packets_sent": io.packets_sent,
        "packets_recv": io.packets_recv,
    }


def get_installed_apps() -> list:
    """
    Attempt to read installed applications.
    On Windows reads the registry; falls back to an empty list on other platforms.
    """
    apps = []
    if platform.system() == "Windows":
        try:
            import winreg
            reg_paths = [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ]
            for reg_path in reg_paths:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            sub_key_name = winreg.EnumKey(key, i)
                            sub_key = winreg.OpenKey(key, sub_key_name)
                            name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                            try:
                                version, _ = winreg.QueryValueEx(sub_key, "DisplayVersion")
                            except FileNotFoundError:
                                version = ""
                            apps.append({"name": name, "version": version})
                        except (FileNotFoundError, OSError):
                            pass
                except OSError:
                    pass
        except Exception as e:
            log.warning(f"Could not read registry: {e}")
    return apps


def collect_all_data(agent_id: str) -> dict:
    """Collect all system metrics in one call."""
    return {
        "agent_id": agent_id,
        "timestamp": datetime.utcnow().isoformat(),
        "system_info": get_system_info(),
        "processes": get_processes(),
        "storage": get_storage(),
        "users": get_users(),
        "network": get_network(),
        "installed_apps": get_installed_apps(),
    }


# ── Command Handlers ──────────────────────────────────────────────────────────

def handle_command(cmd: dict, agent_id: str) -> dict | None:
    """
    Handle a command received from the server.
    Returns a response dict or None if no reply is needed.
    """
    action = cmd.get("action", "")
    log.info(f"Received command: {action}")

    if action == "refresh":
        return {"type": "data", "payload": collect_all_data(agent_id)}

    if action == "kill_process":
        pid = cmd.get("pid")
        if pid is None:
            return {"type": "command_result", "action": action, "success": False, "error": "No PID provided"}
        try:
            p = psutil.Process(pid)
            p.terminate()
            return {"type": "command_result", "action": action, "success": True, "pid": pid}
        except psutil.NoSuchProcess:
            return {"type": "command_result", "action": action, "success": False, "error": f"Process {pid} not found"}
        except psutil.AccessDenied:
            return {"type": "command_result", "action": action, "success": False, "error": f"Access denied for process {pid}"}

    if action == "ping":
        return {"type": "pong", "timestamp": datetime.utcnow().isoformat()}

    log.warning(f"Unknown command action: {action}")
    return {"type": "command_result", "action": action, "success": False, "error": "Unknown action"}


# ── WebSocket Client ──────────────────────────────────────────────────────────

class MonitoringAgent:
    def __init__(self, config: dict):
        self.config = config
        self.agent_id = config["agent_id"]
        self._running = True

    async def run(self):
        while self._running:
            try:
                await self._connect_and_loop()
            except Exception as e:
                log.error(f"Connection error: {e}")
            if self._running:
                log.info(f"Reconnecting in {self.config['reconnect_interval']}s...")
                await asyncio.sleep(self.config["reconnect_interval"])

    async def _connect_and_loop(self):
        url = self.config["server_url"]
        headers = {
            "Authorization": f"Bearer {self.config['agent_token']}",
            "X-Agent-ID": self.agent_id,
        }
        log.info(f"Connecting to {url} as agent {self.agent_id}")
        async with websockets.connect(url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
            log.info("Connected to server.")
            # Send initial data burst
            await ws.send(json.dumps({
                "type": "register",
                "agent_id": self.agent_id,
                "hostname": socket.gethostname(),
            }))
            await ws.send(json.dumps({"type": "data", "payload": collect_all_data(self.agent_id)}))

            # Schedule periodic data push
            last_data_send = time.time()

            while True:
                now = time.time()
                timeout = max(0.1, self.config["data_refresh_interval"] - (now - last_data_send))

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    msg = json.loads(raw)
                    response = handle_command(msg, self.agent_id)
                    if response:
                        await ws.send(json.dumps(response))
                except asyncio.TimeoutError:
                    pass
                except ConnectionClosed as e:
                    log.warning(f"Connection closed: {e}")
                    raise

                # Periodic data refresh
                if time.time() - last_data_send >= self.config["data_refresh_interval"]:
                    log.debug("Sending periodic data update")
                    await ws.send(json.dumps({"type": "data", "payload": collect_all_data(self.agent_id)}))
                    last_data_send = time.time()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    config = load_config()
    if not config.get("agent_token"):
        log.error("AGENT_TOKEN is not set. Set it in config.json or via the AGENT_TOKEN environment variable.")
        sys.exit(1)

    log.info(f"Agent ID: {config['agent_id']}")
    agent = MonitoringAgent(config)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("Agent stopped by user.")


if __name__ == "__main__":
    main()
