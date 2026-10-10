"""日记**格式契约**（第 1 步；存储在第 17 步改为 JSONL）。

**权威存储是 JSONL，一行一段**（键序冻结，见 ``ENTRY_LINE_KEYS``）——结构化数据
放结构化文件，正文里不再编码 id。旧版把 person_id 拼进头行、靠正则抠出来，一个怪
id 就能炸掉写路径；那个方案已废弃。

头行降级为**展示/内容地址前像**：:

    2026-09-23 03:45（喜欢）〔和某某〕      ← 头行：日期 时间（心情）〔标注〕
    正文若干行
                                            ← 空行 = 段间隔

它不再是文件里的定界符，只在两处出现：``seg_id`` 的哈希前像（从结构化字段重建，
面板与存储两侧同源）、以及旧 ``.txt`` 导入时的解析（见 ``split_segments``）。

本模块只做**纯数据手术**：不做文件 IO、不读配置、不认识平台对象。

⚠️ 两条不变式（原包实测踩过，改这里前先读 `docs` 里 P1-b 的复盘）：

1. **重建头行与旧解析共用 ``HEAD_RE`` 与 ``TAG_MAX``**——导入旧文件、以及任何把
   条目序列化成文本的地方，都必须过同一套收敛（``make_head_line``）；
2. **头行字段是历史，不可改**——编辑只换正文（Entry 其余字段原样保留）。
"""

from __future__ import annotations

import hashlib
import json
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

_TAG_PERSON_RE = re.compile(r"#([A-Za-z0-9_.:-]{1,32})$")
"""从旧头行标注尾部抠出 person_id（``和希#1411638634``）。

只服务于**旧 .txt 导入**：新存储里归属是 JSONL 的独立字段，展示文本里不再编码 id。
id 允许字母数字与 ``_.:-``（QQ 是纯数字，别把别平台的 id 也堵死）。"""

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


def split_tag(raw: object) -> tuple[str, str]:
    """旧头行里那一段标注 → ``(可读名, person_id)``。**只用于旧 .txt 导入**。

    没有 ``#`` 的（旧文件、群聊那种无归属的）返回 ``(原样, "")``。
    """
    text = str(raw or "").strip()
    match = _TAG_PERSON_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].strip(), match.group(1)


def make_head_line(date: str, time: str, mood: object = "", tag: object = "") -> str:
    """重建头行（``seg_id`` 前像 / 纯文本视图用）。**不携带 person_id**——
    归属是 JSONL 的独立字段，展示文本里永远不编码 id。"""
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
    person_id: str = ""
    """这一段写给谁（头行 ``和希#1411638634`` 里 id 那截）。

    旧文件没有这一项，为空串——那是"不归属任何人"，**不是**"不属于任何人"：
    按人过滤时它们进不了别人的卡片，但自己翻本子时照样在。"""

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
    person_id: str = ""
    """这一段写给谁；见 ``Segment.person_id``。"""

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


# ---- JSONL（权威存储，第 17 步）-------------------------------------------------
#
# 一行一段：{"date","time","mood","who","person_id","text"}。``book`` 不在行里
# ——两本日记是两个文件，读侧注入。坏行读取侧丢弃兜底（state_history 同一口径），
# 绝不让一行坏数据掀翻整本。


ENTRY_LINE_KEYS = ("date", "time", "mood", "who", "person_id", "text")
"""JSONL 行的冻结键序。**键名可加不能改**（与 storage 契约同一口径）。"""


def entry_line(entry: Entry) -> str:
    """Entry → 一行 JSONL（键序冻结，``ensure_ascii=False``：中文原样可读）。"""
    record = {
        "date": entry.date,
        "time": entry.time,
        "mood": entry.mood,
        "who": entry.who,
        "person_id": entry.person_id,
        "text": entry.text,
    }
    return json.dumps(record, ensure_ascii=False)


def entry_from_line(line: str, book: str = NORMAL) -> Entry | None:
    """一行 JSONL → Entry；空行 / 坏行（不是 JSON 对象、缺 date）返回 ``None``。"""
    text = str(line or "").strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict) or not str(record.get("date") or "").strip():
        return None
    return Entry(
        date=str(record.get("date") or ""),
        time=str(record.get("time") or ""),
        mood=str(record.get("mood") or ""),
        who=str(record.get("who") or ""),
        person_id=str(record.get("person_id") or ""),
        text=str(record.get("text") or ""),
        book=book,
    )


