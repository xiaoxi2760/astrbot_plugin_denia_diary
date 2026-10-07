"""陪伴系统 · AstrBot 适配器（第 1 步：日记；第 2 步：小本本；第 3 步：状态；第 4 步：主动消息）。

**本文件是唯一 import astrbot 的地方**，只做翻译不做判断：

- LLM 工具：``diary_write`` / ``diary_read`` / ``diary_list`` / ``diary_edit``；
  ``note_add`` / ``note_list`` / ``note_complete`` / ``note_forget``；
  ``mood_report``（心情自报，参数全字符串——AstrBot 不生成 ``required``，
  数字类型 + 默认值会把"没提供"写成 0）；
- 提示挂载点：``on_llm_request`` → 互动计数（``affinity.touch``）→ ``core.compose``
  渲染结果挂进 ``req.extra_user_content_parts``（临时内容块，**不拼 system_prompt**——
  每轮都变的文本拼在 system_prompt 尾部会打掉前缀缓存）；
- 主动消息（第 4 步）：``initialize()`` 重建巡检 job（basic handler 只在内存注册表，
  热重载即失效 → 必须删旧建新）；巡检 handler 里闸门与触发器都在 core 判定，这里只
  负责出站——``add_active_job`` 唤醒她本人 / ``StarTools.send_message`` 直发暗号；
  ``on_using_llm_tool`` 是"真的发出去了"的确认点（只认主动轮的
  ``send_message_to_user``，被动轮她回复用的同一个工具不算）。

"该不该"全部在 ``core`` 里：会话身份、可见性、上限、时间窗、开关都在内核算，这里只传参和套壳。

⚠️ 工具描述是**行为规范**，不是注释：改文案等于改她的行为。
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .core import compose
from .core import outbound as outbound_mod
from .core import scope
from .core import settings as settings_mod
from .core import storage
from .core import webui_portrait
from .core import webui_settings
from .core.diary import format as fmt
from .core.diary.api import Diary
from .core.diary.store import DiaryStore
from .core.notebook import Notebook, NotebookStore
from .core.proactive import Proactive
from .core.proactive.store import ProactiveStore
from .core.session import Session
from .core.state import Affinity, State
from .core.state.store import AffinityStore, StateStore
from .web_api import register_all

try:  # pragma: no cover - 取决于 AstrBot 版本（``extra_user_content_parts`` 的内容块类型）
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001 - 旧版本没有这个模块：注入退回 system_prompt
    TextPart = None  # type: ignore[assignment, misc]

PLUGIN_NAME = "astrbot_plugin_denia_diary"


def _plugin_version() -> str:
    """插件版本：**唯一来源是 `metadata.yaml`**（别在代码里再写一份，两处维护必然漂移）。

    注册发生在 import 期，所以这里就地读一次；读不到就回 `0.0.0`（加载不能因为读文件失败而挂掉）。
    """
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    found = re.search(r"^version:\s*[\"']?([^\s\"']+)", text, re.MULTILINE)
    return found.group(1) if found else "0.0.0"

PROMPT_PRIORITY = 5
"""``on_llm_request`` 的优先级（越大越先跑；AstrBot 默认 0）。

