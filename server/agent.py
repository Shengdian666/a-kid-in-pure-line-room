"""小人的行为图（LangGraph）。

图有两个入口，走完最后都汇到同一个 validate 节点：

    (mode=chat)  read_intent ─→ chat_think ─────────────┐
    (mode=tick)  tick_decide ─→ tick_speak ─→ validate ─→ END
                       └──────────────────────↗

- chat 分支：用户发了消息，走 LLM 出一条回复 + 至多一个动作
- tick 分支：没人说话时的自主活动，决定「溜达到哪 / 顺手按点什么 / 嘟囔一句 / 发呆」

自主活动刻意不交给 LLM 做主：每几秒一次 LLM 调用既慢又贵，而且模型抽风会让小人乱抖。
这里让它做规则化的加权选择，只在「嘟囔」和「对变化做反应」这两个点才叫 LLM。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph

from brain import build_brain
from devices import DEVICES, valid

BRAIN, BRAIN_KIND = build_brain()

HISTORY: deque = deque(maxlen=12)  # (role, text)，只记聊天，不记自主行为
_lock = threading.Lock()
_last_autosay = 0.0

# 小人自己爱鼓捣的东西，权重高的是它会主动去碰的
TOYING = ["fan", "record", "globe", "mobile", "blinds", "lamp", "lightSwitch", "ball", "plant", "book"]
AUTOSAY_GAP = 45.0  # 自主说话的最小间隔（秒），不然话多的小孩会烦死人


class State(TypedDict, total=False):
    mode: Literal["chat", "tick"]
    text: str
    room: dict
    agent: dict
    changed: str
    intent: dict
    plan: dict
    reply: str
    actions: list
    emote: str
    events: list


# ---------------------------------------------------------------- chat 分支

def read_intent(state: State) -> State:
    """先用规则扫一遍用户的话，把「说的什么东西 + 想干嘛」标出来。

    这一步不是在抢 LLM 的活 —— 它只是给 LLM 一个明确提示，让「帮我打开电扇」
    这种直球指令不依赖模型的 function calling 水平也能稳定命中。
    """
    from devices import find_device, find_op

    text = state.get("text", "")
    dev = find_device(text)
    op = find_op(text, dev)
    state["intent"] = {"device": dev, "op": op, "is_command": bool(dev and op)}
    return state


def chat_think(state: State) -> State:
    text = state.get("text", "")
    room = state.get("room") or {}
    agent = state.get("agent") or {}

    with _lock:
        history = list(HISTORY)

    try:
        out = BRAIN.chat(text, room, agent, history) if BRAIN_KIND == "llm" else BRAIN.chat(text, room, agent)
    except Exception as exc:
        print(f"[agent] LLM 调用失败：{exc}")
        out = {"reply": "唔……我走神了，你再说一遍？", "actions": []}

    intent = state.get("intent") or {}
    # 规则认出了明确指令、模型却没给动作 → 补上。模型漏掉的直球指令对体验伤害最大。
    if intent.get("is_command") and not out.get("actions"):
        out["actions"] = [{"device": intent["device"], "op": intent["op"]}]

    state["reply"] = out.get("reply") or "……"
    state["actions"] = out.get("actions") or []
    if out.get("emote"):
        state["emote"] = out["emote"]

    with _lock:
        HISTORY.append(("u", text))
        HISTORY.append(("a", state["reply"]))
    return state


# ---------------------------------------------------------------- tick 分支

def tick_decide(state: State) -> State:
    room = state.get("room") or {}
    hour = time.localtime().tm_hour
    night = hour >= 19 or hour < 7

    choices: list[tuple[str, int]] = []
    if state.get("changed"):
        choices.append(("react", 6))  # 用户刚动了什么，凑过去看看
    choices.append(("wander", 5))
    choices.append(("use", 3 if night else 4))
    choices.append(("say", 1 if night else 2))
    choices.append(("idle", 7 if night else 3))

    kinds = [k for k, w in choices for _ in range(w)]
    kind = random.choice(kinds)

    plan: dict[str, Any] = {"kind": kind}
    if kind == "wander":
        plan["target"] = random.choice(TOYING)
    elif kind == "use":
        # 优先挑「状态和小人预期不一致」的东西下手：关着的想去开，开着的想去关
        pool = list(TOYING)
        random.shuffle(pool)
        name = next((n for n in pool if not room.get(n)), random.choice(pool))
        plan["target"] = name
        plan["op"] = "press" if DEVICES[name]["kind"] == "pulse" else ("on" if not room.get(name) else "off")
    elif kind == "react":
        plan["changed"] = state.get("changed", "")

    state["plan"] = plan
    return state


def after_tick(state: State) -> str:
    plan = state.get("plan") or {}
    if plan.get("kind") in ("say", "react"):
        global _last_autosay
        if time.time() - _last_autosay >= AUTOSAY_GAP or plan.get("kind") == "react":
            return "speak"
    return "done"


def tick_speak(state: State) -> State:
    global _last_autosay
    plan = state.get("plan") or {}
    room = state.get("room") or {}
    agent = state.get("agent") or {}

    with _lock:
        history = list(HISTORY)

    try:
        if BRAIN_KIND == "llm":
            if plan.get("kind") == "react":
                out = BRAIN.react(plan.get("changed", ""), room, agent, history)
            else:
                out = BRAIN.mutter(room, agent, history)
        else:
            out = BRAIN.mutter(room, agent)
    except Exception as exc:
        print(f"[agent] 自主说话失败：{exc}")
        return state

    text = (out or {}).get("reply", "").strip()
    if text:
        state["events"] = [{"type": "say", "text": text}]
        _last_autosay = time.time()
    return state


# ---------------------------------------------------------------- 汇合

def validate(state: State) -> State:
    """最后一道闸：白名单之外的东西一律丢掉，绝不下发给前端。"""
    actions = [a for a in (state.get("actions") or []) if valid(a.get("device"), a.get("op"))]
    state["actions"] = actions

    if state.get("mode") == "chat":
        # 用户主动说话时，响应走 HTTP 直接回去，不进轮询队列，免得前端重复执行一遍
        return state

    events = list(state.get("events") or [])
    plan = state.get("plan") or {}

    if plan.get("kind") == "wander" and plan.get("target"):
        events.append({"type": "go", "target": plan["target"]})
    elif plan.get("kind") == "use" and plan.get("target"):
        events.append({"type": "use", "device": plan["target"], "op": plan.get("op", "toggle")})
    elif plan.get("kind") == "react":
        target = (state.get("changed") or "").split("：")[0]
        if target in DEVICES:
            events.append({"type": "go", "target": target})

    if state.get("emote"):
        events.append({"type": "emote", "kind": state["emote"]})

    state["events"] = events
    return state


def _route(state: State) -> str:
    return "intent" if state.get("mode") == "chat" else "tick"


def _build():
    g = StateGraph(State)
    g.add_node("read_intent", read_intent)
    g.add_node("chat_think", chat_think)
    g.add_node("tick_decide", tick_decide)
    g.add_node("tick_speak", tick_speak)
    g.add_node("validate", validate)

    g.set_conditional_entry_point(_route, {"intent": "read_intent", "tick": "tick_decide"})
    g.add_edge("read_intent", "chat_think")
    g.add_edge("chat_think", "validate")
    g.add_conditional_edges("tick_decide", after_tick, {"speak": "tick_speak", "done": "validate"})
    g.add_edge("tick_speak", "validate")
    g.add_edge("validate", END)
    return g.compile()


GRAPH = _build()


def run_chat(text: str, room: dict, agent: dict) -> dict:
    out = GRAPH.invoke({"mode": "chat", "text": text, "room": room, "agent": agent})
    return {"reply": out.get("reply", ""), "actions": out.get("actions") or [], "emote": out.get("emote")}


def run_tick(room: dict, agent: dict, changed: str = "") -> list:
    out = GRAPH.invoke({"mode": "tick", "room": room, "agent": agent, "changed": changed, "events": []})
    return out.get("events") or []
