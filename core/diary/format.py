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

import hashlib
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


# ---- 段的身份（第 15 步）-------------------------------------------------------
#
# ``match``（正文里几个字）是**给 LLM 用**的模糊定位：她不知道偏移量，只能这么指。
# 面板是另一回事——它在 DOM 上点了某一条，那一条必须被**精确**指到，不能"最近一段包含
# 这几个字的"就算数（重名段会改错段，那比改不动更糟）。
#
# 所以段有**内容寻址的 id**：头行 + 正文一起哈希。选它而不是"行号 + 弱校验"的理由是
# **它同时是并发护栏**：文件被人手改过、或者上一条改删已经动过这一段，id 就对不上，
# 调用方据此报"这段已经变了，请刷新"，而不是照着旧内容盲改。


SEG_ID_LEN = 12
"""段 id 长度（十六进制字符数）。48 bit：十万段量级下碰撞概率已在 10⁻⁷ 以下，
而段数上千的本子远远用不到这个量级——够用，且短到能直接塞进 URL 与日志。"""


def seg_id(seg: Segment) -> str:
    """段的内容地址。**同一段每次算出来都一样**（只依赖头行与正文，不依赖行号）。"""
    raw = f"{seg.head_line}\n{seg.text}".encode("utf-8")
    return hashlib.blake2b(raw, digest_size=SEG_ID_LEN // 2).hexdigest()[:SEG_ID_LEN]


def find_by_id(segments: list[Segment], wanted: str) -> Segment | None:
    """按 ``seg_id`` 精确找段；找不到返回 ``None``（**不**退化成"最近一段"）。"""
    key = str(wanted or "").strip()
    if not key:
        return None
    for seg in segments:
        if seg_id(seg) == key:
            return seg
    return None


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


def already_happened(seg: Segment, now: datetime, tz: tzinfo) -> bool:
    """段的时间已经过了（含 1 分钟时钟容差）。

    **无论有没有豁免窗口都要过这一关**：面板能改任意一段，不等于能改一段"还没发生"的
    日记——未来段是时钟没同步对，不是历史。与 ``within_window`` 前半段同一判据，
    拆出来是为了让"不限时间"和"限 7 天"共用一条底线。
    """
    moment = seg.when(tz)
    if moment is None:
        return False
    return now - moment >= timedelta(minutes=-1)


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
    """重写某一段的**正文**（头行原样保留）。按 ``match`` 模糊定位。"""
    segments = split_segments(text)
    seg, error = find_segment(segments, match, now, within_days, tz)
    if seg is None:
        return EditResult(ok=False, error=error)
    if not str(new_body or "").strip():
        # 空正文在这里拦、用**工具自己的话**拦：``diary_edit`` 传的 action 关键字是
        # ``delete``，提示她"用删除"等于要她猜参数名。面板那句是"用删除"（按钮叫删除）。
        return EditResult(ok=False, error="新正文不能为空（想删掉这段就用 delete）")
    return rewrite_segment_at(text, seg, new_body)


def rewrite_segment_at(text: str, seg: Segment, new_body: str) -> EditResult:
    """重写**已经定位到的那一段**的正文（面板路径；不再找段、也不看时间窗）。

    头行原样保留——它是历史（``format`` 模块头的不变式 2），改只换正文。
    """
    body = str(new_body or "").strip()
    if not body:
        return EditResult(ok=False, error="新正文不能为空（想删掉这段就用删除）")
    lines = str(text or "").split("\n")
    tail = lines[seg.end_idx + 1 :]
    block = body.split("\n")
    # 段之间必须留一个空行（format 模块头的不变式 3）。正文整段替掉原范围时，原来那句
    # 尾随空行会被一起吃掉 —— 不补回去，下一段的头行就贴着本段正文，文件看着粘连了。
    if tail and not lines[seg.end_idx].strip():
        block = block + [""]
    out = lines[: seg.head_idx + 1] + block + tail
    return EditResult(
        ok=True,
        text=keep_trailing_newlines("\n".join(out), text),
        seg=seg,
        before=seg.text,
    )


def delete_segment(
    text: str, *, match: str = "", now: datetime, within_days: int, tz: tzinfo
) -> EditResult:
    """删掉某一段（含头行），并把它留下的连续空行收成一个。按 ``match`` 模糊定位。"""
    segments = split_segments(text)
    seg, error = find_segment(segments, match, now, within_days, tz)
    if seg is None:
        return EditResult(ok=False, error=error)
    return delete_segment_at(text, seg)


def delete_segment_at(text: str, seg: Segment) -> EditResult:
    """删掉**已经定位到的那一段**（含头行），并把它留下的连续空行收成一个。"""
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


# ---- 回收站还原（第 15 步）----------------------------------------------------


def segment_from_record(record: dict) -> Segment:
    """从归档记录（``store`` 存的 JSON）还原成 ``Segment``，**行号占位**。

    行下标在这里没意义——它只对"刚 split 出来的那份文本"有效。真正用得上的只有
    头行四个字段与正文，还原时按它们重建头行（见 ``insert_segment``）。
    """
    return Segment(
        head_line=make_head_line(
            str(record.get("date") or ""),
            str(record.get("time") or ""),
            record.get("mood"),
            record.get("who"),
        ),
        head_idx=-1,
        end_idx=-1,
        date=str(record.get("date") or ""),
        time=str(record.get("time") or ""),
        mood=str(record.get("mood") or ""),
        who=str(record.get("who") or ""),
        text=str(record.get("text") or "").strip(),
    )


def insert_segment(text: str, seg: Segment) -> str:
    """把一段按**时间顺序**插回原文（回收站还原用）。

    插在"第一个比它晚的段"之前；都不比它晚就追加到末尾。这样还原出来的本子仍按
    日期时间排列，不会因为还原本是前天的就跑到最后一页去。
    """
    body_lines = seg.text.split("\n")
    if not body_lines or not body_lines[0].strip():
        body_lines = [line for line in body_lines if line.strip()]

    existing = split_segments(text)
    at = len(text.split("\n"))
    for other in existing:
        if other.stamp > seg.stamp:
            at = other.head_idx
            break

    lines = str(text or "").split("\n")
    block = [seg.head_line] + body_lines + [""]
    if at < len(lines) and lines[at - 1].strip():
        block = [""] + block  # 被手改过的文件：上一段没留空行，补一个，别把两段粘一起
    joined = "\n".join(lines[:at] + block + lines[at:])
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return keep_trailing_newlines(joined, text)



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
    "SEG_ID_LEN",
    "TAG_MAX",
    "TIME_FMT",
    "EditResult",
    "Entry",
    "Segment",
    "clock_of",
    "day_of",
    "delete_segment",
    "delete_segment_at",
    "find_by_id",
    "find_segment",
    "insert_segment",
    "keep_trailing_newlines",
    "make_head_line",
    "normalize_body",
    "parse_entries",
    "parse_stamp",
    "render",
    "rewrite_segment",
    "rewrite_segment_at",
    "sanitize_mood",
    "sanitize_tag",
    "scope_of",
    "seg_id",
    "segment_from_record",
    "split_segments",
    "within_window",
]
