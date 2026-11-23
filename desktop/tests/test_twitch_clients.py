"""Twitch client logic against mocked HTTP and a fake local EventSub server (no Twitch traffic)."""

import asyncio
import json

import httpx
import pytest
import websockets

from twitchclipper.twitch.auth import DeviceAuth
from twitchclipper.twitch.eventsub import EventSubClient
from twitchclipper.twitch.helix import Helix, VideoStatus, pick_download


# --- VOD availability wording ----------------------------------------------
def _helix(handler):
    return Helix("cid", "tok", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status,body,expected", [
    (200, {"data": [{"id": "1", "duration": "1h"}]}, VideoStatus.AVAILABLE),
    (200, {"data": []}, VideoStatus.UNAVAILABLE_OR_INACCESSIBLE),
    (404, {"message": "Not Found"}, VideoStatus.UNAVAILABLE_OR_INACCESSIBLE),
    (403, {"message": "Forbidden"}, VideoStatus.FORBIDDEN),
    (401, {"message": "Invalid OAuth token"}, VideoStatus.AUTH_REQUIRED),
    (500, {"message": "boom"}, VideoStatus.CHECK_FAILED),
])
async def test_video_check_never_says_deleted(status, body, expected):
    h = _helix(lambda req: httpx.Response(status, json=body))
    try:
        check = await h.check_video("1")
    finally:
        await h.aclose()
    assert check.status is expected
    msg = check.message.lower()
    assert "deleted" not in check.status.value
    assert "was deleted" not in msg and "has been deleted" not in msg and "is deleted" not in msg
    if expected is VideoStatus.UNAVAILABLE_OR_INACCESSIBLE:
        assert check.message.startswith("Unavailable or inaccessible")


async def test_video_check_network_failure_is_check_failed():
    def boom(req):
        raise httpx.ConnectError("offline")
    h = _helix(boom)
    try:
        check = await h.check_video("1")
    finally:
        await h.aclose()
    assert check.status is VideoStatus.CHECK_FAILED


# --- portrait fallback --------------------------------------------------------
def test_pick_download_portrait_fallback():
    both = {"clip_id": "a", "landscape_download_url": "L", "portrait_download_url": "P"}
    only_land = {"clip_id": "b", "landscape_download_url": "L", "portrait_download_url": None}
    assert pick_download(both, "portrait") == ("portrait", "P", False)
    assert pick_download(only_land, "portrait") == ("landscape", "L", True)
    assert pick_download(only_land, "landscape") == ("landscape", "L", False)
    with pytest.raises(ValueError):
        pick_download({"clip_id": "c", "landscape_download_url": None, "portrait_download_url": None})


# --- device code flow ---------------------------------------------------------
async def test_device_flow_pending_then_success():
    calls = {"token": 0}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/device"):
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://www.twitch.tv/activate",
                                             "expires_in": 1800, "interval": 5})
        if req.url.path.endswith("/token"):
            calls["token"] += 1
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            assert "client_secret" not in form  # public client
            if calls["token"] < 3:
                return httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 14400,
                                             "scope": ["user:read:chat"], "token_type": "bearer"})
        return httpx.Response(404)

    async def no_sleep(_):
        return None

    auth = DeviceAuth("cid", transport=httpx.MockTransport(handler))
    try:
        code = await auth.start(["user:read:chat"])
        tok = await auth.poll(code, ["user:read:chat"], sleep=no_sleep)
    finally:
        await auth.aclose()
    assert tok.access_token == "at" and tok.refresh_token == "rt" and tok.scopes == ["user:read:chat"]
    assert calls["token"] == 3


# --- EventSub WebSocket against a fake local server -------------------------
def _msg(mtype, payload, mid):
    return json.dumps({"metadata": {"message_id": mid, "message_type": mtype,
                                    "message_timestamp": "2026-11-20T12:00:00.000Z"}, "payload": payload})


async def test_eventsub_welcome_notifications_dedupe_and_planned_reconnect():
    port_holder = {}

    async def server(ws):
        path = ws.request.path
        if "second" in path:
            await ws.send(_msg("session_welcome", {"session": {"id": "s2", "keepalive_timeout_seconds": 10}}, "w2"))
            await ws.send(_msg("notification", {"event": {"message": {"text": "after"}}}, "n3"))
            await asyncio.sleep(5)
            return
        await ws.send(_msg("session_welcome", {"session": {"id": "s1", "keepalive_timeout_seconds": 10}}, "w1"))
        await ws.send(_msg("session_keepalive", {}, "k1"))
        await ws.send(_msg("notification", {"event": {"message": {"text": "hi"}}}, "n1"))
        await ws.send(_msg("notification", {"event": {"message": {"text": "hi"}}}, "n1"))  # duplicate
        await ws.send(_msg("notification", {"event": {"message": {"text": "two"}}}, "n2"))
        await ws.send(_msg("session_reconnect", {"session": {"id": "s1", "reconnect_url": f"ws://127.0.0.1:{port_holder['p']}/second"}}, "r1"))
        await asyncio.sleep(5)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port_holder["p"] = srv.sockets[0].getsockname()[1]
        welcomes, texts = [], []
        stop = asyncio.Event()

        async def on_welcome(sid, survive):
            welcomes.append((sid, survive))

        async def on_note(meta, payload):
            texts.append(payload["event"]["message"]["text"])
            if len(texts) == 3:
                stop.set()

        client = EventSubClient(on_welcome, on_note, url=f"ws://127.0.0.1:{port_holder['p']}/first", keepalive_s=None)
        await asyncio.wait_for(client.run(stop), timeout=10)

    assert welcomes == [("s1", False), ("s2", True)]   # subscriptions survive a planned reconnect
    assert texts == ["hi", "two", "after"]
    assert client.stats.duplicates == 1 and client.stats.keepalives == 1
    assert client.stats.reconnects_planned == 1 and client.stats.reconnects_unplanned == 0


async def test_eventsub_keepalive_timeout_records_gap_and_resubscribes():
    count = {"n": 0}

    async def server(ws):
        count["n"] += 1
        await ws.send(_msg("session_welcome", {"session": {"id": f"s{count['n']}", "keepalive_timeout_seconds": 1}}, f"w{count['n']}"))
        await asyncio.sleep(10)  # go silent -> client must time out

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        welcomes = []
        stop = asyncio.Event()

        async def on_welcome(sid, survive):
            welcomes.append((sid, survive))
            if len(welcomes) == 2:
                stop.set()

        async def on_note(meta, payload):
            pass

        client = EventSubClient(on_welcome, on_note, url=f"ws://127.0.0.1:{port}/", keepalive_s=None, grace_s=0.5)
        await asyncio.wait_for(client.run(stop), timeout=15)

    assert welcomes == [("s1", False), ("s2", False)]  # new session: must resubscribe
    assert client.stats.reconnects_unplanned == 1
    assert len(client.stats.gaps) == 1 and client.stats.gaps[0].end is not None
