"""HTTP + WebSocket server. Run with:  python -m buddy"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .audio import pcm_to_wav
from .config import DATA_DIR, SECRET_KEYS, WEB_DIR, Settings
from .hub import Hub, Session
from .memory import CATEGORIES
from .tts import TTSUnavailable

log = logging.getLogger("buddy")
PORT = int(os.environ.get("BUDDY_PORT", "8000"))


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Advertiser:
    """Announces _buddy._tcp on the LAN so the ESP32 can find the server by itself."""

    def __init__(self):
        self.zc = None
        self.info = None

    def start(self, port: int) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.info("zeroconf not installed; the ESP32 will need the server's IP address")
            return
        ip = local_ip()
        try:
            self.info = ServiceInfo(
                "_buddy._tcp.local.", "Buddy Server._buddy._tcp.local.",
                addresses=[socket.inet_aton(ip)], port=port,
                properties={"path": "/ws"}, server=f"buddy-{socket.gethostname().split('.')[0]}.local.")
            self.zc = Zeroconf()
            self.zc.register_service(self.info, allow_name_change=True)
            log.info("advertising _buddy._tcp on %s:%d", ip, port)
        except Exception as e:  # noqa: BLE001
            log.warning("mDNS advertising failed: %s", e)

    def stop(self) -> None:
        if self.zc:
            try:
                self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:  # noqa: BLE001
                pass


def create_app(settings: Settings | None = None, http=None) -> FastAPI:
    settings = settings or Settings(DATA_DIR)
    hub = Hub(settings, http)
    adv = Advertiser()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        if os.environ.get("BUDDY_NO_MDNS") != "1":
            await asyncio.to_thread(adv.start, PORT)
        ip = local_ip()
        log.info("Buddy is up. Web page: http://localhost:%d  (ESP32 server address: %s port %d)", PORT, ip, PORT)
        yield
        await hub.stop()
        await asyncio.to_thread(adv.stop)

    app = FastAPI(title="Buddy", lifespan=lifespan)
    app.state.hub = hub
    app.state.settings = settings

    def check_token(token: str | None) -> bool:
        need = settings["device_token"]
        return not need or token == need

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-buddy-token") or request.query_params.get("token")
            if not check_token(token):
                return JSONResponse({"error": "token required"}, status_code=401)
        return await call_next(request)

    # ---------- websocket ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        role = ws.query_params.get("role", "ui")
        name = ws.query_params.get("name", role)[:40]
        if role not in ("ui", "device") or not check_token(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await ws.accept()
        s = Session(hub, ws, role, name)
        hub.add(s)
        await hub.broadcast_ui({"type": "event", "kind": "sessions"})
        if role == "ui":
            await hub.greet(s)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    s.feed(msg["bytes"])
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    await s.on_message(data)
        except WebSocketDisconnect:
            pass
        except RuntimeError as e:
            log.info("socket closed: %s", e)
        finally:
            hub.remove(s)
            hub.record("disconnect", session=s.id, role=s.role)
            await hub.broadcast_ui({"type": "event", "kind": "sessions"})

    # ---------- pages ----------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------- status & settings ----------
    @app.get("/api/status")
    async def status():
        return hub.status()

    @app.get("/api/settings")
    async def get_settings():
        return {"settings": settings.all(), "keys": settings.secret_status()}

    @app.post("/api/settings")
    async def post_settings(body: dict):
        applied = settings.update(body)
        if {"latitude", "longitude", "timezone"} & applied.keys():
            asyncio.create_task(hub.weather.fetch(force=True))
        return {"applied": applied}

    @app.post("/api/secrets")
    async def post_secret(body: dict):
        name = body.get("name")
        if name not in SECRET_KEYS:
            raise HTTPException(400, "unknown key")
        value = (body.get("value") or "").strip()
        settings.set_secret(name, value)
        if name == "GROQ_API_KEY":
            await hub.llm.discover(force=True)
            code = getattr(hub.llm, "discover_status", None)
            message = ("Groq key removed." if not value else
                       "Groq key works." if code == 200 else
                       "Groq didn't accept that key. Copy it again (it starts with gsk_)." if code in (401, 403) else
                       "Key saved, but Groq couldn't be reached to check it. Is the internet on?")
        else:
            hub.tts.eleven.blocked_until = 0
            quota = await hub.tts.eleven.refresh_quota()
            code = getattr(hub.tts.eleven, "quota_status", None)
            if not value:
                message = "ElevenLabs key removed. Buddy will use the offline voice."
            elif code == 200:
                left = (quota["limit"] - quota["used"]) if quota and quota.get("limit") else None
                message = "ElevenLabs key works." + (f" {left:,} characters left this month." if left is not None else "")
            elif code in (401, 403):
                message = ("Key saved, but ElevenLabs wouldn't confirm it. If Buddy's voice sounds robotic, "
                           "make a new key with Text to Speech access turned on.")
            else:
                message = "Key saved, but ElevenLabs couldn't be reached to check it."
        return {"keys": settings.secret_status(), "check": {"ok": code == 200, "message": message}}

    @app.get("/api/log")
    async def get_log():
        return list(hub.log)

    @app.get("/api/models")
    async def models():
        await hub.llm.discover(force=True)
        return hub.llm.status()

    # ---------- alarms ----------
    @app.get("/api/alarms")
    async def list_alarms():
        return hub.alarms.to_api()

    @app.post("/api/alarms")
    async def add_alarm(body: dict):
        kind = body.get("kind", "alarm")
        try:
            if kind == "timer":
                a = hub.alarms.add_timer(int(body["seconds"]), body.get("label", ""))
            elif kind == "reminder" and body.get("seconds"):
                a = hub.alarms.add_reminder_in(int(body["seconds"]), body.get("label", ""))
            else:
                a = hub.alarms.add_alarm(body["time"], body.get("repeat") or None, body.get("label", ""),
                                         body.get("date") or None, kind=kind)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"bad alarm: {e}")
        return {"alarm": a.id, "confirm": hub.alarms.confirm_text(a)}

    @app.patch("/api/alarms/{alarm_id}")
    async def patch_alarm(alarm_id: str, body: dict):
        a = hub.alarms.set_enabled(alarm_id, bool(body.get("enabled", True)))
        if not a:
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/alarms/{alarm_id}")
    async def delete_alarm(alarm_id: str):
        if not hub.alarms.cancel(alarm_id):
            raise HTTPException(404)
        return {"ok": True}

    # ---------- memory ----------
    @app.get("/api/memory")
    async def list_memory():
        from dataclasses import asdict
        return {"items": [asdict(m) for m in hub.memory.list()], "status": hub.memory_mgr.last_status,
                "categories": CATEGORIES}

    @app.post("/api/memory")
    async def add_memory(body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty")
        m, created = hub.memory.add(text, body.get("category", "other"), pinned=bool(body.get("pinned", True)),
                                    source="user")
        return {"id": m.id, "created": created}

    @app.patch("/api/memory/{mid}")
    async def edit_memory(mid: int, body: dict):
        m = hub.memory.update(mid, body.get("text"), body.get("category"), body.get("pinned"))
        if not m:
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/memory/{mid}")
    async def delete_memory(mid: int):
        if not hub.memory.delete([mid]):
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/memory/compact")
    async def compact_memory():
        return await hub.memory_mgr.compact(force=True)

    @app.post("/api/memory/learn-now")
    async def learn_now():
        transcript, hub.conv.transcript = hub.conv.transcript, []
        return await hub.memory_mgr.learn_from(transcript)

    # ---------- weather ----------
    @app.get("/api/weather")
    async def weather(day: str = "today"):
        data = await hub.weather.fetch()
        if not data:
            raise HTTPException(503, hub.weather.last_error or "weather unavailable")
        out = hub.weather.summarize(data, day)
        out["daily"] = data.get("daily")
        out["hourly"] = {k: v[:48] for k, v in data.get("hourly", {}).items()}
        return out

    @app.get("/api/geocode")
    async def geocode(q: str):
        try:
            return await hub.weather.geocode(q)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e))

    # ---------- voice ----------
    @app.post("/api/say")
    async def say(body: dict):
        text = (body.get("text") or "").strip()
        targets = [s for s in hub.sessions.values() if body.get("session") in (None, s.id)]
        for s in targets:
            asyncio.create_task(s.speak(text))
        return {"sent_to": [s.id for s in targets]}

    @app.post("/api/tts/preview")
    async def tts_preview(body: dict):
        text = (body.get("text") or "Hello! I'm Buddy. This is how I sound.")[:300]
        pcm = bytearray()
        try:
            async for chunk in hub.tts.stream(text):
                pcm += chunk
        except TTSUnavailable as e:
            raise HTTPException(503, str(e))
        return Response(pcm_to_wav(bytes(pcm)), media_type="audio/wav",
                        headers={"X-Engine": hub.tts.last_engine})

    return app
