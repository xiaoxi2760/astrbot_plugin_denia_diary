"""日记**格式契约**（第 1 步）。

头行是整个日记本唯一的定界符：:

    2026-09-23 03:45（喜欢）〔和某某〕      ← 头行：日期 时间（心情）〔标注〕
    正文若干行
                                            ← 空行 = 段间隔

本模块只做**纯文本手术**：不做文件 IO、不读配置、不认识平台对象。

⚠️ 三条不变式（原包实测踩过，改这里前先读 `docs` 里 P1-b 的复盘）：

1. **写入侧与解析侧共用 ``HEAD_RE`` 与 ``TAG_MAX``**——标注写超长会让头行失配，
   整段从读/列/按天里消失（还会被上一段吞掉）；
2. **头行是历史，不可改**——编辑只换正文；
3. **文件必须以换行结尾**——否则下一次追加的头行会粘在上一段末尾，同样导致失配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%H:%M"

HEAD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})"
    r"(?:（([^）\n]{0,60})）)?"
    r"(?:〔([^〕\n]{0,40})〕)?$"
)
TAG_MAX = 40
"""标注长度上限，**必须与 ``HEAD_RE`` 里的 ``{0,40}`` 一致**。"""

MOOD_MAX = 12
GROUP_TAG_PREFIX = "群·"
"""群聊标注前缀（显式 scope 标记，v0.3 新增）。

原包靠"标注以『和』开头"猜群/私聊，把"和平精英交流群"这类群名误判成私聊；
现在群聊一律写成 ``群·群名``。**读取时兼容旧写法**（见 ``scope_of``）。
"""

NORMAL = "normal"
LOVE = "love"
BOOKS = (NORMAL, LOVE)

MOOD_WORDS = (
    "开心", "喜欢", "骄傲", "平静", "期待", "感动",
    "委屈", "难过", "孤单", "担心", "焦虑", "害怕",
    "生气", "羞耻", "尴尬", "害羞",
)
"""建议心情词（写进工具描述，引导她用标准词）。