def entries_from_jsonl(text: str, book: str = NORMAL) -> list[Entry]:
    """整份 JSONL → 条目列表（空行与坏行丢弃；既无正文也无心情的空段丢弃）。"""
    out = [entry_from_line(line, book) for line in str(text or "").splitlines()]
    return [e for e in out if e is not None and (e.text or e.mood)]


def view_text(entries: list[Entry]) -> str:
    """条目 → 纯文本视图（旧 ``.txt`` 版式：头行 + 正文，段间空一行，结尾空一行）。

    只做展示 / 人工查看，**不再是存储格式**——头行由 ``make_head_line`` 从结构化
    字段重建（写入侧与解析侧同源的老不变式），所以视图永远可以被 ``parse_entries``
    原样读回来（person_id 除外——它本来就不进展示文本）。
    """
    blocks = []
    for entry in entries:
        head = make_head_line(entry.date, entry.time, entry.mood, entry.who)
        blocks.append(f"{head}\n{entry.text}" if entry.text else head)
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def entry_segment(entry: Entry) -> Segment:
    """Entry → Segment 视图：让时间窗 / ``find_segment`` / ``seg_id`` 那套既有检查
    直接复用。头行按展示字段重建（**不含** person_id），行号占位无意义。
    """
    return Segment(
        head_line=make_head_line(entry.date, entry.time, entry.mood, entry.who),
        head_idx=-1,
        end_idx=-1,
        date=entry.date,
        time=entry.time,
        mood=entry.mood,
        who=entry.who,
        text=entry.text,
        person_id=entry.person_id,
    )


def entry_seg_id(entry: Entry) -> str:
    """条目的内容地址（面板改删唯一定位）。

    = 从结构化字段重建头行再哈希，与旧版对 txt 算的 ``seg_id`` **同一算法同一前像**
    ——已发布数据（头行从不带 id）升级后 id 不变；面板与存储两侧都从这一个函数取，
    谁也不会算岔。
    """
    return seg_id(entry_segment(entry))


def entry_from_record(record: dict) -> Entry | None:
    """回收站记录 → Entry；没正文返回 ``None``（与 restore 的"没正文不动"口径一致）。"""
    text = str(record.get("text") or "").strip()
    if not text:
        return None
    return Entry(
        date=str(record.get("date") or ""),
        time=str(record.get("time") or ""),
        mood=str(record.get("mood") or ""),
        who=str(record.get("who") or ""),
        person_id=str(record.get("person_id") or ""),
        text=text,
    )


def insert_entry_chronologically(entries: list[Entry], entry: Entry) -> list[Entry]:
    """按时间顺序插回一条（回收站还原）：插在第一个比它晚的段之前，都不晚就追加。

    这样还原出来的本子仍按日期时间排列，不会因为还原本是前天的就跑到最后一页去。
    """
    for index, other in enumerate(entries):
        if other.stamp > entry.stamp:
            return entries[:index] + [entry] + entries[index:]
    return entries + [entry]


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
        who, person_id = split_tag(match.group(4))
        segments.append(
            Segment(
                head_line=lines[row],
                head_idx=row,
                end_idx=end_idx,
                date=match.group(1),
                time=match.group(2),
                mood=match.group(3) or "",
                who=who,
                text=body,
                person_id=person_id,
            )
        )
    return segments


def parse_entries(text: str, book: str = NORMAL) -> list[Entry]:
    """整份文本 → 条目列表（丢掉既没正文也没心情的空段）。"""
    out = [
        Entry(date=s.date, time=s.time, mood=s.mood, who=s.who, text=s.text, book=book, person_id=s.person_id)
        for s in split_segments(text)
    ]
    return [e for e in out if e.text or e.mood]


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


# ---- 回收站还原（第 15 步）----------------------------------------------------


def segment_from_record(record: dict) -> Segment:
    """从归档记录（``store`` 存的 JSON）还原成 ``Segment``，**行号占位**。

    行下标在这里没意义。头行按展示字段重建（**不含** person_id）——内容地址
    两侧同源就靠这一条；归属走 ``entry_from_record`` / ``Segment.person_id``。
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
        person_id=str(record.get("person_id") or ""),
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
    "ENTRY_LINE_KEYS",
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
    "entries_from_jsonl",
    "entry_from_line",
    "entry_from_record",
    "entry_line",
    "entry_seg_id",
    "entry_segment",
    "find_by_id",
    "find_segment",
    "insert_entry_chronologically",
    "make_head_line",
    "normalize_body",
    "parse_entries",
    "parse_stamp",
    "render",
    "sanitize_mood",
    "sanitize_tag",
    "scope_of",
    "seg_id",
    "segment_from_record",
    "split_segments",
    "view_text",
    "within_window",
]
