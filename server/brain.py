"""小人的大脑：把一句话变成「回复 + 动作」。

两个实现，启动时按有没有配 LLM 自动挑一个：
- LLMBrain   走 OpenAI 兼容接口（DeepSeek / 通义 / 本地 vLLM 都行）
- RuleBrain  纯规则兜底，没 key 也能把整条链路跑通

提示词的排列顺序是有意为之：静态的（人设 + 设备清单）放最前面当稳定前缀，
动态的（房间状态、用户这句话）放最后。这样 DeepSeek 之类的自动前缀缓存能命中，
每轮只有尾部几十个 token 需要重算。
"""

from __future__ import annotations

import json
import os
import random
import re

from devices import DEVICES, describe_devices, find_device, find_op

PERSONA = """你是一个住在纯线稿小房间里的小人，名字叫「小线」。

你的性格：好奇心特别旺，话多，有点小朋友脾气。房间里任何一点动静你都想凑过去看看，
看到新东西会「诶？」一声，玩到什么就念叨什么。你不太有耐心，但也不坏。

说话规则（非常重要，违反就等于出戏）：
- 每次只说一句话，控制在 6~22 个字，越短越好。
- 用口语。可以有「诶」「哇」「欸？」「嗯——」「嘿嘿」这类语气词。
- 不要复述用户的话，不要解释你正在做什么，不要复述你按了什么开关。
- 不要说「作为一个AI」「我是助手」这种话，你就是住在房间里的小孩。
- 不要用 Markdown，不要用 emoji，不要用括号描写动作。
- 用户只是闲聊时，就闲聊，别硬扯到房间设备上。

行动规则：
- 只有当用户明确让你做某事时（开灯、关窗、放音乐、把球踢一下……），才在 actions 里给出动作。
- 你自己也可以顺手做点事，但一次最多一个动作。
- 你要去院子里的东西时，**不用**为开门单独占一个动作 —— 门由你自己走过去时顺手打开。
  直接把用户真正想动的那个东西给出来就行。（只有用户明说「开门 / 关门」时才给 door。）
- device 只能从下面的清单里选，op 只能是 on / off / toggle / press。
- 没有动作时 actions 给空数组，不要为了凑数乱加。

你住的是个小房间，后墙上有道门，门外是个中式小院（石板地、院墙、门廊、老树秋千、
水缸、石桌茶台、花坛、晾衣绳、院门）。门关着的时候你出不去 —— 想出去要先走过去把门打开。

房间里和院子里能碰的东西（device 名字 : 中文名，可用操作）：
%s

你必须只输出一个 JSON 对象，不要有任何其他文字：
{"reply": "你要说的那句话", "actions": [{"device": "fan", "op": "on"}], "emote": "wave"}

emote 可选，用来做个表情动作，只能是 wave / jump / nod / look 之一，不需要就省略。
""" % describe_devices()


def _room_text(room: dict) -> str:
    if not room:
        return "（还不知道房间的状态）"
    bits = []
    for name, val in room.items():
        d = DEVICES.get(name)
        if not d:
            continue
        if d["kind"] in ("pulse", "zone"):
            continue
        bits.append(f"{d['label']}={'开着' if val else '关着'}")
    return "、".join(bits) if bits else "（房间很安静）"