第 3 步会把自由词映射成 5 档情绪，心情暗号依赖"最低档"可判定。
"""


# ---- 时间 --------------------------------------------------------------------


def day_of(when: datetime) -> str:
    return when.strftime(DATE_FMT)


def clock_of(when: datetime) -> str:
    return when.strftime(TIME_FMT)


def parse_stamp(date: str, time: str, tz: tzinfo) -> datetime | None:
    """把 ``日期 时间`` 解析成带时区的时刻；解析不了返回 ``None``（调用方按"不动它"处理）。"""
    try:
        naive = datetime.strptime(f"{date} {time}", f"{DATE_FMT} {TIME_FMT}")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=tz)


# ---- 头行 --------------------------------------------------------------------


def sanitize_mood(mood: object) -> str:
    return re.sub(r"[（）\r\n]", " ", str(mood or "")).strip()[:MOOD_MAX]


def sanitize_tag(tag: object) -> str:
    """与 ``HEAD_RE`` 同源地收敛标注：去掉 ``〕`` 与换行，再截断到 ``TAG_MAX``。"""
    text = re.sub(r"[〕\r\n]", " ", str(tag or "")).strip()
    return text[:TAG_MAX]


def make_head_line(date: str, time: str, mood: object = "", tag: object = "") -> str:
    """唯一允许用来生成头行的函数。"""
    clean_mood = sanitize_mood(mood)
    clean_tag = sanitize_tag(tag)
    mood_part = f"（{clean_mood}）" if clean_mood else ""
    tag_part = f"〔{clean_tag}〕" if clean_tag else ""
    return f"{date} {time}{mood_part}{tag_part}"


def scope_of(tag: object) -> str:
    """标注属于群聊还是私聊：``"group"`` / ``"private"`` / ``""``（旧条目没标注）。

    新写法精确（``群·`` 前缀）；旧写法退回启发式（以「和」开头 = 私聊）。
    """
    text = str(tag or "").strip()
    if not text:
        return ""
    if text.startswith(GROUP_TAG_PREFIX):
        return "group"
    if text.startswith("和"):
        return "private"
    if "私聊" in text:
        return "private"
    return "group"


# ---- 段与条目 ----------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """一段（头行 + 它的正文），用**行下标**定位，便于按行局部替换。"""

    head_line: str
    head_idx: int
    end_idx: int
    date: str
    time: str
    mood: str
    who: str
    text: str

    @property
    def stamp(self) -> str:
        return f"{self.date} {self.time}"

    @property
    def scope(self) -> str:
        return scope_of(self.who)

    def when(self, tz: tzinfo) -> datetime | None:
        return parse_stamp(self.date, self.time, tz)


@dataclass(frozen=True)
class Entry:
    """读出来的一条（含它属于哪一本）。"""

    date: str
    time: str
    mood: str
    who: str
    text: str
    book: str = NORMAL

    @property
    def stamp(self) -> str:
        return f"{self.date} {self.time}"

    @property
    def scope(self) -> str:
        return scope_of(self.who)

    def when(self, tz: tzinfo) -> datetime | None:
        return parse_stamp(self.date, self.time, tz)


@dataclass(frozen=True)
class EditResult:
    """改写/删除的结果。``ok=False`` 时 ``error`` 是人话原因。"""

    ok: bool
    text: str = ""
    seg: Segment | None = None
    before: str = ""
    error: str = ""


def split_segments(text: str) -> list[Segment]:
    """整份文本 → 段列表（``end_idx`` 是下一段头行的前一行）。"""
    lines = str(text or "").split("\n")
    heads: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = HEAD_RE.match(line.strip())
        if match:
            heads.append((index, match))

    segments: list[Segment] = []
    for order, (row, match) in enumerate(heads):
        end_idx = (heads[order + 1][0] if order + 1 < len(heads) else len(lines)) - 1
        body = "\n".join(lines[row + 1 : end_idx + 1]).strip()
        segments.append(
            Segment(
                head_line=lines[row],
                head_idx=row,
                end_idx=end_idx,
                date=match.group(1),
                time=match.group(2),
                mood=match.group(3) or "",
                who=match.group(4) or "",
                text=body,
            )
        )
    return segments


def parse_entries(text: str, book: str = NORMAL) -> list[Entry]:
    """整份文本 → 条目列表（丢掉既没正文也没心情的空段）。"""
    out = [
        Entry(date=s.date, time=s.time, mood=s.mood, who=s.who, text=s.text, book=book)
        for s in split_segments(text)
    ]
    return [e for e in out if e.text or e.mood]


def keep_trailing_newlines(joined: str, original: str) -> str:
    """保住原文的结尾换行。

    原包的实伤：``join`` 会吃掉结尾换行 → 下次追加时新头行粘在上一段正文末尾
    → 头行不在行首 → 整段解析不出来（表现为"内容在、段数不涨"）。
    先把结果末尾换行清掉，再补回原文那一串；原文没有就补 ``\\n\\n``。
    """
    match = re.search(r"\n+$", str(original or ""))
    tail = match.group(0) if match else "\n\n"
    return re.sub(r"\n+$", "", str(joined or "")) + tail


def within_window(
    seg: Segment, now: datetime, within_days: int, tz: tzinfo
) -> bool:
    """段是否落在"允许改"的时间窗内（含未来容差，避免把未来段说成"太旧"）。"""
    moment = seg.when(tz)
    if moment is None:
        return False
    delta = now - moment
    if delta < -timedelta(minutes=1):
        return False
    return delta <= timedelta(days=within_days)


def find_segment(
    segments: list[Segment],
    match: str,
    now: datetime,
    within_days: int,
    tz: tzinfo,
) -> tuple[Segment | None, str]:
    """按 ``match`` 找段（正文或头行包含即可；不传 = 最后一段）。返回 ``(段, 错误)``。"""
    if not segments:
        return None, "这本里还没有段落"
    frag = str(match or "").strip()
    if frag:
        found = None
        for seg in reversed(segments):  # 从新到旧找：她多半要改刚写的
            if frag in seg.text or frag in seg.head_line:
                found = seg
                break
        if found is None:
            tail = "；".join(
                f"{s.date} {s.time}｜{s.text[:20]}" for s in segments[-5:]
            )
            return None, f"没找到含「{frag}」的段落。最近几段是：{tail}"
        seg = found
    else:
        seg = segments[-1]
    if not within_window(seg, now, within_days, tz):
        return None, (
            f"{seg.stamp} 那段太旧了（只能改最近 {within_days} 天的）——老日记是历史"
        )
    return seg, ""


def rewrite_segment(
    text: str,
    *,
    match: str = "",
    new_body: str = "",
    now: datetime,
    within_days: int,
    tz: tzinfo,
) -> EditResult:
    """重写某一段的**正文**（头行原样保留）。"""
    segments = split_segments(text)
    seg, error = find_segment(segments, match, now, within_days, tz)
    if seg is None:
        return EditResult(ok=False, error=error)
    body = str(new_body or "").strip()
    if not body:
        return EditResult(
            ok=False, error="新正文不能为空（想删掉这段就用 delete）"
        )
    lines = str(text or "").split("\n")
    out = lines[: seg.head_idx + 1] + body.split("\n") + lines[seg.end_idx + 1 :]
    return EditResult(
        ok=True,
        text=keep_trailing_newlines("\n".join(out), text),
        seg=seg,
        before=seg.text,
    )


def delete_segment(
    text: str, *, match: str = "", now: datetime, within_days: int, tz: tzinfo
) -> EditResult:
    """删掉某一段（含头行），并把它留下的连续空行收成一个。"""
    segments = split_segments(text)
    seg, error = find_segment(segments, match, now, within_days, tz)
    if seg is None:
        return EditResult(ok=False, error=error)
    lines = str(text or "").split("\n")
    start = seg.head_idx
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    end = seg.end_idx
    while end + 1 < len(lines) and not lines[end + 1].strip():
        end += 1
    joined = "\n".join(lines[:start] + lines[end + 1 :])
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return EditResult(
        ok=True,
        text=keep_trailing_newlines(joined, text),
        seg=seg,
        before="\n".join(lines[seg.head_idx : seg.end_idx + 1]),
    )


# ---- 渲染（读出来给她看的文本） ------------------------------------------------


def normalize_body(raw: object, max_chars: int) -> str:
    """正文落盘前的规整。

    - 字面 ``\\n``（反斜杠 + n）还原成换行——LLM 经常这么打；
    - 段落之间统一空一行，否则读出来挤成一坨；
    - 超长按 ``max_chars`` 截断（上限来自配置）。
    """
    text = str(raw or "").strip()
    text = text.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    text = text[:max_chars]
    parts = [part.strip() for part in re.split(r"\n+", text)]
    return "\n\n".join(part for part in parts if part)


def render(entries: list[Entry], *, mark_love: bool = False, footer: str = "") -> str:
    """条目 → 文本。``mark_love`` 只影响**读出来的文本**，绝不写进文件。"""
    blocks = []
    for entry in entries:
        mark = "❤️ " if mark_love and entry.book == LOVE else ""
        mood = f" · {entry.mood}" if entry.mood else ""
        tag = f"〔{entry.who}〕" if entry.who else ""
        blocks.append(f"{mark}【{entry.date} {entry.time}{mood}】{tag}\n{entry.text}")
    body = "\n\n".join(blocks)
    return body + footer


__all__ = [
    "BOOKS",
    "DATE_FMT",
    "GROUP_TAG_PREFIX",
    "HEAD_RE",
    "LOVE",
    "MOOD_MAX",
    "MOOD_WORDS",
    "NORMAL",
    "TAG_MAX",
    "TIME_FMT",
    "EditResult",
    "Entry",
    "Segment",
    "clock_of",
    "day_of",
    "delete_segment",
    "find_segment",
    "keep_trailing_newlines",
    "make_head_line",
    "normalize_body",
    "parse_entries",
    "parse_stamp",
    "render",
    "rewrite_segment",
    "sanitize_mood",
    "sanitize_tag",
    "scope_of",
    "split_segments",
    "within_window",
]