显式声明而不是靠默认值：多插件同时注入 ``system_prompt`` 时，顺序必须是可预期的。
"""

PROACTIVE_JOB_NAME = f"{PLUGIN_NAME}#proactive-patrol"
ACTIVE_JOB_NAME = f"{PLUGIN_NAME}#proactive-wake"
"""cron job 命名：``initialize()`` 按 name 找旧 job 删掉重建（僵尸一律清，
不留"库里还有同名 job 但 handler 已失效"的残骸）。"""


@register(
    PLUGIN_NAME,
    "xiaoxi2760",
    "给她一本自己的纯文本日记、一个小本本，和情绪 / 作息 / 熟悉度，她会主动找你说话",
    _plugin_version(),
)
class DeniaDiary(Star):
    """陪伴系统插件。第 1 步日记 + 第 2 步小本本 + 第 3 步状态系统 + 第 4 步主动消息 + 第 5 步 WebUI 面板。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.settings = settings_mod.load_settings(self.config)
        self.locks = storage.KeyedLocks()
        self.layout = storage.Layout(self._resolve_data_dir()).ensure()
        self.diary = Diary(
            settings=self.settings,
            layout=self.layout,
            store=DiaryStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
        )
        self.notebook = Notebook(
            settings=self.settings,
            layout=self.layout,
            store=NotebookStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
        )
        self.state = State(
            settings=self.settings,
            layout=self.layout,
            store=StateStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
        )
        self.affinity = Affinity(
            settings=self.settings,
            layout=self.layout,
            store=AffinityStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
        )
        self.proactive = Proactive(
            settings=self.settings,
            layout=self.layout,
            store=ProactiveStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
            diary=self.diary,
            notebook=self.notebook,
            state=self.state,
            affinity=self.affinity,
        )
        self.portraits = webui_portrait.PortraitStore(layout=self.layout, locks=self.locks)
        logger.info("[%s] 数据目录：%s", PLUGIN_NAME, self.layout.base_dir)
        for message in self.settings.warnings:
            logger.warning("[%s] 配置降级：%s", PLUGIN_NAME, message)

    # ---- 生命周期 ------------------------------------------------------------

    async def initialize(self) -> None:
        """插件加载后调用：重建主动消息的巡检 job（basic handler 只在内存注册表）。"""
        self.layout.ensure()
        # schema 对齐自检（第 5.2 步对齐）：_conf_schema.json 与 core.settings 默认值表
        # 的键集合 / 分组 / 大类归属双向核对——AstrBot 会剔除 schema 里不存在的键，
        # 两边不一致时用户保存的值会在重载时静默丢失，必须启动就喊出来。
        for problem in webui_settings.verify_schema_alignment(webui_settings.load_schema_file()):
            logger.warning("[%s] schema 对齐自检：%s", PLUGIN_NAME, problem)
        register_all(self.context, PLUGIN_NAME, self)  # WebUI 面板路由（无 register_web_api 则静默跳过）
        await self._setup_proactive_job()

    async def terminate(self) -> None:
        """插件卸载 / 热重载前调用：把熟悉度合并写窗口的增量冲到盘上（正常关闭不丢互动计数）。

        主动消息状态（proactive.json）是写穿的，没有内存态要收尾。
        """
        try:
            await self.affinity.store.flush()
        except Exception as error:  # noqa: BLE001 - 卸载路径不能被磁盘错误卡死
            logger.warning("[%s] 熟悉度收尾落盘失败：%s", PLUGIN_NAME, error)

    async def apply_settings(self, updated: dict, *, applied: list[str] | None = None) -> dict:
        """WebUI 改配置后的**热生效**（第 5.2 步）：备份 → 落盘 → 就地重建
        ``self.settings`` → 巡检间隔变了就重建 job。返回结构化结果，**不抛异常**。

        为什么必须就地重建而不只是写盘：门面（Diary / Notebook / State / Affinity /
        Proactive）在构造时各拿了一份 ``settings`` 引用，插件本体并不会因为配置文件
        变了而重新构造——只写盘等于"改了没生效"，用户会以为面板在骗人。

        落盘走插件**自己的活配置对象**（``AstrBotConfig`` 是 dict 子类，构造时注入）：
        ``clear()`` 后 ``save_config(updated)``；``save_config`` 不可用时回落
        ``update()``。**不走** Dashboard 的 ``/api/v1/plugins/{id}/config``——那条路是
        "写盘 + 热重载插件"，会把整个插件重启掉。
        """
        paths = [str(item) for item in (applied or [])]
        result: dict = {
            "ok": True,
            "applied": paths,
            "warnings": [],
            "reloaded": False,
            "backup": "",
            "notices": [],
        }
        resolved = settings_mod.load_settings(updated)
        old_patrol = int(self.settings.proactive.get("patrol_minutes", 15))
        new_patrol = int(resolved.proactive.get("patrol_minutes", 15))
        old_timezone = str(self.settings.timezone)
        new_timezone = str(resolved.timezone)

        # 1) 备份旧配置：备份不下来就**不改**（写坏配置的风险比"这次没改成"大）
        try:
            backup = webui_settings.backup_config_file(
                getattr(self.config, "config_path", None), self.layout.base_dir
            )
        except OSError as error:
            logger.warning("[%s] 备份旧配置失败，本次改动未落盘：%s", PLUGIN_NAME, error)
            return {**result, "ok": False, "error": f"备份旧配置失败，未做改动：{error}"}
        if backup is not None:
            result["backup"] = backup.name

        # 2) 落盘
        try:
            previous = copy.deepcopy(dict(self.config))
            payload = copy.deepcopy(dict(updated))
            self.config.clear()
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save(payload)
                # 有些宿主版本的 save_config 只写盘不回写内存对象；补一刀保证"改了立刻生效"
                if dict(self.config) != payload:
                    self.config.clear()
                    self.config.update(payload)
            else:
                self.config.update(payload)
        except Exception as error:  # noqa: BLE001 - 落盘失败回落到内存值，不能半途改坏
            logger.warning("[%s] 写配置失败：%s", PLUGIN_NAME, error)
            try:
                self.config.clear()
                self.config.update(previous)
            except Exception:  # noqa: BLE001 - 回落也失败就只留日志
                pass
            return {**result, "ok": False, "error": f"写配置失败，未做改动：{error}"}

        # 3) 热生效：就地换掉插件与各门面的 settings 引用（不重建门面、不搬数据）
        self.settings = resolved
        for facade in (self.diary, self.notebook, self.state, self.affinity, self.proactive):
            if hasattr(facade, "settings"):
                facade.settings = resolved
        for message in resolved.warnings:
            logger.warning("[%s] 配置降级：%s", PLUGIN_NAME, message)
        result["warnings"] = list(resolved.warnings)

        # 4) 巡检间隔变了必须重建 job（basic handler 只在内存注册表，表达式不会自己变）
        #    时区变了也重建：job 上是**建的时候**那个 `timezone`，判定侧却读实时的
        #    `settings.zone()` —— 不重建就会出现"调度按旧时区、判断按新时区"。
        #    ⚠️ 实话：`*/N * * * *` 这种纯分钟步长**几点执行与时区无关**（整小时偏移
        #    下触发时刻完全一样），所以真正的危害只是面板上那个 job 的时区显示是旧的、
        #    以及万一以后加了"每天几点"的 job 会踩坑；顺手对齐，不留两套真相。
        timezone_changed = old_timezone != new_timezone
        if new_patrol != old_patrol or timezone_changed:
            await self._setup_proactive_job()
            result["reloaded"] = True
            if new_patrol != old_patrol:
                result["notices"].append(f"主动消息巡检已重建：{old_patrol} → {new_patrol} 分钟一次。")
            if timezone_changed:
                result["notices"].append(f"主动消息巡检已按新时区重建：{old_timezone} → {new_timezone}。")

        logger.info(
            "[%s] 设置已生效：%s%s",
            PLUGIN_NAME,
            "、".join(paths) or "（无改动）",
            f"（备份 {result['backup']}）" if result["backup"] else "",
        )
        return result

    async def _setup_proactive_job(self) -> None:
        """重建巡检 job（§0#2）：basic job 的 handler 按注册表存活，插件热重载即失效，
        ``update_job`` 又不会重新注册 → 唯一正确姿势是查到旧 job 就 ``delete_job``
        再 ``add_basic_job`` 新建；库里同名但 handler 已失效的僵尸 job 一律清掉。
        """
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            logger.warning("[%s] 宿主没有 cron_manager，主动消息巡检不可用", PLUGIN_NAME)
            return
        try:
            minutes = int(self.settings.proactive["patrol_minutes"])
            for job in list(await manager.list_jobs()):
                if getattr(job, "name", "") == PROACTIVE_JOB_NAME:
                    await manager.delete_job(job.job_id)
            await manager.add_basic_job(
                name=PROACTIVE_JOB_NAME,
                cron_expression=f"*/{minutes} * * * *",
                handler=self._proactive_patrol,
                description="陪伴系统 · 主动消息巡检（判定该不该发，内容判定在 core）",
                timezone=self.settings.timezone,
                persistent=False,
            )
            logger.info("[%s] 主动消息巡检已重建：每 %d 分钟一次", PLUGIN_NAME, minutes)
        except Exception as error:  # noqa: BLE001 - 调度失败不能拖垮其余功能
            logger.warning("[%s] 主动消息巡检 job 注册失败（其余功能不受影响）：%s", PLUGIN_NAME, error)

    async def _proactive_patrol(self, *args, **kwargs) -> None:
        """巡检 handler（basic job，每 ``patrol_minutes`` 分钟一次）：闸门 + 触发器都在
        core 判定（``proactive.patrol``），这里只把决策送出去。首行判开关——面板上
        改配置不用重载插件，下一个 tick 就生效。"""
        if not self.settings.subsystem("proactive"):
            return
        try:
            decision = await self.proactive.patrol(now=self._now())
        except Exception as error:  # noqa: BLE001 - 巡检失败只留日志
            logger.warning("[%s] 主动消息巡检失败：%s", PLUGIN_NAME, error)
            return
        if not decision:
            return
        umo = str(decision["umo"])
        # 日志区分"已派发"和"已发出"（复核 #10）：唤醒是异步的，发出与否看确认点的日志
        logger.info(
            "[%s] 主动消息已派发（slot=%s → %s）：%s",
            PLUGIN_NAME,
            decision["slot"],
            umo,
            str(decision.get("fragment") or "")[:60],
        )
        if decision["kind"] == "direct":
            await self._send_signal(umo, str(decision["char"]))
        else:
            await self._wake(umo, str(decision["note"]))

    async def _wake(self, umo: str, note: str) -> None:
        """唤醒她本人：``add_active_job`` 走真 agent 管线，她拿着完整注入 + 上下文包
        自己组织语言（``payload["session"]`` 是必需项，不给 ``send_message_to_user``
        根本不会挂上——§0#3/#4）。"""
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            return
        try:
            await manager.add_active_job(
                name=ACTIVE_JOB_NAME,
                cron_expression=None,  # 复核 #1：该参数没有默认值，必须显式传
                payload={"session": umo, "note": note},
                run_once=True,
                run_at=self._now(),
            )
        except Exception as error:  # noqa: BLE001 - 派发失败只留日志，配额已扣（两段式）
            logger.warning("[%s] 主动唤醒派发失败（配额已扣，轨迹未写）：%s", PLUGIN_NAME, error)

    async def _send_signal(self, umo: str, char: str) -> None:
        """直发心情暗号（复核 #2）：``send_message`` 返回 ``True`` 才算真的发出去了；
        未初始化会 raise、平台没找到会返回 False——两种失败都**不写** ``last_sent_at``
        （发失败绝不能污染互动轨迹），也**不写暗号冷却**（第 11 步：冷却记在
        ``confirm_sent`` 里，只有真送达才落——一次没送达不该换她 3 天不能用暗号）。"""
        try:
            sent = await StarTools.send_message(umo, MessageChain(chain=[Plain(char)]))
        except Exception as error:  # noqa: BLE001
            logger.warning("[%s] 暗号直发异常（不写 last_sent_at、不烧冷却，下次巡检还会再试）：%s",
                           PLUGIN_NAME, error)
            return
        if sent is not True:
            logger.warning("[%s] 暗号直发未送达（不写 last_sent_at、不烧冷却，下次巡检还会再试）→ %s",
                           PLUGIN_NAME, umo)
            return
        await self.proactive.confirm_sent(umo, now=self._now())
        logger.info("[%s] 暗号已发出 → %s", PLUGIN_NAME, umo)

    # ---- 内部 ----------------------------------------------------------------

    def _host_data_dir(self) -> Path:
        """宿主给的插件数据目录；取不到就退回 ``data/plugin_data/<插件名>``。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as error:  # noqa: BLE001 - 宿主实现可能没有这个方法
            fallback = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
            logger.warning("[%s] 取不到插件数据目录（%s），改用 %s", PLUGIN_NAME, error, fallback)
            return fallback

    def _resolve_data_dir(self) -> Path:
        """数据目录：**只接受宿主插件数据目录本身或它里面的子目录**。

        为什么不给"任意路径"：审计要求持久化数据落在 ``data/plugin_data/<插件名>``。
        让 ``data_dir`` 能写到插件数据目录之外，这条保证就形同虚设——备份、
        卸载清理、权限隔离全都依赖"数据只在插件目录里"这个前提。所以 ``data_dir``
        只作"换个子目录"用；越界（含绝对路径指向别处）就记 warning 并**退回默认目录**，
        而不是照着写出去。留空（默认）＝用宿主给的目录。
        """
        default = self._host_data_dir()
        configured = str(self.settings.data_dir or "").strip()
        if not configured:
            return default
        try:
            target = Path(configured).expanduser().resolve()
            root = default.resolve()
        except OSError:  # pragma: no cover - 路径异常（非法字符等）时按未配置处理
            target, root = Path(configured).expanduser(), default
        if target == root or root in target.parents:
            return target
        logger.warning(
            "[%s] data_dir=%s 不在插件数据目录 %s 里面，已退回默认目录。"
            "持久化数据只允许落在 data/plugin_data/%s 里。",
            PLUGIN_NAME,
            configured,
            default,
            PLUGIN_NAME,
        )
        return default

    def _session(self, event: AstrMessageEvent) -> Session:
        return Session.from_event(event)

    def _scope_denied_reason(self, session: Session) -> str:
        """启用范围判定（第 7 步）：范围外回**人话原因**，空串＝放行。

        判定只住在 ``core.scope.allows``（档位 → owner/private/all）；面板与 WebUI
        接口不走这里——主人视角永远可用。
        """
        allowed, reason = scope.allows(
            str(dict(self.settings.scope).get("mode") or ""),
            is_private=session.is_private,
            person_id=session.person_id(),
            love_peers=list(self.settings.love_peers),
        )
        return "" if allowed else reason

    def _now(self) -> datetime:
        return datetime.now(self.settings.zone())

    def _which(self, book: str) -> str:
        return "恋爱日记" if book == fmt.LOVE else "日记"

    # ---- 工具：写 ------------------------------------------------------------

    @llm_tool("diary_write")
    async def diary_write(
        self,
        event: AstrMessageEvent,
        text: str = "",
        mood: str = "",
        date: str = "",
        valence: str = "",
        arousal: str = "",
    ) -> str:
        """写日记（**你自己的本子**）。想写就写：值得记的一天、某个人说的话、你自己的一点情绪。一天可以写好几条，日期时间自动记下，别自己写日期、别排版。

