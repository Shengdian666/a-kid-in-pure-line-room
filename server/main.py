"""HTTP 服务：既托管房间页面，也托管小人的大脑。

因为前端用的是轮询，这里完全无状态化的做法就够了 —— 一个小人配一个浏览器，
所以直接开一个全局 Session，不需要 session id、不需要 cookie、不需要连接管理。

  GET  /                -> room.html
  GET  /events?since=N  -> 小人自主活动的待办事件（含游标）
  POST /chat            -> 用户说一句话，同步拿回回复 + 动作
  POST /state           -> 浏览器汇报房间设备状态和小人位置

启动：  cd server && uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).with_name(".env"))

ROOT = Path(__file__).resolve().parent.parent
TICK_SEC = float(os.getenv("IDLE_TICK_SEC", "7"))     # 自主活动的检查间隔
IDLE_GAP = float(os.getenv("IDLE_GAP_SEC", "10"))     # 用户刚说过话就先安静一会儿
WATCH_GAP = 25.0                                      # 页面没在轮询就别自说自话


class Session:
    def __init__(self):
        self.events: list[dict] = []
        self.cursor = 0
        self.room: dict = {}
        self.agent: dict = {}
        self.prev_room: dict = {}
        self.last_user = 0.0
        self.last_poll = 0.0

    def push(self, ev: dict):
        self.cursor += 1
        ev = dict(ev, seq=self.cursor)
        self.events.append(ev)
        if len(self.events) > 300:
            self.events = self.events[-200:]

    def changed_label(self) -> str:
        """跟上一轮比，哪些东西被用户改变了。这个直接决定小人会不会「诶？」一声。"""
        if not self.prev_room:
            return ""
        from devices import DEVICES

        for name, val in self.room.items():
            if name not in DEVICES or DEVICES[name]["kind"] == "pulse":
                continue
            if self.prev_room.get(name) != val:
                return f"{name}：{'开着' if val else '关着'}"
        return ""


SESSION = Session()


async def _autonomous_loop():
    """小人的自主活动。放在后台任务里，和用户请求完全解耦。"""
    from agent import run_tick

    while True:
        await asyncio.sleep(TICK_SEC)
        now = time.time()
        if now - SESSION.last_poll > WATCH_GAP:
            continue  # 没人开着页面，别空转
        if now - SESSION.last_user < IDLE_GAP:
            continue  # 用户正在聊天，让它先听人说话
        try:
            events = await asyncio.to_thread(run_tick, SESSION.room, SESSION.agent, SESSION.changed_label())
            for ev in events:
                SESSION.push(ev)
        except Exception as exc:
            print(f"[tick] 自主活动出错：{exc}")
        SESSION.prev_room = dict(SESSION.room)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_autonomous_loop())
    print(f"[room] 房间已就绪 → http://127.0.0.1:8000   (自主活动间隔 {TICK_SEC}s)")
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan, title="pure-line-room agent")


@app.get("/events")
async def events(since: int = 0):
    SESSION.last_poll = time.time()
    fresh = [e for e in SESSION.events if e["seq"] > since]
    return {"cursor": SESSION.cursor, "events": fresh}


@app.post("/chat")
async def chat(payload: dict):
    from agent import run_chat

    text = (payload.get("text") or "").strip()[:400]
    if not text:
        return {"reply": "", "actions": []}

    SESSION.last_user = time.time()
    try:
        out = await asyncio.to_thread(run_chat, text, SESSION.room, SESSION.agent)
    except Exception as exc:
        print(f"[chat] 出错：{exc}")
        return JSONResponse({"reply": "唔……我脑子卡住了。", "actions": []}, status_code=200)
    return out


@app.post("/state")
async def state(payload: dict):
    SESSION.last_poll = time.time()
    if isinstance(payload.get("room"), dict):
        SESSION.room = payload["room"]
    if isinstance(payload.get("agent"), dict):
        SESSION.agent = payload["agent"]
    return {"ok": True}


@app.get("/health")
async def health():
    from agent import BRAIN_KIND

    return {"ok": True, "brain": BRAIN_KIND, "room": SESSION.room}


@app.get("/")
@app.get("/room.html")
async def index():
    return FileResponse(ROOT / "room.html")


@app.get("/audio.js")
async def audio_js():
    return FileResponse(ROOT / "audio.js", media_type="application/javascript")


# 只放行页面真正需要的目录。不要把整个 ROOT 挂成静态目录 ——
# 那样 server/.env（里面有你的 API key）和所有 .py 都会被浏览器直接下载。
app.mount("/sounds", StaticFiles(directory=str(ROOT / "sounds")), name="sounds")
