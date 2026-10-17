# 开发指南

本文面向改代码 / 接接口的人。安装和日常使用请看 [README](README.md)。

## 项目结构

| 路径 | 职责 |
| :--- | :--- |
| `main.py` | 适配层之一：10 个工具 + `/看日记` 指令 + 提示挂载点 + 生命周期 + WebUI 注册 |
| `web_api/` | WebUI 的 HTTP 适配层（在 `core/` 之外） |
| `core/` | 内核：零 AstrBot 依赖，纯函数，可离线测试 |
| `pages/diary/` | 插件页：单目录自包含，零构建零依赖 |
| `tools/webui_selfcheck.py` | 静态对账：路由表 ↔ 前端 endpoint + 资源与编码体检 |
| `test/` | 单测（放插件根下） |

```
astrbot_plugin_denia_diary/
├─ metadata.yaml / _conf_schema.json / README.md / DEVELOPMENT.md / logo.png / LICENSE
├─ template/            /看日记 图片卡的 Jinja2 模板（paper / ink / postcard）
├─ main.py
├─ web_api/
│  ├─ __init__.py       对外只暴露 register_all
│  ├─ _web.py           astrbot.api.web 的唯一导入点（离线时降级成桩）
│  ├─ routes.py         路由表 + 表驱动 register_all（含 hasattr 兼容守卫）
│  └─ handlers.py       薄壳 handler：解析 request → 调 core 纯函数 → json_response
├─ pages/diary/
│  ├─ index.html        左导航 + stage 骨架 + bridge 缺失守卫
│  ├─ app.js            bridge 就绪 / endpoint 封装 / 视图切换 / "对谁"选择器
│  ├─ ui.js             h / clear / toast / 格式化 / 手写 SVG 折线
│  ├─ style.css         自写样式（不引外部 CSS）
│  ├─ preview-bridge.js 离线预览用的 stub bridge（只在 file:// 或 ?preview=1 生效）
│  └─ views/            七个视图：overview / diary / notebook / affinity / proactive / data / settings
├─ tools/webui_selfcheck.py
├─ core/                内核：零 AstrBot 依赖，可离线测试
│  ├─ settings.py       配置默认值、取值范围、校验与降级
│  ├─ storage.py        存储契约：目录布局 / 原子写 / mtime 缓存 / schema_version
│  ├─ compose.py        状态渲染（唯一出口）：按优先级拼段 + 注入预算（400 字、五类）
│  ├─ session.py        会话身份（umo → 标注 / 分流 / 可见性；person_id 收口"眼前人"）
│  ├─ scope.py          启用范围判定（纯函数）
│  ├─ outbound.py       出站文本清洗（纯函数）
│  ├─ diary/            format（格式契约）· store（文件层）· api（语义入口）
│  │  ├─ card.py        服务端模板渲染（Jinja2 子集，零第三方依赖）
│  │  ├─ card_cache.py  已渲染卡的落盘缓存（按内容寻址 + LRU 淘汰）
│  │  ├─ card_render.py 出图编排：缓存 → playwright → remote → pil 降级梯
│  │  ├─ card_local.py  引擎 playwright：本机无头浏览器（懒 import）
│  │  └─ card_pil.py    引擎 pil：Pillow 直接画（保底，不出网不起浏览器）
│  ├─ notebook/         store（文件层 + 上限 + 回收站）· api（语义入口）
│  ├─ state/            store · affinity（漏积分器）· api
│  ├─ proactive/        store · gate（闸门）· triggers（七触发器）· api（门面）
│  ├─ greeting/         store（greetings.json）· api（识别 / 记录 / 注入素材，第 19 步）
│  ├─ voice/            语音出口决策（纯函数：speak_plan / speakable_indices / apply_audio，F1）
│  ├─ webui_data.py     面板数据面（纯函数、可离线单测）
│  └─ webui_settings.py 设置页数据面（schema 递归展开 + 校验准绳）
└─ test/
   ├─ support.py        公共设施（不被 discover 收集）
   └─ test_{settings,storage,session,diary,compose,notebook,state,proactive,greeting,voice,main}.py
```

## 适配器（`main.py`）