⚠️ 写**你自己的话**：第一人称、口语，可以有情绪和吐槽；**别写成工作汇报**，也别把路径、令牌、别人的隐私写进去。

Args:
            text(string): 日记正文。分段就**直接换行**，别打出 `\\n` 这两个字面字符。
            mood(string): 可选，一两个词的心情（如"有点开心"）。
            date(string): 可选，写到哪一天（YYYY-MM-DD），默认今天。
            valence(string): 可选，愉悦度打分 -2~2 的整数（开心为正、难受为负）。
            arousal(string): 可选，激活度打分 -2~2 的整数（精力充沛为正、疲惫为负）。
        """
        session = self._session(event)
        denied = self._scope_denied_reason(session)
        if denied:
            # 范围外整轮不介入（第 7 步）：不写日记、不观察情绪——配置改回范围内即可恢复
            return f"这个会话不在启用范围内，我不在这里记。({denied})"
        result = await self.diary.write_async(
            session, text=text, mood=mood, date=date, now=self._now()
        )
        if not result.get("ok"):
            return f"没写成：{result.get('error') or '未知原因'}"
        # 心情沉淀是 best-effort：打分解析失败或落盘出错都不影响日记写入的结果
        try:
            await self.state.observe(
                session,
                word=mood,
                valence=_parse_coord(valence),
                arousal=_parse_coord(arousal),
                now=self._now(),
            )
        except Exception as error:  # noqa: BLE001 - 沉淀失败不能带累日记
            logger.warning("[%s] 日记心情沉淀失败：%s", PLUGIN_NAME, error)
        return (
            f"写好了：{result['date']} {result['time']}，今天第 {result['entries_today']} 段"
            f"（{result['chars']} 字，记在**{self._which(result['book'])}**里）。"
            "这是你自己的本子，没人检查。"
        )

    # ---- 工具：翻 ------------------------------------------------------------

    @llm_tool("diary_read")
    async def diary_read(
        self,
        event: AstrMessageEvent,
        date: str = "",
        q: str = "",
        tail: int = 20,
        all: bool = False,
        scope: str = "",
        book: str = "",
    ) -> str:
        """翻自己的本子。看最近写的：直接调（默认最近 20 条）；看某一天：给 date；看整本：all=true；找写过什么：给 q。