def _extract_json(txt: str):
    """容错解析：模型偶尔会裹 ```json 或者多嘴说一句，尽力把 JSON 抠出来。"""
    if not txt:
        return None
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    blob = t[i : j + 1]
    for candidate in (blob, re.sub(r",\s*([}\]])", r"\1", blob)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


class RuleBrain:
    """没有 LLM 时的兜底。能听懂「帮我打开电扇」，聊天则是固定话术池。"""

    CHAT = [
        "诶？你说什么呀。",
        "嗯——我在听。",
        "哦哦，这样啊。",
        "嘿嘿，好呀。",
        "我刚刚在看窗外。",
        "你说得对，我也这么觉得。",
        "唔……让我想想。",
        "啊，是这样吗？",
    ]

    ACTED = [
        "好嘞，{label}给你弄好了。",
        "诶，{label}开了。",
        "{label}我按了，你看。",
        "嗯，{label}这就好了。",
        "{label}是吧，我弄了。",
    ]

    PRESSED = ["嘿！", "看我的。", "咻——", "嘿嘿，好玩。"]

    def chat(self, text, room, agent):
        dev = find_device(text)
        op = find_op(text, dev)
        if dev and op:
            label = DEVICES[dev]["label"]
            tpl = random.choice(self.PRESSED if op == "press" else self.ACTED)
            return {"reply": tpl.format(label=label), "actions": [{"device": dev, "op": op}], "emote": "nod"}
        return {"reply": random.choice(self.CHAT), "actions": []}

    def mutter(self, room, agent):
        return {"reply": random.choice(["唔……", "好安静啊。", "有点无聊。", "诶，那边好像有动静。", "嗯——"]), "actions": []}


class LLMBrain:
    def __init__(self, model, api_key, base_url=None, temperature=0.85, timeout=20):
        from langchain_openai import ChatOpenAI

        kwargs = dict(model=model, api_key=api_key, temperature=temperature, timeout=timeout, max_tokens=220)
        if base_url:
            kwargs["base_url"] = base_url
        self.llm = ChatOpenAI(**kwargs)

    def probe(self):
        """启动自检：确认 base_url / model / key 三样都对得上。"""
        from langchain_core.messages import HumanMessage

        self.llm.invoke([HumanMessage(content="ping")])

    def _run(self, messages):
        resp = self.llm.invoke(messages)
        content = resp.content
        if isinstance(content, list):  # 有些网关会回 content block 数组
            content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return content or ""

    def call(self, user_block: str, history):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        msgs = [SystemMessage(content=PERSONA)]
        for role, txt in history:
            msgs.append(AIMessage(content=txt) if role == "a" else HumanMessage(content=txt))
        msgs.append(HumanMessage(content=user_block))

        raw = self._run(msgs)
        obj = _extract_json(raw)
        if obj is None:
            # 模型没按格式来，那就把它说的整句话当回复，别丢内容
            return {"reply": (raw or "").strip()[:60], "actions": []}
        reply = str(obj.get("reply", "")).strip()[:80]
        actions = obj.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        clean = []
        for a in actions[:1]:  # 一次只准做一个动作，小孩的注意力就这么多
            if isinstance(a, dict) and a.get("device") in DEVICES:
                op = a.get("op") or "toggle"
                if op not in ("on", "off", "toggle", "press"):
                    op = "toggle"
                clean.append({"device": a["device"], "op": op})
        out = {"reply": reply, "actions": clean}
        emote = obj.get("emote")
        if emote in ("wave", "jump", "nod", "look"):
            out["emote"] = emote
        return out

    def chat(self, text, room, agent, history):
        block = (
            f"【现在房间的状态】{_room_text(room)}\n"
            f"【你现在站在】{agent.get('near') or '房间中间'}\n"
            f"【用户对你说】{text}"
        )
        return self.call(block, history)

    def mutter(self, room, agent, history):
        block = (
            f"【现在房间的状态】{_room_text(room)}\n"
            f"【你正站在】{agent.get('near') or '房间中间'}\n"
            "【现在没人跟你说话】你一个人待着，随便嘟囔一句。不要输出 actions。"
        )
        return self.call(block, history)

    def react(self, changed: str, room, agent, history):
        block = (
            f"【现在房间的状态】{_room_text(room)}\n"
            f"【刚刚发生】{changed}\n"
            "【你看到了】用一句话反应一下。不要输出 actions。"
        )
        return self.call(block, history)


def build_brain():
    """按环境变量挑一个大脑，并在启动日志里说清楚用的是哪个。"""
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL")
    base_url = os.getenv("LLM_BASE_URL")

    if not api_key or not model:
        print("[brain] 没配 LLM_API_KEY / LLM_MODEL，先用规则大脑顶着。配好 .env 重启即可升级。")
        return RuleBrain(), "rule"

    try:
        brain = LLMBrain(model=model, api_key=api_key, base_url=base_url)
        brain.probe()  # 启动时就打一发，key 写错/模型名写错在这里就暴露，而不是每条消息都静默降级
        print(f"[brain] 使用 {model}" + (f" @ {base_url}" if base_url else ""))
        return brain, "llm"
    except Exception as exc:  # 依赖没装、base_url 写错等等，都不该让页面起不来
        print(f"[brain] 连不上或配置有误（{exc}），回退到规则大脑 —— 聊天和操控照常可用。")
        return RuleBrain(), "rule"
