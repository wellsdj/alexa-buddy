"""End-to-end through the real FastAPI app and WebSocket protocol."""
import json
import time

from fastapi.testclient import TestClient

from buddy.main import create_app


def recv_until(ws, predicate, limit=400):
    """Read messages until predicate(msg) is true. Returns (match, everything_seen)."""
    seen = []
    for _ in range(limit):
        m = ws.receive()
        if m.get("bytes") is not None:
            msg = {"binary": m["bytes"]}
        else:
            msg = json.loads(m["text"])
        seen.append(msg)
        if predicate(msg):
            return msg, seen
    raise AssertionError(f"never matched; saw {[x.get('type', 'binary') for x in seen][-30:]}")


def test_typed_question_is_answered_with_audio(settings, apis):
    app = create_app(settings, apis.client())
    with TestClient(app) as client, client.websocket_connect("/ws?role=ui&name=test") as ws:
        recv_until(ws, lambda m: m.get("type") == "hello_ack")
        ws.send_text(json.dumps({"type": "text", "text": "what's the weather"}))
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("Right now in Richmond")
        end, seen = recv_until(ws, lambda m: m.get("type") == "audio_end")
        frames = [m["binary"] for m in seen if "binary" in m]
        assert frames and all(f[0] == start["seq"] for f in frames)
        assert sum(len(f) - 1 for f in frames) == end["bytes"] > 0
        ws.send_text(json.dumps({"type": "playback_done", "seq": start["seq"]}))
        recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "idle")
        voice = apis.tts_requests[0]
        assert "output_format=pcm_16000" in voice["url"] and voice["body"]["model_id"] == "eleven_flash_v2_5"


def test_elevenlabs_out_of_credit_tells_browser_to_speak(settings, apis):
    apis.tts_status = 401
    app = create_app(settings, apis.client())
    with TestClient(app) as client, client.websocket_connect("/ws?role=ui") as ws:
        recv_until(ws, lambda m: m.get("type") == "hello_ack")
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))
        say, _ = recv_until(ws, lambda m: m.get("type") == "say")
        assert say["text"].startswith("It's ")
    assert app.state.hub.tts.eleven.blocked_until > time.time()  # won't hammer a dead key


def test_device_gets_schedule_and_reminder_is_spoken(settings, apis):
    app = create_app(settings, apis.client())
    hub = app.state.hub
    with TestClient(app) as client, client.websocket_connect("/ws?role=device&name=esp32") as ws:
        ws.send_text(json.dumps({"type": "hello", "fw": "1.0"}))
        recv_until(ws, lambda m: m.get("type") == "hello_ack")
        first, _ = recv_until(ws, lambda m: m.get("type") == "alarms")
        assert first["fires"] == []
        r = client.post("/api/alarms", json={"kind": "reminder", "seconds": 60, "label": "check the oven"})
        assert r.status_code == 200
        pushed, _ = recv_until(ws, lambda m: m.get("type") == "alarms" and m["fires"])
        rid = pushed["fires"][0]["id"]
        assert pushed["fires"][0]["kind"] == "reminder" and abs(pushed["fires"][0]["at"] - (time.time() + 60)) < 3
        ws.send_text(json.dumps({"type": "alarm_fired", "id": rid, "kind": "reminder"}))
        start, _ = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"] == "Here's your reminder: check the oven."
        assert hub.alarms.get(rid) is None  # one-shot removed after firing


def test_device_gets_mic_boost_and_volume_changes(settings, apis):
    app = create_app(settings, apis.client())
    with TestClient(app) as client, client.websocket_connect("/ws?role=device&name=esp32") as ws:
        ws.send_text(json.dumps({"type": "hello", "fw": "1.0"}))
        ack, _ = recv_until(ws, lambda m: m.get("type") == "hello_ack")
        assert ack["mic_shift"] == 14 and ack["volume"] == 6
        client.post("/api/settings", json={"device_mic_shift": "12", "volume": 9})
        got = {}
        recv_until(ws, lambda m: got.setdefault(m.get("type"), m) and "mic_gain" in got and "volume" in got)
        assert got["mic_gain"]["shift"] == 12 and got["volume"]["level"] == 9