恋爱日记那本里全是和最亲密的人有关的，**只在这本子主人自己的私聊里翻得到**。

Args:
            date(string): 哪一天（YYYY-MM-DD）。
            q(string): 搜关键词。
            tail(number): 最近多少条，默认 20，最多 200。
            all(boolean): true ＝ 从头读整本（太长会被截断）。
            scope(string): group ＝ 只看群聊，private ＝ 只看私人；不填＝全部。
            book(string): love ＝ 只看恋爱日记，normal ＝ 只看普通日记；不填＝两本都算。
        """
        result = self.diary.read_for(
            self._session(event),
            date=date,
            q=q,
            tail=tail,
            all_=bool(all),
            scope=scope,
            book=book,
            now=self._now(),
        )
        if not result.get("ok"):
            return str(result.get("error") or "翻不了")
        header = f"（{result['count']} / 共 {result.get('total', result['count'])} 段）\n"
        return header + str(result["text"])[:8000]

    @llm_tool("diary_list")
    async def diary_list(
        self, event: AstrMessageEvent, limit: int = 7
    ) -> str:
        """翻日记目录（哪天写了、几段、每段开头一句）。想不起哪天写过什么时先用它。

Args:
            limit(number): 最多列多少天，默认 7，最多 60。
        """
        result = self.diary.list_days(self._session(event), limit=limit, now=self._now())
        if not result.get("ok"):
            return str(result.get("error") or "翻不了")
        if not result["days"]:
            return "（本子还是空的）"
        lines = [
            f"- {day['date']}（{day['entries']} 段，{day['chars']} 字）{('：' + day['first'] + '…') if day['first'] else ''}"
            for day in result["days"]
        ]
        return f"你的日记（最近 {len(lines)} 天，本子共 {result['total']} 段）：\n" + "\n".join(lines)

    # ---- 工具：改 / 删 --------------------------------------------------------

    @llm_tool("diary_edit")
    async def diary_edit(
        self,
        event: AstrMessageEvent,
        action: str = "list",
        book: str = "",
        match: str = "",
        text: str = "",
    ) -> str:
        """改或删**你自己写的**日记段（写错了、后悔了、不想留了）。