| 挂载点 | 内容 |
| :--- | :--- |
| `diary_write` | 写一条（正文 / 可选心情 / 可选日期 / 可选 `valence`、`arousal` 打分；写完 best-effort 沉淀心情） |
| `diary_read` | 按天 / 最近 N 条 / 整本 / 关键词翻；可按 `scope`（群聊·私聊）与 `book`（普通·恋爱）筛 |
| `diary_list` | 目录：最近几天、各几段、每段开头一句 |
| `diary_edit` | `list` / `rewrite` / `delete`（只能动最近 N 天，头行不动，改删前备份） |
| `note_add` | 记一条（`kind`＝fact / promise；`about` 缺省＝眼前人；约定可带 `due` 期限） |
| `note_list` | 翻本子（带条目 id；群聊只报数不念内容） |
| `note_complete` | 把约定销账（写 `done_at`，完成即腾坑） |
| `note_forget` | 删一条（先过归属判定，进 `trash` 回收站） |
| `mood_report` | 心情自报（`word` 原词 + 可选 `valence` / `arousal` 字符串打分）；**没有 `affinity_*` 工具**，她不碰熟悉度数值 |
| `sleep_check` | 查**眼前人**的作息记录（早晚安时间 / 睡眠时长 / 连续天数）；私聊限定（口径同 diary_read），没记录就明说——工具描述写明「记不到就说没有，别编」 |
| `@filter.command("看日记")`（别名 /日记卡） | **用户指令，不是 LLM 工具**（第 14 步）：人主动敲才跑，把一天渲染成图片卡。读取走 `read_day_for`（结构化 + 可见性与 `read_for` 同源）；渲染三档降级链 pretty（`html_render` + `template/diary_card_*.html.j2`，日记正文 POST 给文转图端点）→ plain（本机 `card_pil`）→ 纯文本；**群聊不落正文**（口径同 note_list 只报数）；正文进模板前一律 `html.escape`（该链路无 autoescape）。六款模板 + 自定义字体见 [日记卡](#日记卡第-15-步重做) |
| `@filter.command("日记款式")`（别名 /日记卡样式、/换日记款式） | **换卡片款式**（第 15 步，同属用户指令）：`/日记款式 手帐`。落盘 + 热生效，走 `apply_settings`；认不出的款式明说不静默回落 |读取走 `read_day_for`（结构化 + 可见性与 `read_for` 同源）；渲染三档降级链 pretty（`html_render` + `template/diary_card_*.html.j2`，日记正文 POST 给文转图端点）→ plain（本机 `card_pil`）→ 纯文本；**群聊不落正文**（口径同 note_list 只报数）；正文进模板前一律 `html.escape`（该链路无 autoescape）。六款模板 + 自定义字体见 [日记卡](#日记卡第-15-步重做) |
| `on_llm_request`（priority=5） | 先 `affinity.touch`（私聊 private / 群聊 mention）+ `proactive.note_contact`（person→会话反查表）+ `greeting.observe`（整条消息是问候才记，记上后 `mark_slot_today` 双向防重），再把 `core.compose` 渲染的一段挂进 `req.extra_user_content_parts`（见下）；**主动轮不触发它**，那边走 `payload.note` |
| `on_using_llm_tool` | 发送前清洗标记、转换主动语音；不确认发送。 |
| `on_llm_tool_respond` | 仅匹配主动轮、消息工具、与当前会话一致的成功结果，才写发送轨迹；同一事件只确认一次。 |
| `initialize` / `terminate` | 生命周期。`initialize()` 重建巡检 job（basic handler 只在内存注册表，热重载即失效 → 按 name 查旧 job 一律 delete 再 add）；`terminate()` 冲熟悉度合并写窗口 |

工具描述是**行为规范**（"别写成工作汇报"、"开头那行不动"、"只记两类"、"闲聊情绪玩笑不记"），不是注释——改文案等于改她的行为，`test/test_main.py` 逐条钉住了这些关键词。

### 提示注入的挂载点

随会话/时间/心情**每轮都变**的动态内容（`compose_prompt` 产物，≤400 字）挂 `req.extra_user_content_parts`（`TextPart(...).mark_as_temp()`，只参与本轮请求、不进会话历史）——**不拼 `req.system_prompt`**：system_prompt 是前缀缓存的关键部分，尾部每次都变会显著抬高 token 开销（上架审核第二条）。宿主过旧（拿不到 `TextPart`，< v4.24.2）或注入抛异常时，退回追加 `system_prompt`，不报错。主动轮拼进 `payload.note` 的做法不受影响。

## 日志约定（logger）

- 全插件 logger **唯一来源**是 `core/_log.py`：`core/` 内 `from ._log import logger`（或子包 `from .._log import logger`）；`web_api/` 用 try/except 双分支 `from ..core._log import logger` / `from core._log import logger`；`main.py` 直接 `from astrbot.api import llm_tool, logger`。
- **严禁 `import logging` / `logging.getLogger(__name__)`**——上架审核的硬性规范。
- `core/_log.py` 做的是"一次降级，全局复用"：真机上 `astrbot.api.logger` 是**按调用者解析的代理**（`_PluginContextLogger.__getattr__` 用 `sys._getframe(1)` 找调用方模块 → 落到插件专属 logger，行前缀 `[astrbot_plugin_denia_diary]` 由宿主自动加，core/ 里的调用同样正确落位）；离线（无 astrbot）降级成 `_NullLogger` 空壳，接口同形、什么都不做。
- **裁定**：审核规范 A（只能 `astrbot.api` logger）高于本仓"core/ 零 astrbot import"的约定 B。`core/_log.py` 是**全仓唯一**允许出现 astrbot 字样的内核模块，那行 import 必须包在 try/except（只捕 `ImportError`）里；三处守卫按"唯独放行 `_log.py` 且验 try/except 形"的口径执行——`tools/webui_selfcheck.py` 的 core/ 扫描、`test/test_webui_data.py` 与 `test/test_outbound.py` 的 `test_core_has_no_astrbot_import`。别在本文件之外再引入 astrbot import。
- 提示：消息文本里手写的 `[%s] PLUGIN_NAME` 前缀与宿主自动加的插件名前缀会重复（历史习惯，纯观感），后续轮次可统一去掉文本前缀。

## 日记系统

三层，职责不重叠：

| 层 | 文件 | 只做这些 |
| :--- | :--- | :--- |
| 格式契约 | `core/diary/format.py` | **JSONL 行编解码（`ENTRY_LINE_KEYS` 冻结键序）**、旧 txt 导入解析、条目级改删 / 还原、`seg_id`、渲染。无文件 IO、无配置、不认识平台 |
| 文件层 | `core/diary/store.py` | 两本的位置、**JSONL 读写 + 旧 `.txt` 一次性导入**、改删前备份、**回收站归档与还原**、每文件一把 `asyncio.Lock` |
| 语义入口 | `core/diary/api.py` | `read_for` / `write_for` / `edit_for` / `prompt_line` / `event_hint`；**`enabled` 与可见性判定都在这里** |

第 15 步新增的一层在 store 上：`apply_by_id`（按 `seg_id` 改删，面板用）、`archive_segment` / `trash_items` / `trash_record` / `restore_segment`（回收站）。第 18 步把存储换成 JSONL 后，`match`（聊天工具）与按 `seg_id`（面板）两条路**共用同一份条目手术**（`DiaryStore._surgery`）——先备份原文，再落盘，免得以后改一边忘了另一边。`seg_id` 由 `format.entry_seg_id` 从结构化字段统一重建，已发布数据（头行从不带 id）升级后 id 不变。

已落地的约定（原包 `qq-bridge-diary` 实测过的坑，见该包分析报告的 P 编号）：

1. **写入侧与解析侧同源**：标注一律经 `format.make_head_line()` + `TAG_MAX=40` 截断，超长不再让整段消失（P1-b）。
2. **读取入口唯一**：恋爱日记只对 `love_peers` 名单内那个人的私聊可见；群聊与其他人读不到（P1-a）。
3. **`enabled=false` 时读也读不到**（原包只挡了写，P2-b）。
4. **群聊标注带显式 `群·` 前缀**：读取侧不再靠"以『和』开头"猜（旧条目仍兼容旧启发式）。
5. **头行是历史不可改**，改只换正文；改/删**先备份原文**到 `diary-trash/`。
6. **未来日期与"太旧"口径一致**：`list` 说不可改的，`edit` 也不会改（P3-b）；面板的豁免开关**只豁免"太旧"，不豁免"未来"**。
7. **凌晨那句措辞不怪人**：`prompt_line` 的状态词在 `00:00~05:59` 说「新的一天刚开头，还没写」，其余时间才说「今天还没写」——凌晨日历已经翻页，但她的一天还没开始（默认作息表第一段就是 `06:00|刚醒`）。
   ⚠️ **没有引入"逻辑日"**：`day_of` / 条目归属 / `slots_today` / 配额 / 熟悉度衰减一律照旧按日历日算，`EARLY_HOURS_END` 只决定那句话怎么写（有测试钉住"凌晨写的日记仍归当天"）。

时间统一走 `settings.zone()`（默认 `Asia/Shanghai`），禁止裸 `datetime.now()`。

## 小本本

**只记重要的**，两类条目："日常闲聊、情绪、玩笑不记"由工具描述声明 + 数量硬上限 + 管理员最终删除权（面板）三道闸落地。

| 型 | 字段 | 注入策略 |
| :--- | :--- | :--- |
| 约定型 | `id` / `text` / `about` / `created_at` / `due_at`（可空）/ `done_at` | 私聊：关于眼前人的未完成约定全文（过期排前，带期限标记）；群聊：只出一条脱敏计数行 |
| 事实型 | `id` / `text` / `created_at` / `updated_at` | **只在与该人的私聊**注入（同一个群 ≠ 该人在场，群里一律不念） |

口径（落进 `core/notebook/api.py`）：

1. **记录不限会话**：群聊也能记，`about` 缺省＝"眼前人"（`Session.person_id()`：私聊＝对端、群聊＝发言者、主动轮从 umo 兜底推导）。
2. **注入判定写在 api 入口内部**，不靠工具自觉：`facts_for` 群聊一律空；`promise_line` 群聊只报全局未完成数（不带内容）。
3. **改动按条目归属**：`about == 眼前人` 才能完成 / 删（私聊群聊同一套）；别人的条目一律"没找到"（不泄露存在性）。
4. **上限**：约定 20 / 事实 15（每人每类，配置项）；约定按**未完成**计（完成即腾坑）。满了返回「本子满了，先删掉或合并几条再来」——不报错；"合并"没有专门工具，就是删旧（`note_forget`）+ 写新（`note_add`）。
5. **回收站**：删掉的条目进 `notebook.json` 的 `trash`（上限 50，超了丢最旧），面板管理员可查。
6. `note_complete` 对事实 id 返回"事实没有「完成」一说"；约定销账写 `done_at` 后不再注入、不占上限。

注入预算（总量 400 字、五类）：约定（优先级 30）→ 事实 + 熟悉度档位词（20）→ 互动轨迹（15）→ 状态（10）→ 日记提醒（0）；超预算按砍序**整档**丢弃（`core/compose.py`），"先砍基调保当下"这类类目内部规则留在各自系统里。

## 状态系统

三合一：**情绪**（当下 + 近期基调 + 自报原词）、**作息**（config 时段表 → 状态词）、**熟悉度**（漏积分器单标量 + 粗档位词）。三者最终产物只有注入里的一两行 + 触发器用的机器可读出口；**数值永不进注入**。

1. **坐标由她自报，系统永不做「原词 → 坐标」映射**：`mood_report` 与 `diary_write` 的 `valence` / `arousal` 是**字符串**参数（`""`＝没给），给了才动坐标（-2~+2 越界夹取），没给只沉淀原词。坐标给衰减与暗号阈值；注入用她的原词（没有原词才用象限词：挺开心 / 很平静 / 有点烦躁 / 有点低落）。
2. **两层情绪**：当下（快层，自报驱动，**6 小时可见期**，坐标按 **3 小时半衰期**衰减回中性）；近期基调（慢层，每次观测向目标挪一步地沉淀，**7 天半衰期**）。注入合成一行："最近她心情不错，不过这会儿有点烦。"
3. **基调限幅**：单次最多移动一格、单日最多一格——防"一篇很丧的日记把基调拽低一整周"。状态档 50 字里情绪句 ≤44 字，超出**先砍基调、保当下**（规则留在 `State.mood_line` 内部）。
4. **作息是纯函数不落盘**：config 表每行「HH:MM|状态词」（默认 5 段，支持周末第二张表，留空沿用工作日）；到点的最近一段生效，凌晨归入最后一段。`is_late_night()` 按 `state.late_night`（默认 `23:30-06:30`，可跨午夜）喂关怀触发器与主动消息免打扰。
5. **熟悉度 = 漏积分器**：`score ×= λ^(Δt/单位) + Δ`，**λ=0.5、单位=21 天（三周半衰期，待调）**——判据：一周不聊不掉档、一个月不见才明显降（7 天半衰期会让稳定互动者贴着熟稔线跑，一天不聊就掉档，这是返工轮修掉的病根）；Δ 两档——**私聊 1.0 / 群里被叫醒并接话 0.6**（没有"群聊普通发言"档；`kind="group"` 只是 `mention` 的别名）；**单日增量封顶 3.0**；档位阈值 **熟稔 ≥10 / 泛泛 ≥3 / 其余陌生**。
6. **衰减按经过时间折算**：落盘时把上次落盘以来的衰减折进去，读取时把当前时刻的衰减现算出来（score / band / top），不写回——落盘频率再怎么变，数字都可复现。**互动计数合并写**：`touch()` 只更新内存 overlay，窗口安静 1.5 秒后由后台任务统一原子落盘；读侧恒为"文件 + overlay"；正常关闭（`terminate`）强制 flush，硬断电最多丢窗口内（≤1.5 秒）的增量。
7. **互动轨迹（trace 档）零新存储**：`f(affinity.last_ts, proactive.last_sent_at)` 两个时间戳的比较——"她上次主动找他是何时、对方回了没有 + 距上次互动多久"。`proactive.json` 不存在 / 字段缺失 / 解析失败 → **整档返回空串**，不报错、不占预算；字段名 `last_sent_at`（ISO8601，按会话，先按 umo 再按 session_id 查）已冻结。早晚安的问候素材（第 19 步）并进**同一档**（`compose` 的 trace 段拼在 `trace_line` 之后），不新增类目、预算与砍序一个字节没动。
8. **情绪历史独立追加文件**：`state_history.jsonl` 一行一个 JSON（`ts` / `layer` / `valence` / `arousal` / `word`），坐标没变不写、变了按 `layer` 各写一行、保留 180 天——面板情绪曲线的唯一数据源。
9. **注入预算 400 字五类**：`PROMPT_BUDGET` 400，`PRIORITY` 含 `trace`（插在 fact 与 state 之间）；熟悉度档位词并进 **fact 档**（与"关于当前人的事实"同一配额）。不回归保险丝：`state=None` 且 `affinity=None` 时输出与第 2 步逐字节相同（有测试钉住）。
10. **开关**：`subsystems.state` 一刀切管三合一（关了 `observe` 拒写、注入行全空、`is_late_night` 恒 False）；`score` / `band` / `top` 作为纯数据口仍可读（面板本来就直接读文件）。

## 主动消息

三件套职责分离、互不知情，全部收在 `core/proactive/`，cron 与出站 API 在 `main.py`：

1. **触发器（有什么可说）**：七个，各自只产候选（说给谁 / **原文片段** / 优先级），无权决定发不发。优先级 `暗号 > 约定跟进 > 纪念日 > 关怀 > 久未联系 > 早安/晚安 > 日记驱动`——软内容排最后，频率上限先挤掉最像打卡的那类。**有内容才发**：候选必须带具体素材（约定原文 / 事实原文 / 最后互动那天的日记 / 作息词 / 真实的时间事实），取不到就不发。所有目标会话从 `contacts`（`on_llm_request` 里顺手记录的 person → 会话反查表）查 umo；对目标人取料走 `facts_for` / `read_for`，可见性判定原样生效——恋爱日记的内容只会送到它唯一的读者手里。
2. **闸门（该不该说）**：**先闸门后内容**——免打扰是全局闸（常规内容整个静默），配额 / 最小间隔是会话闸（先算出"今天还能对谁说话"，触发器只对这些会话取料，巡检 15min 一天 96 次不白翻日记原文）；然后按优先级逐个问触发器，**一 tick 至多放行一条**。三条硬约束：免打扰时段（`is_late_night`）、私聊每日 2 条 / 群聊每日 1 条、同会话最小间隔 2 小时。**群聊克制**：除约定跟进 / 纪念日外，其余触发器只在私聊出（纪念日的内容源 `facts_for` 本就只在私聊可见，实际能进群的只有约定跟进）。
3. **出站（怎么说）**：默认**唤醒她本人**——`add_active_job(payload={"session": umo, "note": …}, run_once=True)`；`note` = `compose_prompt` 的完整注入（人格一致，不另写渲染器）+ ≤100 字上下文包（`【类目】指令 素材：原文片段`，压掉换行——note 经宿主 `json.dumps` 进 system prompt，预算按转义后算）。**直发只用于心情暗号**：整条消息就是那个字符（默认 `。`，可配 `signal_char`），`StarTools.send_message` 返回 `True` 才算发出（平台没找到 / 异常都不写 `last_sent_at`）。

**两段式 `last_sent_at`**：决策时只扣 `today_count` / `last_slot` / `slots_today`；`last_sent_at` 等**真的发出去**才写——唤醒路径的确认点是 `on_llm_tool_respond`（工具返回目标会话一致的成功结果，且事件带 `cron_job` extra），直发路径是 `send_message` 返回 `True`。中间失败 = 配额白吃一次，换轨迹永不替她说"她找过你了"。日志里"已派发"与"已发出"分开记。

**暗号的冷却也在确认点才写**：`signal_date`（每日一次）与 `signal_last_at`（3 天独立冷却）原先记在 `_commit`（决策时），发送失败不回滚——结果"一次没送达 = 她 3 天不能再用暗号"，而她本人和用户都不知道为什么。现在两个键挪进 `confirm_sent`，只有真送达才落；发失败时下一次巡检会**再试**（每 `patrol_minutes` 一次，直到送出或她缓过来为止），`today_count` 照常每一步都自增（配额按"决策"计，这是刻意的）。

**心情暗号四道闸**（破免打扰，稀有性由四道闸保证）：① 只发 `love_peers` 名单内的**私聊**（没记录过 contact = 不知道往哪发，不发）；② 当下 valence 落最低档——判**衰减后**坐标（`snapshot`），阈值常量 **-0.7（待调）**：自报 -2 后约 4.5h、-1 后约 1.5h 内判中，语义是"她**现在**还在难受"；从没给过坐标的期间不触发；③ 独立冷却 3 天；④ 每天最多一次。

**暗号总开关 `proactive.signal_enabled`（第 20 步补）**：之前这件事**关不掉**——唯一的"变相关掉"办法是把自己从 `love_peers` 里摘掉，而那会连带让恋爱日记对这份名单里的其他人也不可见（第 17 步的可见性规则）。为一个主动消息的观感问题去动日记可见性，代价完全不成比例，所以补了独立开关。

关掉的效果是**一个候选都不产**（`Proactive._signal_candidate` 开头就 return），不是"产了再拦"。理由：留痕会让"为什么今天她一句都没说"这类排查看见一个没生效的开关，用户会以为配置没存上。既然关了，`signal_date` / `signal_last_at` 两个冷却键也不会攒——攒了就等于"欠着一条暗号"，哪天再打开会立刻发一条。

**暗号穿透配额**：`_commit` 里 `slot == "signal"` **跳过配额复核**——防打扰的闸门不该挡住求助信号；稀有性已由四道闸保证。`today_count` 照常自增（暗号也是一条主动消息，计数不失真），所以私聊配额 2 时暗号最多 1 条 + 常规 1 条。

**发送记录（`proactive-log.jsonl`）**：确认发出后追加一行 `{ts, umo, slot, fragment}`（ts 是确认时刻；slot / fragment 取决策暂存，重启丢失则 slot 回落 `last_slot`、fragment 记空串——"她什么时候找过谁"不丢）；append-only，保留最近 **200 行或 90 天**（裁剪时整文件重写一次）。它是面板「主动消息记录」的唯一数据源。

**待回窗口**：`trace_line` 在 `pending_window_hours`（默认 6h）内且对方未回时只说"她刚主动找过你"，窗口外才说"你还没回她"——她刚发完五分钟，不演被冷落。

**同日防重（`slots_today`）**：早安 / 晚安 / 约定跟进 / 纪念日 / 关怀 / 久未联系 / 日记驱动各 slot 当天只发一次（早安晚安各算一个 slot）——2 小时最小间隔挡不住"窗口比间隔长"的类目（纪念日全天、早安窗 2.5h），一天一条的约束收口在这里。

**双向防重（早晚安互动联动，第 19 步）**：他先说了早安/晚安（`core/greeting` 的 `observe` 命中）→ `mark_slot_today` 把该会话当天的 `morning` / `goodnight` slot 占上，`find_greetings` 的防重读的就是 `slots_today`，巡检侧零改动。占坑只写 `slots_today`——不扣 `today_count`（这不是她发的主动消息）、不写 `last_sent_at`（她没发东西）、不进发送记录。

**劝睡窗与免打扰的重叠是正确行为**：劝睡窗 22:30–23:30，`late_night` 默认 23:30 起——23:30 后连劝睡都被免打扰挡掉是设计（深夜连劝睡都不该发）；把 `late_night` 配得更早就更短。各触发器的时段窗口（劝睡 / 劝饭 / 早晚安 / 日记驱动）是代码常量，**待调**；频率七项是配置。

**数据形状**：`proactive.json` = 冻结的 `sessions`（键 umo：`last_sent_at`（冻结键）/ `today_date` / `today_count` / `last_slot`）+ 扩展（`slots_today` / `signal_last_at` / `signal_date` / 顶层 `contacts`）——`trace_line` 的读取面只认 `sessions[*].last_sent_at`，扩展键它一律忽略。

## 语音（F1 第一步：她说，宿主发声）

她的主动消息过宿主 TTS 变成语音发出。**她组织语言的方式完全不变**（还是 `send_message_to_user` 发文字）——插件在 `on_using_llm_tool` 钩子里（出站清洗之后）把主动轮的文本**就地转成 record 组件**，判定全在 `core/voice/`（纯函数，app-ready：S1 壳将来接自己的 TTS 复用同一决策层）。

**上游 API 核对（源码出处见 `调研-F1语音.md`）**：`context.get_using_tts_provider_async()` → `TTSProvider.get_audio(text)` **只有文本参数**（音色/语速在 provider 配置里）→ 音频落 AstrBot temp 目录；`send_message_to_user` 原生认 `{"type": "record", "path": ...}`，且 temp 目录在它的本地文件白名单里——链路天然通。

**她在写之前就知道自己在发语音**（用户 2026-10-11 定稿）：转不转是**巡检决策时掷定的**（`Proactive._decide_speak`：mode + `voice.probability` + 情绪倾向 + roll，测试可注入 `_voice_roll`），结论做两件事——①写进唤醒 note 的 `VOICE_NOTE` 告知句（"像说话一样讲——口语、短句；别用 emoji、颜文字、换行"），她按说话的口吻组织语言、还可能自然说出"我给你发条语音"；②暂存 `_pending_speak[umo]`，钩子按暂存**执行**不再掷——"她被告知"与"实际转语音"永远一致。暂存与 `_pending_log` 同生命周期：确认发出（`confirm_sent`）即清，重启丢失回落 False（不发语音，无害）。

| 环节 | 口径 |
| :--- | :--- |
| 范围 | **只主动轮**（事件带 `cron_job` extra）；被动轮宿主管线有自己的 TTS 开关与概率，插件不做第二份 |
| 档位 | `voice.mode`：`off`（默认）/ `voice`（语音替换文字）/ `dual`（语音在前文字在后，对齐宿主 `dual_output` 惯例） |
| 决策 | `speak_plan`（纯函数）：mode + `voice.probability` + 情绪倾向（当下 valence<0 概率上浮 `SOFT_VALENCE_BOOST=+0.3`，待调）+ roll；probability≤0 永不开口、≥100 必开口。**决策点在巡检**（`_decide_speak`，写 note 告知 + 暂存），钩子只执行 |
| 变换 | `speakable_indices`（非空 plain、≤`MAX_SPEAK_CHARS=200` 待调）→ 适配层逐段 `get_audio` → `apply_audio` 换组件；**没有任何一段合成成功 → 保持原文** |
| 降级红线 | 没配 provider（`get_using_tts_provider*` 拿不到就 None）/ 合成抛异常 / 文本超长 → 原文字照发，**语音坏了不能连累她说话** |
| 不受影响 | 暗号直发走 `StarTools.send_message`（不经工具）；确认点照常（原地改的是发送内容，`send_message_to_user` 工具名不变） |

**明确不做（第一步）**：音色/语速随情绪——宿主标准接口做不到，要自带 OpenAI 兼容 TTS 客户端（tts_emotion_router 的做法）+ 用户配 API key，挂待拍板；跨机部署的文件服务 URL（宿主与 NapCat 不同机时本地路径发不出，需要 `use_file_service`）。

## 早晚安（问候记录，第 19 步）

gscore / 麦麦那类「用户发起」的早晚安互动：**记录归系统、接话归她**——他说了早安/晚安，系统把事实记下来、把一句话素材并进互动轨迹档，怎么回应（调侃他起太晚 / 关心昨晚没睡好）都是她自己组织语言。**不拦消息、不发模板**（与"不替她说话"同源；识别/记录全在 `core/greeting/`，零 astrbot 依赖，App 壳同一套接线）。

| 环节 | 口径 |
| :--- | :--- |
| 识别 | 整条消息 `fullmatch` 可配置正则（`greeting.morning_patterns` / `night_patterns`，默认词表取自 gsuid_core YumekoSleep 的指令词 + 语气后缀容忍）；聊天里**提到**这两个词一律不算——宁可漏不可误伤 |
| 记录 | `greetings.json` 按人事件流 `{date, kind, ts}`；**当日首次为准**（同日重复不记，sleep_tracker "多次刷新"的坑）；冷却（`duplicate_minutes`，默认 30 分钟）内的重复连事件都不算；范围外整轮不留痕 |
| 凌晨归属 | 0-4 点的晚安统计上**归前一天**（`NIGHT_DAY_BOUNDARY`）——"连续 N 天道晚安"数的是睡前的道别 |
| 派生 | 连续天数 = 从归属日往回逐日数；睡眠时长 = 最近晚安 → 这次早安的间隔，落在 **1~14 小时**才说（夜猫子 / 回笼觉算不准就**不提**） |
| 注入 | `line()` 两层（用户 2026-10-11 定稿「记录睡眠时间 + 让她知晓」）：**当轮素材**（2 分钟新鲜窗，"刚说了早安，连续 N 天，昨晚睡了约 X 小时"——这是新闻，当轮说一次）+ **全天知晓**（当天记到过早安 → 全天出**背景行**"（背景：这个人今早 7:50 道过早安，昨晚睡了约 X 小时；已知的事，别主动复述。）"）。反复读框架是调研后采纳的 daily_life 做法（用户反馈"一直注入容易复读"：oyasumi/YumekoSleep/麦麦日程管家对问候只做一次性开口、不常驻；常驻上下文的插件都带"只是参考、别照着说"的框架）。晚安不全天驻留（它由明早的睡眠时长消费）。并进 compose 的 **trace 档** |
| 联动 | 记录命中后调 `Proactive.mark_slot_today` 占掉当天同款主动问候的 slot（**双向防重**：他先说了晚安，她当晚不再发）——只写 `slots_today`，不扣配额、不写 `last_sent_at`、不进发送记录 |
| 查询 | `sleep_check` 工具（第 10 个）：`greeting.report()` 按归属日聚合的作息行 + 连续天数（锚定最近一次事件，今天没问候不清零）；**只看眼前人、私聊限定**——跨人查询没有入口，同 diary_read 的口径 |

**明确不做**（gscore / nonebot_morning 的玩法，与"不做签到打卡积分"冲突）：睡眠评分、连续补偿、排行榜、打卡日历。事件数据都在，以后想加评分是在派生层叠，不用迁移。

## 出站文本清洗

**问题**：她发出去的消息里带的 `&&sleep&&` 会**原样漏给用户**。原因有三层：

1. `send_message_to_user` 只认 `plain / image / record / video / file / mention_user` 六种 type，**没有表情包类型**；
2. 表情包插件唯一的处理点在 `on_decorating_result`，而它第一行就是 `event.get_result()`——**直发路径没有 result 对象，拦不到**；
3. `on_using_llm_tool` 拿到的 `tool_args` 紧接着就被 `execute(**valid_params)` 展开成**同一个 dict** → 在钩子里改真的生效。

**做法**：在现有的 `on_using_llm_tool` 发送前钩子最前面加一步清洗，`tool_args["messages"] = clean_messages(...)` **原地改**。判断在 `core/outbound.py`（纯函数、零 astrbot 依赖）。

| 规则 | 说明 |
| :--- | :--- |
| 只处理 | 工具名为 `send_message_to_user` 的调用，**主动轮与被动轮都洗** |
| 只动 | `messages` 里 `type == "plain"` 的 `text`；其它 type 与同一个 dict 里的别的键一个字节都不碰 |
| 只剥 | `&&名字&&`（`[x]` / `(x)` / `:x:` / `meme:id` **一律不动**——`(x)` 会吃掉正常括号动作、`:x:` 会吃掉颜文字，**宁可漏不可误伤**） |
| 剥法 | 整段删掉（不替换成文字），并压掉因此产生的多余空格与换行前的悬空空格 |
| 剥空 | 剥完只剩空白 → **保持原文**，绝不发出空串 |
| 绝不抛异常 | 整段 `try/except`；清洗坏了就按原文发送——**清洗坏了不能连累她说话** |

## 日记卡（第 15 步重做）

`/看日记` 出图丑，主因**不是模板**，是出图参数。宿主的 `html_render` 默认是
`{"full_page": True, "type": "jpeg", "quality": 40}`（见 AstrBot
`astrbot/core/utils/t2i/network_strategy.py`），插件不传 `options` 就全盘继承——
**JPEG quality 40 压 15~16px 中文小字**，字缘全是振铃、纸纹和横格线直接断层。
现在显式传 `card.RENDER_OPTIONS = {"type": "png", "quality": 95}`（常量搬到了 `core/diary/card_render.py` 旁边，第 20 步）。

## 出图链路（第 20 步）

`/看日记` 出图慢，**主因不是模板也不是渲染，是"只有远端一条路"**。

第 1~19 步里，模板是 HTML+CSS，要变成 PNG 就得有个渲染引擎；当时仓库零第三方依赖、宿主本地
又没有能解释 CSS 的东西（`LocalRenderStrategy.render_custom_template` 直接
`raise NotImplementedError`），所以只能借宿主的 `html_render`——而它是**远端** t2i。

实测（`tools/bench_card.py`，本机、真实卡片负载）：

| | 耗时 | 备注 |
| :--- | :--- | :--- |
| 本地模板渲 HTML | ~1 ms | 一直在，从不是瓶颈 |
| JSONL 全量解析（3650 条） | 16 ms | 一直在，从不是瓶颈 |
| **远端 t2i** | **4.4 ~ 10.8 s** | 官方端点**随机挑**、失败自动换下一个重试，抖动会翻倍；高峰期还会 429 |
| playwright（冷 / 热） | 1.6 s / 0.5 s | 排版最贴模板 |
| pil | 0.3 ~ 0.5 s | 完全离线 |
| **缓存命中** | **< 1 ms** | 日记是反复翻的东西，这才是常态 |

于是链路改成 **缓存 → playwright → remote → pil**，三档全在，任一可用即可：

```python
ENGINE_ORDER = ("playwright", "remote", "pil")   # core/diary/card_render.py
```

| 环节 | 住哪 | 干什么 |
| :--- | :--- | :--- |
| 缓存 | `core/diary/card_cache.py` | 按 `sha256(款式 + 模板签名 + 模板数据)` 存 PNG；命中刷新 mtime，超 60 张按 LRU 淘汰 |
| playwright | `core/diary/card_local.py` | 本机 Chrome / Edge 截图，浏览器**常驻复用**（冷启 1.4s 只付一次） |
| remote | 宿主的 `html_render` | 回调由 `main.py` 注入，本模块不 import astrbot |
| pil | `core/diary/card_pil.py` | Pillow 照纸感模板重画，保底 |
| 编排 | `core/diary/card_render.py` | 降级梯；判断住 `core/`，才能离线单测 |

### 三条规矩

1. **`diary.card_engine` 选的是"先试谁"，不是"不许退"。** 选中的引擎失败会继续往下退（并记
   日志）。理由和"页码数不出来就不出图"同源——一张出不成图的日记卡对用户等于"今天读不到
   日记"，比慢几秒严重得多。
2. **缓存是加速器，不是前提。** 缓存目录写不进去（只读挂载、磁盘满）最多让下次重新出一遍，
   不该让图发不出去——`card_cache.put` 失败会退到系统临时目录再写一份。
3. **三个引擎都是懒依赖。** `playwright` / `Pillow` 都在**出图那一刻**才 import，探测不到就
   当"这台机器用不了这条路"。配置加载与启动自检一次都不碰它们。

### pil 与模板的关系

`card_pil` 不是"解释 HTML"，是**照 `diary_card_paper.html.j2` 的度量与配色用 Pillow 重画**
（同一套 `--lh: 34px` 行高、同一套颜色、同一个 800px 画布宽度）。CSS 里的 `flex` /
`repeating-linear-gradient` / `feTurbulence` 纸纹在 PIL 里没有对应物，硬解释等于自己写一个
残缺浏览器。

⚠️ 代价说清楚：**pil 只画纸感那一款**，不认 `diary.card_style`；纸纹与投影也没有。所以
`remote` 排在 `pil` 前面——要六款样式都得能选，就该走 remote。

### 量一次

```
py tools/bench_card.py            # 本地三引擎 + 缓存（不联网）
py tools/bench_card.py --remote   # 额外量一次真实远端 t2i
```

### 画布与版式（改模板前必读）

| 事实 | 后果 |
| :--- | :--- |
| 画布固定**约 920px**（宿主自带 t2i 模板写的是 `width: min(100%, 920px)`） | 截图取文档滚动宽度，**把 body 收窄裁不掉**，只会让纸挤在左边、右边一大片空白（旧版就是这样） |
| 截图高度取 `max(内容高, 视口高)` | 内容短时底部空一大片 → `.sheet` 用 `min-height: calc(100vh - 80px)` 撑满 |
| 渲染发生在**远端**无头浏览器 | `template/` 下的图片它读不到，只有 **data URI / 公网 URL** 才有效；纸纹一律内联 SVG |

**纵向节奏只认一个基准**：每款模板都有 `--lh`，正文行高、段间距、`.meta` 行高、
`.entry` 间距**全部取它的整数倍**。差半个行高，文字就骑到横线上去（旧版也是）。

**日期只在右上角出现一次**。页脚再抄一遍是冗余，`test_main.py` 逐款钉住
`{{ date }}` 在模板里只出现 1 次。

### 六款模板

| `card_style` | 样子 |
| :--- | :--- |
| `paper`（默认） | 横线纸：装订孔 + 红边线 + 内联 SVG 纸纹，横线 2px |
| `ink` | 墨信：米色纸、衬线字、抬头双细线、正文首行缩进 2em |
| `postcard` | 明信片：暖橙渐变横幅，右上角是"邮戳"（日期就在这儿） |
| `tape` | 手帐：半透明和纸胶带压四角 + 左侧点状贴纸 + 淡横线 |
| `dots` | 点阵本（bullet journal）：点阵不是方格，字写在上头不打架 |
| `seal` | 火漆信笺：双细线内框 + 右下火漆印 + 衬线字首行缩进 |

枚举唯一来源是 `core/settings.py` 的 `CARD_STYLE_CHOICES`；`test_main.py` 会核
**每个枚举都有对应模板文件**（少一个就静默走降级链出丑图）。`tools/preview_card.py`
的默认款式列表也**读同一份枚举**——写死过一次，加了新款忘了改，预览就只渲三款。

### 换款式的两条路

**① 面板下拉选。** `core/settings.py` 的 `FIELD_CHOICES` 登记"哪些字段是枚举型"
（目前 `diary.card_render` / `diary.card_style`），`describe_schema` 据此给字段
挂一个 `choices` 键，前端就把自由文本框换成 `<select>`。

以前是文本框：选项只写在 hint 里，用户得手打 ``paper``，**输错一个字母就静默回落
默认值、界面上完全看不出哪儿错了**。改成下拉就没有输错这回事。当前值不在枚举里时
会被补进选项（旧配置升上来时老值不该被下拉框藏起来）。

**② 聊天指令 `/日记款式`**（别名 `/日记卡样式`、`/换日记款式`）：

```
/日记款式           → 报当前款式 + 全部可选
/日记款式 手帐      → 换（中文别名认得，不用打 tape）
/日记款式 认不出的  → 明说"没有这一款"，**不**静默回落
```

走的是 `apply_settings`——**落盘 + 热生效**，和面板同一套路径，所以私聊和面板看到的
是同一份配置，不会"面板改了聊天里没变"。只动 `card_style` 一个键，其余日记参数原样带过。

### 手帐排版的三条纪律

后三款（手帐 / 点阵 / 火漆）是照真手帐复刻的，纪律抄在这儿当规范，新增款式照办：

1. **大面积留白，文字不要铺满整张纸**；
2. **配色不超过 3 种淡色**，少大面积涂色；
3. **装饰只放在四边，不许挡住正文**。

第三条最容易犯：`tape` 款的页脚左右各让出 130px，底下那两条胶带正好落在让出来的空档里。
`seal` 款的火漆印压在右下角、页脚就把 ✦ 撤了——一款只留一处装饰。

> ⚠️ 装饰一律**用 CSS / 内联 SVG 画**，不引外部贴图：渲染在远端无头浏览器里，
> `template/` 下的图片它读不到（火漆、和纸胶带、点状贴纸都是 `radial-gradient` /
> `repeating-linear-gradient` 叠出来的）。

### 自定义字体

`diary.card_font`（字符串，默认空＝用款式自带的）。两种写法：

- 字体名 / 字体栈：`Noto Serif SC, serif` → 直接进 `font-family`
- 字体文件直链：`https://…/x.woff2` → 生成 `@font-face`

⚠️ 生成的 CSS 是**原样**内联进模板 `<style>` 的（不像正文那样 `html.escape`——
它是 CSS，转义了就废了），所以 `_card_font_css` 用**白名单正则**自己把关：
字体名只放行 `[A-Za-z0-9 中文 ,._'()]`；直链只放行 `http(s)://…` + 字体后缀。
任何一个不过就整条丢弃、退回款式默认字体并记日志——**宁可字体没生效，
也不能让人往配置里塞 `</style>`**。`test_main.py` 逐条钉死了几个注入样本。

### 本地预览

```bash
py tools/preview_card.py            # 六款并排写到 test/.tmp/cards/index.html
py tools/preview_card.py paper      # 只看某一款
py tools/preview_card.py --open paper
```

出图链路要 AstrBot 起着、要出网、还只看得到最终那张 PNG，改一行 CSS 看不到中间态。
`tools/preview_card.py` 把模板本地渲成 HTML，浏览器直接看，用来迭代排版。
它自己实现了那套模板用到的 Jinja2 子集（`{{ }}` / `{% for %}` / `{% if %}`），
**不为预览给零依赖的仓库引第三方库**；用别的语法会渲不出来（会直接报错，不静默出空图）。

## 启用范围

一个总闸决定"她在哪里工作"：**记录 / 观察 / 注入 / 她的写工具**按会话范围收放，**主动消息的对象**也跟着同一档位走。判定只住在 `core/scope.py`（纯函数，零 astrbot），三个入口（`main.py` 注入钩子、三个写工具、`core/proactive/`）都只调它。

| 档 | 记录 / 观察 / 注入 / 她的写工具 | 主动消息目标 |
| :--- | :--- | :--- |
| `owner`（只主人） | **只在 `love_peers` 里那个人的私聊**里工作；群聊一律不工作 | 主人私聊 |
| `private`（只私聊·**默认**） | **任何私聊**都工作；群聊不工作 | **任何私聊过的人** + 名单内的人 |
| `all`（全部启用） | 私聊 + **群聊**都工作 | 任何私聊过的人 **+ 群聊**（受护栏，见下） |

「工作」= 注入了她的动态上下文 / 写了日记或小本本 / 观察了情绪。**范围外就是整轮不介入**：不注入、`diary_write` / `note_add` / `mood_report` 拒绝并回一句人话（「这个会话不在启用范围内，我不在这里记」）、连熟悉度计数与联系人表都不留痕。

⚠️ **面板与 WebUI 接口永远可用**，不受这个闸影响——否则切到 `owner` 之后，在群里打开面板会被自己锁死。

**升级注意**：从这一步起默认只在私聊工作（`private`），群聊里的记录 / 注入 / 写工具默认全部停止。需要群聊请把 `scope.mode` 切到「全部启用」（`all`）。

### `love_peers` 的语义收窄

`love_peers` = **主人身份**：`owner` 档的判定 + 恋爱日记可见性（这两条不变）。主动消息的对象由**档位**决定（`scope.proactive_targets`），名单里的人只是 `owner` 档下的全部、`private` 档下的子集。心情暗号是例外：它是主人之间的约定信号，对象保持 `love_peers` 私聊不变。

### 主动消息的护栏

- **对象必须"跟她互动过"**：目标集合的唯一来源是 `proactive.contacts`（每个被动轮由 `note_contact` 记录"谁在哪跟我说话"）。从没互动过的人、没见过的群，**任何档位都进不来**；
- **配额沿用按会话的现成计数**：`sessions[<umo>].today_count` + `daily_limit_private` / `daily_limit_group`——扩对象天然每人一份配额，A 发满不影响 B，没有新增计数器；
- 深夜 / 免打扰 / 冷却 / 素材这些闸一个不少；
- **群里不许裸发**：群聊目标的唤醒 note 开头就要求她先 @ 提及对方（`all` 档）。

## 面板（WebUI）

Dashboard → 插件管理 → `astrbot_plugin_denia_diary` → 插件页。**新增/删除页面目录后必须重载插件**（官方 §0#8）；只改静态资源刷新页面即可。

### 七个 tab

| tab | 数据 | 读写 |
| :--- | :--- | :--- |
| 总览（她此刻） | 当下情绪 + 基调 + 作息词 + 今日主动计数 + 四个子系统开关 + 每个数据文件 `{exists, bytes, mtime}` + 版本 | 读 |
| 日记 | 两本的篇数/字数/最近更新；按日期或"最近 N 条"读正文。**恋爱日记默认收起**。两个看法：**正文**（改 / 删在此）与**卡片**（真渲染出来的那张，按天翻页带页码，底部有回收站） | 读 + 写（第 15 步：改 / 删 / 回收站还原） |
| 小本本 | 事实与约定（按顶栏"对谁"过滤）+ 上限；可标记完成 / 删除 | 读 + 写 |
| 熟悉度 | 榜（衰减后分数降序）+ 档位词 + `last_ts` + **当前 `love_peers`（只显示）** | 读 |
| 曲线与主动 | 手写 SVG 折线（valence / arousal，当下令 + 基调点）+ 主动消息计数与发送记录（倒序） | 读 |
| 数据 | 坐标与数据文件体检 | 读 |
| 设置 | **全部配置项**（schema 驱动展开） | 读 + 写 |

### 数据面裁定（重要）

**读与写都走 `store` 层，不走门面。** 门面方法全都吃一个 `Session`，而面板是 Dashboard 登录态、**没有会话上下文**；聊天里的可见性规则与归属判定是**对话安全规则**，套到面板上会让主人自己反而看不到、改不了。所以 `core/webui_data.py` 直接读 `DiaryStore` / `NotebookStore` / `StateStore` / `AffinityStore` / `ProactiveStore`，**不需要 `umo` 也能画出全部内容**。

衰减与档位**不自算**：情绪与作息照抄 `State.snapshot()` 的 `mood`/`rhythm` 子表，榜与档位词照抄 `Affinity.top()` / `Affinity.band()`。

### 面板改 / 删日记（第 15 步）

日记页不再是只读。每段正文下面有「编辑 / 删除」，删掉的段落进**回收站**，可一键还原。

**为什么用 `seg_id` 而不是 `match`。** `diary_edit` 工具靠"正文里有这几个字"定位，那是给 LLM 用的模糊手段——重名的段会指错，指错了比指不到更糟。面板是在 DOM 上点了**某一条**，必须精确指到它。`seg_id` 是**内容地址**（头行 + 正文的 blake2b 前 48 bit，`format.seg_id`）：

- 同一段反复算都是同一个 id（只依赖内容，不依赖行号）；
- 内容一变 id 就变 —— 这一条顺便当**并发护栏**：文件被人手改过、或上一条改删已经动过它，id 对不上，后端回"这段已经变了，请刷新"，而不是照着旧内容盲改；
- `find_by_id` **不许**退化成"最近一段"（那是 `match` 那条路径的行为，两条路各司其职）。

因此前端在**每次写操作后整屏重拉**：留在页面上的 seg_id 全是过期的，接着改必然失败。

**两种备份，两种语义**（别混）：

| | 什么文件 | 谁写 | 能一键还原吗 |
| :--- | :--- | :--- | :--- |
| 整本快照 | `diary-trash/{stamp}-{action}-{book}.jsonl` | `diary_edit` 工具 | ❌ 整本回写会连带抹掉这之后的写入 |
| 单段记录 | `diary-trash/seg-{stamp}-{book}.json` | 面板删除 | ✅ 按时间顺序插回原位 |

回收站**只列单段记录**。整本快照还在同一个目录里，但不摆进回收站——按钮上写"还原"就是在骗人。

**时间窗与豁免开关。** `diary.edit_within_days`（默认 7 天）本来是**她的性格规则**（"老日记是历史"），不是数据权限。所以另给面板一个开关 `diary.panel_edit_unlimited`：

| | `panel_edit_unlimited: false`（默认） | `true` |
| :--- | :--- | :--- |
| 面板 | 只能改最近 N 天 | 任意一段 |
| `diary_edit` 工具 | 只能改最近 N 天 | **仍然**只能改最近 N 天 |
| 未来日期的段 | 不可改 | **仍然**不可改 |

两条底线不变：**头行是历史，改只换正文**；**未来段两种模式都拒**（豁免的是"多久以前"，不是"没发生"——未来段是时钟没同步对）。判据是 `fmt.already_happened`（底线，两种模式共用）+ `fmt.within_window`（限期）。

**面板的写入守同两把尺子**（不然面板能造出聊天侧永远写不出来的东西）：正文先过 `fmt.normalize_body(text, max_chars)`；改 / 删都先写整本快照再原子落盘，顺序不能反。

**其它几条钉死的口径**：

- **恋爱日记面板照改不误** —— 面板是主人视角，不套聊天里的可见性规则（同 §数据面裁定）；
- **回收站上限 200 条**，超了丢最旧的；坏文件跳过不让整页打不开；
- **还原幂等**：同一份记录还原两次，第二次回"这一段已经在本子里了"，绝不插出两条一样的；
- **`trash_id` 钉死在回收站目录内**（`Path(key).name != key` 就拒），挡目录穿越；
- 面板改删**不受 `subsystems.diary` 门控** —— 那是"她能不能记"，与主人能不能改自己的本子无关（读侧本来也不门控）。

### 面板里的卡片预览（第 16 步）

日记页有两个看法：**正文**（原来那个，改删都在这儿）与**卡片**（真渲染出来的那张，
按天翻页，带「第 N / 共 M 页」）。不用为了确认一句样式好不好看去 QQ 上戳一次 `/看日记`。

**渲染发生在插件里，不在前端。** `/看日记` 的出图链路是远端 t2i 服务才有 Jinja2；
宿主要件表里**没有** jinja2（本地装也没用），iframe 里的前端更没有。所以
``GET diary/card`` 由 ``core.diary.card.render_card`` 在服务端渲成 HTML，前端只把
这段塞进 iframe 的 ``srcdoc``。

**三个入口共用一份渲染器**：``/看日记`` 出图、面板预览、``tools/preview_card.py``。
面板里看到的就是真卡片，不是"大概长这样"的近似图——预览和实物对不上，预览就是骗人的。
HTML 转义也因此只在 ``card.card_context`` 一处做（出图链路没有 autoescape）。

⚠️ ``core/diary/card.py`` 只认 ``{{ }}`` / ``{% for %}`` / ``{% if %}``。模板用了别的
语法（``{% set %}``、过滤器）会**抛异常**而不是静默出空图；`test_diary_card.py` 把
六款模板逐个真渲一遍盯着这条。iframe 加了 ``sandbox="allow-same-origin"``（**不加**
allow-scripts）：模板里没有脚本，预览也不该有执行能力。

**预览里的换款式只改预览、不落盘**——真要生效去设置页选或敲 ``/日记款式``，免得随手
一点把她的卡片样式改了又不记得改回来。

### 日记存储换 JSONL（第 18 步）

**权威存储从纯文本 `.txt` 换成 JSONL**（`日记.jsonl` / `恋爱日记.jsonl`）：一行一段
`{"date","time","mood","who","person_id","text"}`，键序冻结（`format.ENTRY_LINE_KEYS`，
键名可加不能改）。动机（第 17 步把 person_id 编码进头行的教训）：

- **归属不该住在展示文本里**。头行拼 `#id` 要靠正则抠出来，一个不合规范的 id 就能炸
  掉写路径；面板与存储两侧各自重建头行还会把 `seg_id` 算岔。结构归结构，展示归展示。
- **生态调研**（2026-10-11）：AstrBot 本体对话历史走 SQLAlchemy+SQLite；同类插件
  daymind 是"正文 `.txt` + 元数据 sidecar `.json`"，★389 的 private_companion 自有
  数据全是 JSON。没人把结构化 id 塞进人类可读文本里。用户拍板走 JSONL。

**迁移**：旧 `.txt` 在首次访问时**一次性导入** JSONL（含旧头行 `#id` 里抠出的归属），
导入后 txt 原样留在数据目录当**人工可读的旧档**，不再读写；导入幂等（以 JSONL 存在
为标记），无需迁移工具。`seg_id` 由 `format.entry_seg_id` 从结构化字段统一重建，与
已发布版本对 txt 算的 id **同一算法同一前像**——升级不改 id，面板在途引用不断。

**归属回填（方案甲，用户 2026-10-11 拍板）**：旧段落头行只有「和某某」没有 id，导入
那一刻按名字反查 `name_preference` + 联系人表（`store.person_id_by_name`）——**恰好
匹配一人才认**，查不到 / 重名 / 群标注一律保持未归属（宁可不见，不可错认）。匹配
不到的批次想重试：删掉对应的 `日记.jsonl` 再触发一次导入即可（旧 txt 还在）。回填
只发生在导入那一次，之后改称呼**不会**追溯改历史归属。

**什么没了**：头行不再是文件格式（`view_text` 仅供展示 / 旧档查看）；"文件以换行
结尾"的不变式随之作废；`rewrite/delete/insert_segment` 那套纯文本手术由条目级
`_surgery` 取代。所有读侧（面板 payload、`/看日记`、注入提示）走 `read_entries`。

### 日历清单与结构化条目

前端要画日历、渲染单页日记本，数据面为此加了两份**只增不改**的键（老键一个没动）：

- `GET diary/list` 每本多出 `days` / `first_date` / `days_truncated`：
  - `days` 是 `{"date": "YYYY-MM-DD", "count": n, "chars": m}` 每天一项，**按 `date` 升序**，同一天多条聚合成一项（`count`/`chars` 是那天合计）；
  - 上限 **730 天**（`MAX_CALENDAR_DAYS`，约两年）：超了只保留**最近的 730 天**并把 `days_truncated` 置 `true`——前端据此提示"更早的不在日历里"。`first_date` 一律取**截断后** `days[0].date`（没有日记就是 `""`），别按"最早一天"单独理解，否则截断时前端会跳进空月份；
  - 顶层 `entries` / `chars` / `latest_date` 仍是**全量口径**，不随截断变。
- `GET diary/content` 多出 `entries` 数组：`{"date", "time", "mood", "who", "chars", "text"}` 每条一项，顺序与 `picked`（`parse_entries` 出来的顺序）完全一致、条数等于 `count`。`mood` / `who` **原样透传**（`mood` 可能是空串；`who` 是标注原文，昵称替换是前端拿 `who_options` 干的活）；`entries[].text` 是那一条的正文原文（可含换行，前端按空行分段渲染）。
  - **`text` 原样保留**（整段视图，老前端与既有测试在读它）；非空 `picked` 时恒有 `"\n\n".join(e["text"] for e in entries) == text`（测试钉住）。

### 「对谁」显示名字

顶栏「对谁」下拉不再是一串裸数字。三件事：

1. **昵称落盘**：`proactive.json` 的 `contacts[<person_id>]` 加 **`name`** 键（加键不改名，不用迁移）——互动时从 `session.sender_name` 写入；**空名字一个字不动**（不写空串、不擦已有值），换新名字才覆盖。
2. **显示串 = `名字(数字)`**：`display_name(settings, person, contacts)` 按 `name_preference[person] → contacts[person]["name"] → 空` 三级回落（与 `Session.label()` 同序），有名字且名字 ≠ id 就拼 `名字(id)`，否则就是 id 本身（**绝不出现** `1411638634(1411638634)`）。**不传 `contacts` 退回旧行为**（只认 `name_preference`，不加后缀）——熟悉度榜等没有联系人表的既有调用零破坏。`who_options*` 每项带 `name`（昵称本体，可能空）与 `label`（显示串），**`id` 恒为 person_id**（前端按 id 去重）。
3. **候选跟档位标注（一个都不删）**：`who_options*` 每项加 `is_owner`（在 `love_peers` 里）与 `out_of_scope`（当前档下她不会在这个人的会话里工作；按 `scope.mode` 只有 `owner` 档会产生档位外的人），`owner` 档主人**置顶**（稳定排序）；三档候选数量相同。`status_payload` / `notebook_payload` 另加 **`scope_mode`**（前端显示"当前启用范围：只主人"）。
4. **默认选中主人**（真机反馈 2026-10-07）：候选合并时若当前没有有效选择（没存过 / 存的 id 已不在候选里），前端自动落到 `is_owner` 第一人并持久化；没有主人可选才显示「（未选）」占位。总览「对谁」卡片读**前端选中态**而非回包 `who_name` 回显——首屏取数发生在默认值落定之前，读回显会把默认主人顶回「未选」。

### 路由表

endpoint **不带插件名前缀**、**不带前导斜杠**（前端写 `"status"`）；注册时由 `web_api/routes.py` 补成 `/astrbot_plugin_denia_diary/status`。

| endpoint | 方法 | 说明 |
| :--- | :--- | :--- |
| `status` | GET | 总览；`?who=` 可选 |
| `diary/list` | GET | 两本概览 |
| `diary/content` | GET | `?book=&date=&tail=`；每条另带 `seg_id` 与 `editable`，顶层带 `edit_window` |
| `diary/rewrite` | POST | `{"book", "seg_id", "text"}` 改一段的**正文**（头行不动；正文先过 `max_chars`） |
| `diary/delete` | POST | `{"book", "seg_id"}` 删一段（**先进回收站**） |
| `diary/trash` | GET | 回收站：删掉的那些**单段**记录（倒序） |
| `diary/restore` | POST | `{"id"}` 从回收站还原一段（幂等） |
| `diary/card` | GET | `?book=&date=&style=` 卡片**渲染好的 HTML** + 翻页信息（`days` / `index`）；日记页「卡片」预览用 |
| `notebook` | GET | `?who=` 过滤 |
| `notebook/complete` | POST | `{"id": "..."}` |
| `notebook/delete` | POST | `{"id": "..."}`（先进 `notebook.json` 的 `trash`） |
| `affinity` | GET | 榜 + `love_peers`（只显示） |
| `history` | GET | 情绪曲线数据点；`?days=` 默认 30，上限 365 |
| `proactive` | GET | 主动消息计数 + 发送记录；`?limit=` |
| `settings` | GET | 设置页字段表：`sections` / `groups` / `fields` / `values` / `defaults` / `warnings` / `editable_paths` / `problems` |
| `settings` | POST | `{"changes": {"diary.max_chars": 3000}}`（点分路径 → 新值）；错误**逐字段**返回在 `errors` |
| `settings/reset` | POST | 恢复默认设置（无 body；默认值**只从 schema 取**；返回 `changed` 列表，先备份再落盘） |
| `portrait` | GET | 立绘：`current`（含 `data_url`，无图 null）+ `items`（只有元数据）；`?id=` 取单张（含 `data_url`） |
| `portrait/upload` | POST | multipart 字段名固定 `file`；按**魔数**收 png/jpeg/webp/gif，单张 ≤8 MB、总数 ≤20 张 |
| `portrait/select` | POST | `{"id": ...}` 切换当前立绘（响应带新的 `current` 含 `data_url`） |
| `portrait/delete` | POST | `{"id": ...}` 删除一张（连文件；删 current 落到剩余第一张，全删完 null） |

**错误约定（写死，别一处 200 一处 500）**：

- **业务结果一律 HTTP 200 + `{ok, error}`**——id 不存在、已重复完成都属此类，契约就是这么写的；
- **只有请求形状不对**（缺 `id`、body 不是 JSON 对象）才用 `error_response` + **400**；
- 后端抛异常由 `logged_handler` 统一吃掉 → `error_response` + 500，**不让异常冒出去**，同时记 `request.username` 做审计。

**真机信封（4.28.1 实测，前端照这个读）**：

| | body |
| :--- | :--- |
| 成功 | `{"ok":true,…}` —— **裸载荷**，没有外层 `data` |
| 失败 | `{"status":"error","message":"…","data":{"endpoint":"…","errors":[…],"problems":[…]}}` + **HTTP 400** |

两条别踩：

1. **一律经 `web_api/_web.py` 的 `error_response`，别直接调宿主的**。宿主签名很窄（`error_response(message, *, status_code=400, data=None, headers=None)`，`status_code` 关键字限定、不接任意 kwarg），历史上 21 处 `error_response(msg, 400, endpoint=…)` 在真机上全 `TypeError` → 连 `logged_handler` 的兜底 500 一起炸穿 → **所有错误路径全是 500**，而离线单测全绿（离线桩比真机宽）。归一层的内部签名是 `error_response(message, status_code=400, *, errors=None, problems=None, endpoint="", headers=None)`，附加信息统一进信封的 `data`。**回归测试在 `test/test_web_api_contract.py`**。
2. **400 信封里的 `data` 到不了 iframe**——官方 bridge 的失败只透出一个字符串（`plugin_page_bridge.js` `pending.reject(new Error(message.error))`）。所以 toast 用 `error.message` 没问题；**服务端的逐项错误想在前端"就地显示"，只能走「200 + `{ok:false, errors:[…]}`」的业务结果**，别指望 400 的 `data`。

### 兼容与降级

- `context` 上没有 `register_web_api`（AstrBot < 4.24.2）时**静默跳过注册**，插件本体照常在聊天里工作；
- 拿不到 bridge（直接 `file://` 打开、或 bridge 未就绪）显示"请从插件详情页打开本页面"，**不白屏**；
- 任一视图读数失败渲染可读错误框，其余 tab 照常可用。

### 设置页

设置页是 schema 驱动的：`GET settings` 把 `_conf_schema.json` **递归展开**成字段表——前端**不抄一份字段清单**，`web_api/handlers.py` 也不维护第二份映射：

- `groups`：顶层节点（保持 schema 书写顺序），组是 `object` 并带 `children`；
- `fields`：扁平叶子表，`path` 是**点分路径**（`diary.max_chars`），就是提交时 `changes` 的键名；
- 叶子带 `type`（覆盖 `bool / string / int / list / dict` 五种）、`description`、`hint`、`default`；`int` 项还带 `min` / `max`（取自 `core.settings.INT_LIMITS`，**不重抄一份**）；
- `values` 是**当前实际生效的值**（`self.settings` 那份校验后的快照，**不是**原始 config）——越界被夹紧、非法已回落的面板上都显示夹紧/回落后的值，不会骗人；
- `defaults` 是 schema 默认值，给「恢复默认值」用；`warnings` 原样带出配置降级项。

控件按类型自动选：`bool`→开关、`int`→数字框（带 min/max）、默认带换行的 `string`→textarea（作息表）、`string`→单行、`list`→一行一项、`dict`→JSON 文本域（**解析失败就地报错、绝不提交**）。底部固定「保存 / 撤销改动 / 恢复默认值」。

#### 字段提示与字符串格式

`describe_schema` 的每个 field 都带一个 `editor` 字符串，前端据此把特定字段换成更好的输入控件（**其它键一个没少**；`_conf_schema.json` 不动，`POST settings` 的载荷格式也不变——值仍然是字符串）：

| 字段 | `editor` | 前端控件 |
| :--- | :--- | :--- |
| `state.rhythm` | `"rhythm"` | 作息表编辑器 |
| `state.rhythm_weekend` | `"rhythm"` | 同上 |
| `state.late_night` | `"time_range"` | 双时间框（可跨午夜） |
| `scope.mode` | `"scope"` | 三段切换（owner / private / all） |
| 其余全部 | `""` | 按类型自动选 |

提示表住在 `core/webui_settings.py` 的 `FIELD_EDITORS` 常量里；表里写了 schema 上不存在的路径会被 `verify_schema_alignment` 报出来（启动 warning + `GET settings` 的 `problems`）。

两个自定义编辑器要解析的**字符串格式**（与 `_segment_word` / `_parse_window` 的实现一一对应）：

- **作息表**（`state.rhythm` / `state.rhythm_weekend`）：每行 `HH:MM|状态词`；
  - `#` 开头是注释行；全角 `｜` 也当分隔符；解析不了的行、或词为空的行**忽略（不报错）**；
  - **到点的最近一段生效**；**凌晨（第一段之前）归入最后一段**（跨天收尾，如 `23:00|该睡了` 对凌晨 02:00 依然有效）；
  - 行顺序无关（内部按时间排序）；**空表 = 没有作息词**，注入里就不会有作息行；
  - `rhythm_weekend` **留空 = 沿用工作日那张**，只有周六日才读它。
- **深夜窗**（`state.late_night`）：`HH:MM-HH:MM`，**可跨午夜**（如 `23:30-06:30`，判 `分钟≥起点 或 <终点`）；起止相同或留空 = 永不深夜；解析不了 = 永不深夜（不报错）。

#### 校验准绳是 `load_settings` 的 warnings

面板**不复刻**一套配置规则——`core/settings.py` 才是配置的**唯一解释者**（越界夹紧、非法回落、正则零宽拦截都在那儿）。所以流程是：

1. `changes` 合并进当前 config 的**深拷贝** → 未知点分路径 / 只读项 / 类型不符 → **整单拒绝，一个字节都不写**；
2. `core.settings.load_settings(前后各一次)` → **新增**的 warning 必然来自被改的键 → 整单拒绝；
3. 过了才落盘。既有 warning **不**算新问题。

`POST settings` 的校验分两层，错误**逐字段**返回（`errors: [{"path", "error"}]`）：

1. **类型对齐**（`coerce_value(field, raw) -> (value, error)`，`review_changes` 逐项收集**全部**错误）：未知路径 / 只读项 / 类型不符（bool 冒充 int、字符串冒充 bool 在这里就拒）；取值范围**不在这层查**（那是 `load_settings` 的活，两层各写一半规则必然漂移）。有任何错误 → **400 + `errors` 逐字段 + 一个字节不写**。
2. **唯一准绳**（`verify_settings`）：`load_settings` 前后各跑一次，新增 warning → 400 + `errors`（`path` 从 warning 文案开头的字段路径提取，整体性的挂空 path）+ `problems` 原样。

合法响应在既有 `applied` 之外加了一个别名 `changed`（与 `applied` 同值）。

**恢复默认（`POST settings/reset`，无 body）**：默认值**只从 schema 取**（`defaults_by_path`），只回**可编辑且真的变了**的项（`data_dir` 永不进 reset）；无变化 → `{ok, changed: []}` 且**不落盘、不备份**；有变化 → 走与保存完全相同的链。

**错误口径**：

| 情况 | 响应 |
| :--- | :--- |
| body 不是 JSON 对象 / 缺 `changes` / `changes` 为空 | **400** |
| 未知点分路径、只读项、类型不符、组路径本身 | **400** |
| 值被 `load_settings` 判为非法（越界/坏正则/坏时区/零宽） | **400** + `problems` 逐条列出，**不写盘** |
| 备份或落盘失败 | 200 + `{ok: false, error}` |

#### 落盘、热生效与备份

写盘走插件**自己的活配置对象** `self.config`（`AstrBotConfig` 是 dict 子类，构造时注入）：`clear()` 后 `save_config(updated)`；`save_config` 不可用时回落 `update()`。**不走** Dashboard 的 `/api/v1/plugins/{id}/config`——那条路是"写盘 + 热重载插件"，会把整个插件重启掉。

成功落盘后**就地热生效**（`main.py` 的 `apply_settings`）：

- **就地重建 `self.settings`**，并把 Diary / Notebook / State / Affinity / Proactive 五个门面的 `settings` 引用一起换掉——门面在构造时各拿了一份快照，只换 `self.settings` 等于"改了不生效"；
- `proactive.patrol_minutes` 变了 → 重建巡检 job（老规矩：按 name 删旧再 `add_basic_job` 新建）；
- `timezone` 变了 → **也重建**：判定用 `settings.zone()` 每次现算没错，但 job 上的 `timezone` 是建的时候那个，不重建就是"调度按旧时区、判断按新时区"两套真相。⚠️ 说实话：`*/N * * * *` 这种纯分钟步长的**触发时刻与时区无关**（整小时偏移下完全一样），所以真危害只是面板上那个 job 的时区显示是旧的——这是一次"对齐"，不是修了个严重 bug。

**安全**：写盘前把旧配置文件整份备份到数据目录 `config_backup_<YYYYmmdd-HHMMSS>.json`；备份失败**就整单放弃**（备份是安全网，备份不下来时"这次没改成"好过"配置写坏"）；落盘失败回落到内存旧值并回报错误。写操作全程 `try/except` + `logged_handler`，记 `request.username` 审计。

#### 只读项与大类树

- **`data_dir` 只显示不可改**：改它＝搬走全部日记与本子，Layout 在插件启动期就建好了。返回结构化错误说明原因，要改请走原生插件面板 + 重载插件。
- **`enabled`（总开关）允许改**，但响应里会带回提示：关掉后日记、小本本、状态、主动消息全部不可用（数据保留）。
- `love_peers` 与 `name_preference` **都能改**。
- `GET settings` 的 `sections` 是一棵**展示树**：大类（`基础 / 内容 / 主动消息 / 出站`）→ 分组（`_conf_schema.json` 的 13 个顶层键）→ 组内叶子的点分路径。归属写在 `core/webui_settings.py` 的两张常量表里（`CONFIG_GROUPS` 声明分组与展示顺序、`CONFIG_SECTIONS` 声明大类），**前端只按树渲染，不写死分组**；归属写错/漏分组由 `verify_schema_alignment` 报出来。

#### schema 对齐自检

`verify_schema_alignment(schema) -> [problems]`：`_conf_schema.json` 与 `core/settings.py` 默认值表**双向**核对——顶层键集合、组内键集合、`dict` 叶子与分组的形状、`int` 项有没有 `INT_LIMITS` 取值范围、大类归属是否覆盖/越界。`initialize()` 启动时跑一次，有就**逐条写 warning 日志**；`GET settings` 的 `problems` 数组原样带出。AstrBot 会剔除 schema 里不存在的键，两边不一致时用户保存的值会在重载时静默丢失——这就是自检必须存在的原因。

## 元数据（metadata.yaml）

字段集与顺序以工作区 `dev-examples/metadata.example.yaml` 为准：必填 `name` / `display_name` / `short_desc` / `desc` / `version` / `author` / `repo`，可选 `astrbot_version` / `support_platforms` / `tags`。宿主只解析这几个键，多余字段不认——`license` 就因此删掉了，许可证以仓库根的 `LICENSE`（MIT）为准。本仓定稿：

- `astrbot_version: ">=4.24.2"`：`TextPart(...).mark_as_temp()` 临时内容块注入的最低版本（< 4.24.2 本来也注册不了插件页 API）。不满足时宿主拒绝加载（WebUI 安装可「无视警告」跳过）——宁可让版本不够的人明确装不上，也不悄悄退回拼 `system_prompt` 的降级路径（上架审核第二条要治的病）。
- `support_platforms: [aiocqhttp]`：只声明真机验证过的平台；该字段只作展示不拦载，测过新平台再往里加。
- `author: "xiaoxi2760"`（git 名，与仓库 owner 一致）；`metadata.yaml` 与 `main.py` 的 `@register` 第二参两处同步。若值是纯数字形式，必须带引号：裸写会被 YAML 解析成 int，宿主校验要求字符串，加载直接报「插件元数据校验失败」。
- `short_desc` 是市场卡片短描述，缺省回退 `desc`；`tags` / `social_link` 只被插件市场源 JSON 消费，宿主本体不读。

## 品牌化与立绘

插件显示名是**情绪日记**（`metadata.yaml` 的 `display_name`）。注意 `name:` 仍是 `astrbot_plugin_denia_diary`——它同时是**目录名与模块名**，改它等于换安装目录，已经装过的用户点「更新」会变成又装一份。仓库名同理。

- **logo**：插件**根目录**的 `logo.png`（256×256）。宿主只在根目录找这个固定文件名（`star_manager` 的 `logo_fname`），认到之后详情页回 `/api/file/<token>`。参考插件的做法是 256×256 方形人脸裁切。
- **左上角标题**：配置 `panel.brand` / `panel.brand_sub`。默认 `情绪日记` / `观察面板`；**留空串＝那一行不显示**（空串是合法值，不是"没填"）。超 60 字在读取时就地截断并记 warning；从面板保存超长会被整单拒绝。前端只认后端回的值，`index.html` 里那份字面量是"拿不到配置时"的兜底。
  ⚠️ 藏那两行要靠 CSS 的 `[hidden]` 兜底——`.brand` 是 `display:flex`，UA 的 `[hidden]{display:none}` 特异性不够，光设 `hidden` 属性藏不住（真浏览器检查抓到的）。
- **不打包任何立绘**：没图时首页显示「这里可以放图」占位；图只在用户自己传过之后才出现。离线预览想空着看：`pages/diary/index.html?preview=1&portrait=empty`。

### 立绘上传

存储在数据目录 `portraits/`，**文件名服务端生成** `p_<12 位 hex>.<ext>`——用户原始文件名只作展示来源且存净化版（去目录、去危险字符、限长 60），路径穿越无从谈起。同目录 `index.json` 记 `{"current", "items"}`（每项 `{id, name, mime, bytes, created_at}`）。

- `GET portrait` → `current`（含 `data_url`，可直接塞 `<img src>`）+ `items`（只有元数据——单张 8 MB 的 base64 不允许乘以张数塞进一个响应）；一张都没有是 200 + `{"current": null, "items": []}`；
- `GET portrait?id=` → 单张含 `data_url`；`POST portrait/select` 响应直接带新的 `current`，前端切换不必再发一次请求；`POST portrait/delete` 连文件一起删。

**安全与校验（官方"不要信任 Page 传来的路径、文件名、格式或数值范围"）**：

- 格式按**魔数**判定（PNG / JPEG / GIF87a|89a / RIFF+WEBP），`content_type` 一概不信，扩展名由魔数定；
- 单张 ≤ **8 MB**、总数 ≤ **20 张**、请求体 `content_length` **预检**（超大直接 400 不读进内存），能设 `request.max_content_length` 就设（宿主没有这属性就 try/except 兜住）；
- 违规 → **400 且一个字节不落盘**。

**落盘纪律**：`index.json` 走 `storage.atomic_write_text` + `KeyedLocks`；**先落图再写 index**，写 index 失败**回滚删图**；删除连文件一起删；`GET` 时"index 有、盘上没文件"的条目跳过并顺手清 index（自愈，current 被清就落到剩余第一张）。

**宿主兼容**（照 meme_manager 的垫）：`PluginUploadFile.save` 在不同 AstrBot 版本上同步 / 异步不一样——`inspect.iscoroutinefunction` 为真就 `await`，否则 `await asyncio.to_thread(save, path)`；走「save 到临时文件再读字节」，不赌 `read()` 的兼容面。上传对象以鸭子类型进 core（`core/` 依旧零 astrbot import）。

## 存储契约

数据目录由宿主在运行时给出（AstrBot：`StarTools.get_data_dir("astrbot_plugin_denia_diary")`）。

| 文件 | 用途 | 格式 |
| :--- | :--- | :--- |
| `日记.jsonl` | 日记正文（**权威存储**，第 18 步） | JSONL，一行一段，键序冻结见 `format.ENTRY_LINE_KEYS` |
| `恋爱日记.jsonl` | 恋爱日记正文（权威存储） | 同上 |
| `日记.txt` | 旧纯文本日记：首次访问时**一次性导入** JSONL，之后原样保留当人工可读旧档，不再读写 | 纯文本，无版本头 |
| `恋爱日记.txt` | 同上（恋爱日记那本） | 纯文本，无版本头 |
| `notebook.json` | 小本本（约定型 / 事实型 + 回收站） | JSON + `schema_version` |
| `state.json` | 情绪当前值（全局一条，不按人分） | JSON + `schema_version` |
| `state_history.jsonl` | 情绪历史（面板情绪曲线的唯一数据源） | JSONL 追加文件，一行一个 JSON，无版本头 |
| `affinity.json` | 熟悉度（按人一个漏积分器标量） | JSON + `schema_version` |
| `proactive.json` | 主动消息状态（`sessions` + `contacts`） | JSON + `schema_version` |
| `proactive-log.jsonl` | 主动消息发送记录（**确认发出后**才追加；保留最近 200 行或 90 天） | JSONL 追加，无版本头 |
| `greetings.json` | 早晚安记录（按人事件流；连续天数 / 睡眠时长是**派生值不落盘**） | JSON + `schema_version` |
| `names.json` | id → 昵称 / 群名缓存 | JSON + `schema_version` |
| `diary-nudge.json` | 日记提醒冷却（按会话） | JSON + `schema_version` |
| `diary-trash/` | 改/删日记前的原文备份（整本快照 `*.jsonl`）+ 单段归档（`seg-*.json`） | JSONL 快照 / JSON 记录 |

`notebook.json` 形状（`schema_version` 由 `atomic_write_json` 自动补；`id` 为 12 位 hex，生成后不变，面板可引用）：

```json
{
  "schema_version": 1,
  "facts": { "<peer_id>": [ { "id": "...", "text": "...", "created_at": "ISO8601", "updated_at": "ISO8601" } ] },
  "promises": [ { "id": "...", "text": "...", "about": "<peer_id>", "created_at": "ISO8601", "due_at": "YYYY-MM-DD 或 null", "done_at": "ISO8601 或 null" } ],
  "trash": [ { "kind": "fact 或 promise", "deleted_at": "ISO8601", "entry": { "…被删的原条目…" } } ]
}
```

`state.json` 形状（作息是"config 表 + 当前时间"的纯函数**不落盘**，这里只存情绪）：

```json
{
  "schema_version": 1,
  "mood": {
    "word": "有点烦",
    "valence": 0,
    "arousal": 0,
    "updated_at": "ISO8601",
    "word_at": "ISO8601",
    "baseline": { "valence": 0, "arousal": 0, "updated_at": "ISO8601", "day": "YYYY-MM-DD", "moved_valence": 0.0, "moved_arousal": 0.0 }
  }
}
```

`affinity.json` 形状（`day` / `gained_today` 是"单日增量封顶"的记账键）：

```json
{
  "schema_version": 1,
  "people": { "<person_id>": { "score": 0.0, "last_ts": "ISO8601", "day": "YYYY-MM-DD", "gained_today": 0.0 } }
}
```

（骨架键以框架方案为准——`word_at` / `day` / `gained_today` / `moved_*` 是"可加"的记账键，键名只增不改。）

`state_history.jsonl` 形状（**文件路径与行形状就是接口**，黑盒验收直接读文件）：

```json
{"ts": "2026-10-05T17:12:03+08:00", "layer": "now", "valence": -1, "arousal": 2, "word": "有点烦躁"}
```

三条写入规则（落进 `core/state/store.py` 的 `append_history` 与 `core/state/api.py` 的 `observe`）：

1. **追加型，不塞进 `state.json`**：`state.json` 里没有任何 history 字段（当前值与时间序列分文件）。
2. **坐标真的变了才追加**：`observe()` 里坐标动了写 `layer=now` 行、基调真挪了写 `layer=baseline` 行；**同值重复自报不写**——只刷新 `state.json` 的时间戳；只报词不动坐标也不写。
3. **保留最近 180 天**：模块常量 `HISTORY_KEEP_DAYS = 180`（不做配置项）。追加时发现最老行超 180 天 → 整文件重写一次（唯一重写时机，走 `atomic_write_text`）；正常路径是单行纯追加（`open("a")` + fsync），没有写放大。

七条约定：

1. **原子写**——同目录临时文件 → `os.replace`；Windows 上对目标被占用做有限重试；失败不留半截文件。
2. **编码与换行**——UTF-8（无 BOM）；行尾恒为 `\n`。写文本一律 `newline=""`，禁止 Windows 把 `\n` 翻成 `\r\n`（日记格式以 `\n` 为不变式）。
3. **读取**——按 `(mtime_ns, size)` 失效的进程内文本缓存；文件被外部进程原子替换后自动感知，不需要重启插件，也不每次请求重复解析。
4. **JSON 信封**——顶层必带 `schema_version`（当前为 1）；版本比本程序**新**时按原样使用、**绝不改写**；版本比当前旧且缺迁移函数时原样使用并记警告。全程不抛异常。
5. **日记正文不带版本头**——它的"schema"是头行正则，与写入侧同源。
6. **文件权限**——原子写用 `mkstemp`，默认 0600（Windows 忽略该位）；需要跨用户读取时自行调整。
7. **进程内串行**——每文件一把 `asyncio.Lock`（`KeyedLocks`），**只保护本进程**；面板是外部进程，靠"它也原子写 + 我们按 mtime 重读"协作。

## 配置表

`_conf_schema.json` 与 `core/settings.py` 一一对应。校验策略：**越界夹紧、非法回落默认、未知项忽略**，原因写进 `Settings.warnings`，不抛异常。

| 字段 | 默认 | 范围 |
| :--- | :--- | :--- |
| `enabled` | true | 总开关；关闭时所有子系统不可用 |
| `timezone` | `Asia/Shanghai` | 任何 IANA 时区；无效则回落 |
| `data_dir` | 空 | 空 = 用宿主给的目录；非空 = **只接受宿主插件数据目录本身或它里面的子目录**，越界（含绝对路径指别处）启动时记 warning 并退回默认目录 |
| `panel.brand` | `情绪日记` | 面板左上角标题（也是浏览器标签页标题）；留空＝那行不显示；>60 字截断 |
| `panel.brand_sub` | `观察面板` | 标题下面那行小字；留空＝那行不显示 |
| `subsystems.{diary,notebook,state,proactive,greeting}` | 全 true | 子系统开关 |
| `diary.edit_within_days` | 7 | 1–60 |
| `diary.max_chars` | 1200 | 200–20000 |
| `diary.nudge_after_hour` | 22 | 0–23 |
| `diary.soft_nudge_after_hour` | 19 | 0–23 |
| `diary.event_hint_cooldown_min` | 30 | 5–1440 |
| `diary.fallback_hint_cooldown_min` | 90 | 15–1440 |
| `diary.exchange_window_min` | 30 | 5–1440 |
| `diary.card_render` | `pretty` | 精美卡片 / 本机朴素图 / 文本。远端路径统一受 `diary.card_allow_remote` 控制，默认禁止；朴素档始终只走本机 PIL。 |
| `diary.card_style` | `paper` | pretty 档的模板款式：`paper`（纸感，面板同款）/ `ink`（墨信）/ `postcard`（明信片）；非法回落 paper |
| `notebook.promise_limit` | 20 | 1–200；约定上限（每人，按未完成计） |
| `notebook.fact_limit` | 15 | 1–200；事实上限（每人） |
| `state.rhythm` | 5 段默认表 | 每行「HH:MM\|状态词」；到点的最近一段生效，凌晨归入最后一段；留空＝不出作息行 |
| `state.rhythm_weekend` | 空 | 周六周日生效；留空＝与工作日同一张 |
| `state.late_night` | `23:30-06:30` | 深夜时段（可跨午夜）；留空＝永不深夜；非法值恒 False |
| `scope.mode` | `private` | `owner` / `private`（默认）/ `all`；非法值回落 `private` 并记 warning |
| `love_peers` | `[]` | 手工指定的"最亲密"名单（去空、去重、保序） |
| `name_preference` | `{}` | `{id: 称呼}`，空称呼丢弃 |
| `proactive.patrol_minutes` | `15` | 巡检间隔（1-59）；basic job 按 `*/N * * * *` 重建 |
| `proactive.daily_limit_private` | `2` | 私聊每日上限（1-10） |
| `proactive.daily_limit_group` | `1` | 群聊每日上限（0-10，0＝群里绝不主动） |
| `proactive.min_interval_minutes` | `120` | 同会话最小间隔（0-1440） |
| `proactive.signal_cooldown_days` | `3` | 暗号独立冷却（1-30） |
| `proactive.signal_enabled` | `true` | 心情暗号总开关（第 20 步）。**关掉就一个候选都不产**，也不是"产了再拦"——连日志与冷却都不留痕，免得用户以为开关没生效 |
| `proactive.pending_window_hours` | `6` | 待回窗口（1-48），喂 `trace_line` |
| `proactive.quiet_days` | `3` | 久未联系阈值（1-30，待调） |
| `proactive.signal_char` | `。` | 暗号字符（1~4 个字符） |
| `greeting.duplicate_minutes` | `30` | 0-1440；同款问候的重复冷却（0＝不限，当日首次为准仍生效） |
| `greeting.morning_patterns` | 5 条默认词表 | 正则列表，**整条消息**完全匹配才算；坏条目忽略，全空回落默认 |
| `greeting.night_patterns` | 6 条默认词表 | 同上 |
| `voice.mode` | `off` | 主动消息的语音出口：`off`（默认）/ `voice`（只发语音）/ `dual`（语音在前文字在后）；非法回落 off |
| `voice.probability` | `100` | 0-100；开口概率，情绪不好（valence<0）自动上浮（core.voice 常量 +30pp，待调） |
| `outbound.strip_meme_marks` | `true` | 出站剥表情包标记总开关 |
| `outbound.marker_pattern` | `&&[^&\s]{1,24}&&` | `re` 语法；非字符串 / 编译失败 / **能匹配空串** 三种情况都回落默认 |

熟悉度的衰减与档位参数（λ / Δ / 单日封顶 / 阈值）与主动消息的判定阈值（暗号 valence 阈值 -0.7、劝睡 / 劝饭 / 早晚安 / 日记驱动的时段窗口）是**代码常量**（`core/state/affinity.py`、`core/proactive/`），不进配置面板——它们是算法的一部分，不是用户该调的旋钮。

## 测试

```powershell
$env:PYTHONDONTWRITEBYTECODE=1; $env:PYTHONUTF8='1'
py -m unittest discover -s test -t . -v
py tools/webui_selfcheck.py
```

- 临时目录默认落在 `test/.tmp`（已 gitignore），可用环境变量 `DIARY_TEST_TMP` 覆盖；
- **不写系统 TEMP、不用 `mkdtemp`**：受限环境下系统 TEMP 可能不允许建嵌套目录，而 `mkdtemp` 在部分沙箱下建出的目录自身权限异常；
- 内核不 import astrbot，因此内核测试不需要任何桩。

`tools/webui_selfcheck.py` 查：页面存在、资源齐全且全相对、无 CDN / 无 `../`、`core/` 零 astrbot import、**后端路由表 ↔ 前端 endpoint 双向一一对应**、新文件 UTF-8 无 BOM + 纯 LF。改了一边忘了另一边就在这里报错。

**离线看界面**（不必起 AstrBot）：直接用浏览器打开 `pages/diary/index.html`——`preview-bridge.js` 只在 `file://` 或 `?preview=1` 时装假 bridge，正式 iframe 里它什么也不做。

## 来源

| 部分 | 来源 |
| :--- | :--- |
| 行为约定（头行格式、四种读模式、改删边界、提醒与冷却策略、心情词表、面板筛选） | `qq-bridge-diary`（原包 v1.0，JS/ESM）——逐条对齐 |
| 头行正则 `HEAD_RE` 与 `TAG_MAX=40` | 逐字取自原包的 `DIARY_HEAD_RE` |
| 4 个日记工具的描述文案 | 改编自原包的 `mcp/diary-tools.snippet.js`（去掉 `key`/`token` 参数：AstrBot 的 event 自带会话身份） |
| Python 实现、存储契约、`core/` 分层、适配器、测试 | 本仓新写 |
| 面板视觉手法（仪表条、设计令牌、bento 网格） | [`astrbot_plugin_daily_life`](https://github.com/siciyuanweilai/astrbot_plugin_daily_life)（MIT，作者 四次元未来）——**只取手法不取体量** |

原包自带的实测 bug 复盘（P1-a / P1-b / P2-b / P3-b）已在本仓转成回归测试，见 `test/test_diary.py`。

## 许可证

本仓代码以 MIT 协议开源（`LICENSE`）。

## 不做

- 安装器 / 往别人的文件里插接线（做成真插件后不需要）；
- 消息启发式的自动情感分析、自动记笔记（噪音毁信任）；
- 签到 / 打卡 / 积分 / 熟悉度游戏化；
- 独立的待办备忘系统（并入日记）。

## 1.7.0 陪伴连续性与发送校验

- 发送前钩子仅清洗与转语音；发送后钩子精确匹配宿主成功文案中的会话，才确认送达。失败仍保留决策素材，单事件去重。
- `diary.card_allow_remote=false` 为默认值；出图编排和精美卡片文字转图兜底均受它控制，本机不可用时返回文本。两种隐私策略分开缓存。
- 曲线使用真实日期横轴，独立绘制当下与基调，坐标范围 -2~2，缺测不补零；日期点击通过 `ctx.openDiaryDate` 打开日记。
- 小本本完整存储及工具查询不变，只有提示按话题、近期更新和到期程度选取完整条目，并为状态与互动轨迹预留预算。被动轮使用当前消息，主动轮使用候选素材作为查询词。
- `proactive.json.diagnostic` 保存最近一次巡检或发送结果，不保存正文；诊断计算/写盘失败不影响派发。
- `comments.json` 为旁挂批注文件。面板的 `diary/comments`、`save`、`publish`、`withdraw`、`delete` 五条接口分别列出、存草稿、发布、撤回和归档。草稿不会注入，已发布批注仅交给源归属人的私聊一次；再次校验启用范围、恋爱名单和源段落内容地址。被动轮批注与其它动态内容合计不超过 400 字，主动轮追加最多 100 字批注素材。