def test_voice_stop_silences_ringing_alarm_and_briefs(settings, apis):
    settings.update({"morning_briefing": True})
    app = create_app(settings, apis.client())
    hub = app.state.hub
    a = hub.alarms.add_alarm("07:00", "daily")
    with TestClient(app) as client, \
            client.websocket_connect("/ws?role=device&name=esp32") as dev, \
            client.websocket_connect("/ws?role=ui") as ui:
        dev.send_text(json.dumps({"type": "hello"}))
        recv_until(dev, lambda m: m.get("type") == "alarms")
        recv_until(ui, lambda m: m.get("type") == "hello_ack")
        dev.send_text(json.dumps({"type": "alarm_fired", "id": a.id, "kind": "alarm"}))
        recv_until(ui, lambda m: m.get("kind") == "ringing" and m.get("id") == a.id)
        ui.send_text(json.dumps({"type": "text", "text": "stop"}))
        stop, _ = recv_until(dev, lambda m: m.get("type") == "alarm_stop")
        assert stop["quiet"] is False
        dev.send_text(json.dumps({"type": "alarm_stopped", "id": a.id, "reason": "voice"}))
        # The briefing only happens in the morning; either way the device must stop ringing.
        assert hub.devices()[0].ringing is None


def test_rest_api_round_trip(settings, apis):
    app = create_app(settings, apis.client())
    with TestClient(app) as client:
        assert client.get("/api/status").json()["keys"]["GROQ_API_KEY"]["set"]
        r = client.post("/api/memory", json={"text": "Likes flat whites", "category": "preferences"})
        mid = r.json()["id"]
        assert client.get("/api/memory").json()["items"][0]["text"] == "Likes flat whites."
        client.patch(f"/api/memory/{mid}", json={"text": "Likes oat flat whites"})
        assert client.get("/api/memory").json()["items"][0]["text"] == "Likes oat flat whites."
        assert client.delete(f"/api/memory/{mid}").json() == {"ok": True}
        r = client.post("/api/alarms", json={"time": "07:15", "repeat": "weekdays", "label": "work"})
        assert "every weekday" in r.json()["confirm"]
        alarms = client.get("/api/alarms").json()
        assert alarms[0]["description"] == "work alarm at 7:15 AM every weekday"
        w = client.get("/api/weather").json()
        assert w["spoken"].startswith("Right now in Richmond") and len(w["daily"]["time"]) == 7
        s = client.post("/api/settings", json={"wake_threshold": "0.6", "bogus": 1}).json()
        assert s["applied"] == {"wake_threshold": 0.6}
        assert client.get("/api/geocode", params={"q": "Richmond"}).json()[0]["label"].startswith("Richmond")
        wav = client.post("/api/tts/preview", json={"text": "hello"})
        assert wav.status_code == 200 and wav.content[:4] == b"RIFF"


def test_token_protects_api_and_socket(settings, apis):
    settings.update({"device_token": "s3cret"})
    app = create_app(settings, apis.client())
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"X-Buddy-Token": "s3cret"}).status_code == 200
        try:
            with client.websocket_connect("/ws?role=device") as ws:
                ws.receive()
            assert False, "socket without token should close"
        except Exception:
            pass
        with client.websocket_connect("/ws?role=ui&token=s3cret") as ws:
            recv_until(ws, lambda m: m.get("type") == "hello_ack")


def test_saving_a_key_says_whether_it_works(settings, apis):
    app = create_app(settings, apis.client())
    with TestClient(app) as client:
        bad = client.post("/api/secrets", json={"name": "GROQ_API_KEY", "value": " gsk_wrong "}).json()
        assert not bad["check"]["ok"] and "didn't accept" in bad["check"]["message"]
        good = client.post("/api/secrets", json={"name": "GROQ_API_KEY", "value": "gsk_right"}).json()
        assert good["check"] == {"ok": True, "message": "Groq key works."}
        el = client.post("/api/secrets", json={"name": "ELEVENLABS_API_KEY", "value": "sk_x"}).json()
        assert el["check"]["message"] == "ElevenLabs key works. 8,800 characters left this month."
        assert settings.secret("GROQ_API_KEY") == "gsk_right"