⚠️ 三条边界：① **只能动最近这些天**的段落；② **开头那行不动**，只改正文；③ 每次改/删**原文都会自动备份**。

action：list（看有哪些段、哪些还能改）/ rewrite（要带 text）/ delete。用 match 指定哪一段（正文里几个字就行；不填＝最后一段）。**不用传 book**，不传就两本一起找。

Args:
            action(string): list / rewrite / delete，默认 list。
            book(string): normal 或 love；一般不用传，不传＝两本一起找。
            match(string): 正文里的几个字，用来指定哪一段；不填＝最后一段。
            text(string): rewrite 用的新正文（开头那行会自动保留）。
        """
        result = await self.diary.edit_for(
            self._session(event),
            action=action or "list",
            book=book,
            match=match,
            text=text,
            now=self._now(),
        )
        if not result.get("ok"):
            message = str(result.get("error") or "没改成")
            if result.get("hint"):
                message += f"\n（{result['hint']}）"
            return message
        if result.get("action") == "list":
            segments = result.get("segments") or []
            if not segments:
                return "这两本里还没有段落。"
            lines = [
                f"- {seg['date']} {seg['time']}"
                f"{('（' + seg['mood'] + '）') if seg['mood'] else ''}"
                f"［{self._which(seg['book'])}］"
                f"{'' if seg['editable'] else '［太旧，只能看］'}"
                for seg in segments
            ]
            return (
                f"共 {len(segments)} 段（最近 {result['within_days']} 天内的才能改）：\n"
                + "\n".join(lines)
            )
        return str(result.get("text") or "好了。")

    # ---- 工具：小本本（第 2 步） ----------------------------------------------

    @llm_tool("note_add")
    async def note_add(
        self,
        event: AstrMessageEvent,
        kind: str = "",
        text: str = "",
        about: str = "",
        due: str = "",
    ) -> str:
        """往小本本上记一条。**只记两类**：promise ＝ 约定（答应对方的事、约好一起做什么，可带期限）；fact ＝ 关于对方的重要事实（生日、喜好、工作、身体）。不填 about 就是记给眼前这个人。

