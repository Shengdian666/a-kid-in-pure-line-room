"""房间里可以被小人操控的东西。

这份清单是前后端的唯一契约：前端 room.html 里的 DEV 表用的是同一批名字，
后端只会下发这里出现过的 device / op，多一个都会被 validate 节点丢掉。
"""

DEVICES = {
    "lightSwitch": {
        "label": "电灯开关",
        "aliases": ["电灯开关", "大灯的开关", "灯的开关", "开灯", "关灯", "电灯", "大灯", "灯"],
        "kind": "toggle",
    },
    "lamp": {
        "label": "台灯",
        "aliases": ["台灯", "书桌灯", "桌上的灯", "小灯"],
        "kind": "toggle",
    },
    "fan": {
        "label": "吊扇",
        "aliases": ["吊扇", "电扇", "风扇", "扇子", "转的那个扇"],
        "kind": "toggle",
    },
    "window": {
        "label": "窗户",
        "aliases": ["窗户", "窗", "开窗", "关窗", "透气"],
        "kind": "toggle",
    },
    "blinds": {
        "label": "百叶帘",
        "aliases": ["百叶帘", "窗帘", "帘子", "百叶", "遮光"],
        "kind": "toggle",
    },
    "door": {
        "label": "房门",
        "aliases": ["房门", "门", "关门", "开门"],
        "kind": "toggle",
    },
    "drawer": {
        "label": "抽屉",
        "aliases": ["抽屉"],
        "kind": "toggle",
    },
    "cabinet": {
        "label": "书柜的柜门",
        "aliases": ["柜门", "书柜门", "柜子", "书柜"],
        "kind": "toggle",
    },
    "book": {
        "label": "书柜上的书",
        "aliases": ["一本书", "那本书", "书", "翻书", "拿书"],
        "kind": "toggle",
    },
    "clock": {
        "label": "挂钟的钟摆",
        "aliases": ["钟摆", "挂钟", "时钟", "钟"],
        "kind": "toggle",
    },
    "record": {
        "label": "黑胶唱机",
        "aliases": ["黑胶唱机", "唱机", "唱片", "黑胶", "音乐", "放歌", "放首歌", "听歌"],
        "kind": "toggle",
    },
    "picture": {
        "label": "墙上的挂画",
        "aliases": ["挂画", "相框", "画"],
        "kind": "toggle",
    },
    "globe": {
        "label": "地球仪",
        "aliases": ["地球仪", "地球"],
        "kind": "toggle",
    },
    "mobile": {
        "label": "悬挂的风铃",
        "aliases": ["风铃", "挂饰", "吊饰", "风铃挂件"],
        "kind": "toggle",
    },
    "plant": {
        "label": "盆栽",
        "aliases": ["盆栽", "植物", "绿植", "花"],
        "kind": "toggle",
    },
    "mug": {
        "label": "马克杯",
        "aliases": ["马克杯", "杯子", "咖啡", "热茶", "水杯"],
        "kind": "toggle",
    },
    "chair": {
        "label": "椅子",
        "aliases": ["椅子", "凳子", "座位"],
        "kind": "toggle",
    },
    "sofa": {
        "label": "沙发上的抱枕",
        "aliases": ["抱枕", "枕头", "沙发"],
        "kind": "pulse",
    },
    "ball": {
        "label": "地板上的球",
        "aliases": ["皮球", "球"],
        "kind": "pulse",
    },
}

OPS = {"on", "off", "toggle", "press"}

# 判定动词时按长度从长到短扫，避免「关上」被「上」这类短词抢先命中
ON_WORDS = [
    "点亮", "打开", "开启", "启动", "开一下", "拉上去", "拉起来", "放点", "放首", "来点", "播放",
    "转一下", "转起来", "转转", "翻一下", "翻翻", "翻到", "吹一下", "吹吹", "晃一下", "摇一下",
    "放", "开", "亮", "播", "转", "翻", "吹", "晃",
]
OFF_WORDS = ["关上", "关掉", "关闭", "放下来", "拉下来", "别放", "不要放", "停一下", "停下", "别", "停", "灭", "收", "关"]
PRESS_WORDS = ["踢一下", "踢一", "拍一下", "拍一", "打一下", "按一下", "碰一下", "踢", "拍", "按", "碰"]
TOGGLE_WORDS = ["弄一下", "搞一下", "动一下", "试试"]


def describe_devices() -> str:
    """给 LLM 看的静态设备清单。这段进 system prompt，保持稳定以便命中前缀缓存。"""
    lines = []
    for name, d in DEVICES.items():
        act = "可以 press（拍一下/踢一下，一次性的）" if d["kind"] == "pulse" else "可以用 on / off / toggle"
        lines.append(f"- {name}：{d['label']}，{act}")
    return "\n".join(lines)


def find_device(text: str) -> str | None:
    """按「最长的别名优先」从用户的话里找出他说的东西。"""
    best, best_len = None, 0
    for name, d in DEVICES.items():
        for a in d["aliases"]:
            if a in text and len(a) > best_len:
                best, best_len = name, len(a)
    return best


def find_op(text: str, device: str | None) -> str | None:
    """找出用户想干什么。找不到明确动词就返回 None（交给 LLM 判断）。"""
    if device and DEVICES[device]["kind"] == "pulse":
        for w in PRESS_WORDS:
            if w in text:
                return "press"
        return "press"
    for w in OFF_WORDS:
        if w in text:
            return "off"
    for w in ON_WORDS:
        if w in text:
            return "on"
    for w in PRESS_WORDS:
        if w in text:
            return "press"
    for w in TOGGLE_WORDS:
        if w in text:
            return "toggle"
    return None


def valid(name, op) -> bool:
    return name in DEVICES and op in OPS