⚠️ **日常闲聊、情绪、玩笑一律不记** —— 每人每类有条数上限。满了会说「本子满了」，那就先 note_forget 删几条旧的，或把几条并成一条再记。

Args:
            kind(string): fact ＝ 重要事实；promise ＝ 约定。
            text(string): 要记的一句话，越短越好。
            about(string): 关于谁（对方的用户 id）。不填 ＝ 眼前这个人。
            due(string): 只在记约定时用：期限（YYYY-MM-DD），没有就空着。
        """
        session = self._session(event)
        denied = self._scope_denied_reason(session)
        if denied:
            return f"这个会话不在启用范围内，我不在这里记。({denied})"
        result = await self.notebook.note_add(
            session, kind=kind, text=text, about=about, due_at=due, now=self._now()
        )
        if not result.get("ok"):
            return f"没记成：{result.get('error') or '未知原因'}"
        name = result["about_name"]
        if result["kind"] == "promise":
            due_note = f"，期限 {result['due_at']}" if result.get("due_at") else ""
            return (
                f"记好了：给{name}的约定{due_note}"
                f"（这类还开着 {result['count']}/{result['limit']} 条）。"
            )
        return f"记好了：关于{name}的一条事实（{result['count']}/{result['limit']} 条）。"

    @llm_tool("note_list")
    async def note_list(self, event: AstrMessageEvent, kind: str = "") -> str:
        """翻小本本：看看记了哪些、id 是什么（完成或删除都要用 id）。私聊里看得到关于眼前这个人的事实与约定；**群聊里只报数、不念内容**。

Args:
            kind(string): fact ＝ 只看事实；promise ＝ 只看约定；不填 ＝ 都看。
        """
        result = self.notebook.note_list(self._session(event), kind=kind)
        if not result.get("ok"):
            return str(result.get("error") or "翻不了")
        if result["scope"] == "group":
            count = result["open_count"]
            if not count:
                return "小本本上还没有开着的事。"
            return (
                f"你还有 {count} 件答应过的事没办完（内容不在群里念）。"
                "想看具体条目或删掉几条，回私聊翻 note_list。"
            )
        blocks: list[str] = []
        promises = result["promises"]
        facts = result["facts"]
        name = result["about_name"]
        if promises:
            lines = [
                f"- [{item['id']}] {'已完成' if item.get('done_at') else '未完成'}"
                f"{('，期限 ' + item['due_at']) if item.get('due_at') else ''}：{item['text']}"
                for item in promises
            ]
            blocks.append("约定：\n" + "\n".join(lines))
        if facts:
            lines = [f"- [{item['id']}] {item['text']}" for item in facts]
            blocks.append("事实：\n" + "\n".join(lines))
        if not blocks:
            return f"（{name}名下还没记任何东西。）"
        limits = result["limits"]
        blocks.append(
            f"（{name}名下共 {len(promises)} 条约定 / {len(facts)} 条事实，"
            f"上限 {limits['promise_limit']}/{limits['fact_limit']}；"
            "完成用 note_complete，删用 note_forget。）"
        )
        return f"小本本 · 关于{name}：\n" + "\n".join(blocks)

    @llm_tool("note_complete")
    async def note_complete(self, event: AstrMessageEvent, note_id: str = "") -> str:
        """把一条约定销账。只有约定能「完成」，事实没有这个说法（要删用 note_forget）。id 从 note_list 里取。

Args:
            note_id(string): 条目 id（note_list 里每条方括号里那串）。
        """
        result = await self.notebook.note_complete(
            self._session(event), note_id, now=self._now()
        )
        if not result.get("ok"):
            return str(result.get("error") or "没销成")
        return f"销账：{result['text']}（这条约定完成了，本子上腾出一个坑。）"

    @llm_tool("note_forget")
    async def note_forget(self, event: AstrMessageEvent, note_id: str = "") -> str:
        """删掉小本本上的一条（记错了、过时了、要合并几条时用）。删掉的条目进回收站，管理员还能找回。id 从 note_list 里取。

Args:
            note_id(string): 条目 id（note_list 里每条方括号里那串）。
        """
        result = await self.notebook.note_forget(
            self._session(event), note_id, now=self._now()
        )
        if not result.get("ok"):
            return str(result.get("error") or "没删成")
        which = "约定" if result["kind"] == "promise" else "事实"
        return f"删掉了一条{which}：{result['text'][:40]}（已进回收站。）"

    # ---- 工具：心情自报（第 3 步） --------------------------------------------

    @llm_tool("mood_report")
    async def mood_report(
        self,
        event: AstrMessageEvent,
        word: str = "",
        valence: str = "",
        arousal: str = "",
    ) -> str:
        """记下你此刻的心情（给自己记账，不是发给别人看）。感觉变了、聊完一轮、睡前盘点，想到就报一次。

valence ＝ 愉悦度（-2 很难受 ~ +2 很开心），arousal ＝ 激活度（-2 疲惫 ~ +2 精力充沛），都是**可选**的整数打分；打了分会帮你校准语气，**分数不展示给任何人**。不打分就只记心情原词。

Args:
            word(string): 此刻心情的原话，一两个词（如：有点烦 / 超开心）。
            valence(string): 可选，愉悦度 -2~2 的整数；不填＝不动坐标。
            arousal(string): 可选，激活度 -2~2 的整数；不填＝不动坐标。
        """
        session = self._session(event)
        denied = self._scope_denied_reason(session)
        if denied:
            return f"这个会话不在启用范围内，我不在这里记。({denied})"
        result = await self.state.observe(
            session,
            word=word,
            valence=_parse_coord(valence),
            arousal=_parse_coord(arousal),
            now=self._now(),
        )
        if not result.get("ok"):
            return str(result.get("error") or "没记下")
        saved_word = result.get("word") or ""
        if saved_word:
            return f"记下了：{saved_word}。"
        return "记下了（这会儿说不清，只记了打分）。"

    # ---- 提示挂载点（被动轮） --------------------------------------------------

    @filter.on_llm_request(priority=PROMPT_PRIORITY)
    async def inject_diary_hint(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """先记一次互动（``affinity.touch``），再把 ``core.compose`` 渲染的短段挂进本次请求。

        主动轮（cron 唤醒）**不会**触发这个钩子：那边由插件把同一份渲染结果拼进
        ``payload.note``（见方案 §三 与 §4.4）。``touch`` 只在她被叫醒的这一刻计数
        （群聊普通发言没有信号，复核 #4）：私聊记 private，群聊记 mention。
        ``note_contact`` 顺手记下"这个人平时在哪找我说话"（第 4 步触发器的目标反查表）。

        挂载点见 ``_attach_hint``：**不碰 ``system_prompt``**，走临时内容块。
        """
        if req is None:
            return
        session = self._session(event)
        if self._scope_denied_reason(session):
            return  # 范围外整轮不介入（第 7 步）：不注入、不计数、不记录——否则熟悉度与
            # 联系人表会替她"记住"一个她根本不该工作的会话
        await self.affinity.touch(
            session, kind="private" if session.is_private else "mention", now=self._now()
        )
        await self.proactive.note_contact(session)
        text = compose.compose_prompt(
            self.diary,
            session,
            notebook=self.notebook,
            state=self.state,
            affinity=self.affinity,
            now=self._now(),
        )
        if not text:
            return
        self._attach_hint(req, text)

    def _attach_hint(self, req: ProviderRequest, text: str) -> None:
        """把渲染结果挂进本次请求——**绝不拼 ``system_prompt``**。

        ``system_prompt`` 是前缀缓存的关键部分，而这段渲染随会话 / 时间 / 心情**每轮都变**：
        拼在它尾部等于每次请求都让前缀缓存失效，代价是实打实的 token 与首字延迟。
        所以走 ``extra_user_content_parts``——那是"每轮临时内容"的位置，不参与前缀缓存；
        再标 ``mark_as_temp()`` 让它不落进会话历史（这段是"她此刻的状态"，不是用户说过的话，
        攒进历史只会越滚越长地挤掉真对话）。

        兜底：宿主版本没有该字段或 ``TextPart`` 时退回拼 ``system_prompt``——
        丢缓存只是慢一点，不能不让她看见这段状态。
        """
        if TextPart is None:  # pragma: no cover - 取决于宿主版本
            req.system_prompt = (req.system_prompt or "") + text
            return
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                parts = []
                req.extra_user_content_parts = parts
            part = TextPart(text=text)
            marker = getattr(part, "mark_as_temp", None)
            if callable(marker):
                part = marker()
            parts.append(part)
        except Exception as error:  # noqa: BLE001 - 兜底也不能把她的这轮带崩
            logger.warning(
                "[%s] 临时内容块注入失败，退回 system_prompt：%s", PLUGIN_NAME, error
            )
            req.system_prompt = (req.system_prompt or "") + text

    # ---- 确认点（主动轮） ------------------------------------------------------

    @filter.on_using_llm_tool()
    async def note_proactive_sent(
        self, event: AstrMessageEvent, tool, tool_args
    ) -> None:
        """"真的发出去了"的确认点（§0#7）：主动唤醒路径里她真调了
        ``send_message_to_user`` → 写 ``last_sent_at``（两段式第二步）。

        被动轮她回复用的也是同一个工具——靠事件上的 ``cron_job`` extra 区分：
        只有 cron 唤醒的事件带它（§0#4/#5）。全程吞异常：确认失败只影响轨迹措辞，
        不能带崩她的回复。
        """
        try:
            # 第 5.1 步：出站文本清洗。**必须排在下面的确认点之前**——被动轮她回复时
            # 下面会因没有 cron extra 提前 return，但标记照样会漏给用户。
            self._strip_outbound_marks(tool, tool_args)
            if getattr(tool, "name", "") != "send_message_to_user":
                return
            getter = getattr(event, "get_extra", None)
            cron_extra = getter("cron_job", None) if callable(getter) else None
            if not cron_extra:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            await self.proactive.confirm_sent(umo, now=self._now())
            logger.info("[%s] 主动消息已发出（确认点命中）→ %s", PLUGIN_NAME, umo)
        except Exception as error:  # noqa: BLE001
            logger.warning("[%s] 主动消息确认点失败：%s", PLUGIN_NAME, error)

    def _strip_outbound_marks(self, tool: object, tool_args: object) -> None:
        """出站文本清洗（第 5.1 步）：把 ``&&名字&&`` 从发出去的消息里剥掉。

        **必须原地改**：``tool_args`` 紧接着被 ``execute(**valid_params)`` 展开成**同一个
        dict**，所以 ``tool_args["messages"] = ...`` 真的作用于这次发送；返回新 dict 无效。

        判断本身在 ``core.outbound``（纯函数、零 astrbot 依赖、可离线测），这里只做
        接线与兜底：

        - 只处理工具名为 ``send_message_to_user`` 的调用；
        - 只动 ``messages`` 里 ``type == "plain"`` 的 ``text``；
        - **整段 try/except**：清洗坏了就按原文发送——这条是红线，不能连累她说话。
        """
        try:
            if getattr(tool, "name", "") != outbound_mod.OUTBOUND_TOOL:
                return
            if not isinstance(tool_args, dict):
                return
            config = dict(getattr(self.settings, "outbound", {}) or {})
            if not bool(config.get("strip_meme_marks", True)):
                return
            original = tool_args.get("messages")
            cleaned = outbound_mod.clean_messages(
                original, pattern=str(config.get("marker_pattern") or "")
            )
            if cleaned is original:  # 纯函数：没变化时原样返回同一个对象
                return
            tool_args["messages"] = cleaned  # 原地改同一个 dict
            logger.info("[%s] 出站已剥离表情包标记（%d 个组件）", PLUGIN_NAME, len(cleaned))
        except Exception as error:  # noqa: BLE001 - 红线：清洗失败不影响她说话
            logger.warning("[%s] 出站清洗失败，按原文发送：%s", PLUGIN_NAME, error)


def _parse_coord(value: object) -> int | None:
    """把工具参数里的字符串打分翻译成 -2~+2 的整数；空串/非法＝没提供（``None``）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(-2, min(2, int(float(text))))
    except ValueError:
        return None


__all__ = ["PLUGIN_NAME", "PROMPT_PRIORITY", "DeniaDiary"]
