# 情绪日记（astrbot_plugin_denia_diary）

> 日记和主动消息插件，可让 bot 写日记并记录日程、好感之类的。

给 bot 一本自己的纯文本日记，外加小本本（事实与约定）、状态（情绪 / 作息 / 熟悉度）
与主动消息；数据全部落在**可读的文件**里（日记是纯文本），内核零第三方依赖、可离线测试。
自带 WebUI 观察面板：总览 / 日记 / 小本本 / 熟悉度 / 曲线与主动 / 数据 / 设置。

## 安装与更新

- **装**：把 `astrbot_plugin_denia_diary-<版本>.zip` 上传到 AstrBot 的插件管理页，或在
  `data/plugins/` 下解包（解出来必须是 `data/plugins/astrbot_plugin_denia_diary/`，别多一层目录）。
- **更新**：`metadata.yaml` 的 `repo:` 指向
  <https://github.com/xiaoxi2760/astrbot_plugin_denia_diary>（**公开库**）。AstrBot 靠这个字段
  才能在插件列表里点「更新」——不填 `repo` 就没有这条路。
- 要求 AstrBot `>= 4.24`（`astrbot_version`）。
- 首次打开面板：Dashboard → 插件管理 → 情绪日记 → 插件页。

## 先看这三件事（面板）

1. **默认只在私聊工作**（配置 `scope.mode = private`）：群聊里不记录、不注入、不主动。
   想放到群里（或反过来只认主人）就点顶栏那三段开关，**点一下即时生效**。
   面板本身永远可用，切哪一档都能看能改。
2. **首页立绘要自己传**：包里不带任何图（也不该替用户决定放谁的照片），
   没图时那块写着「这里可以放图」——点右边的「选择文件」传一张就行。
3. **左上角标题是你自己的**：设置页 → 基础 → 面板外观，改 `面板标题` / `面板副标题`，
   保存后当场生效（留空＝那一行不显示）。

## 当前进度

| 步 | 内容 | 状态 |
| :--- | :--- | :--- |
| 0 | 存储契约（`core/storage.py` + `core/settings.py`） | ✅ 已交付 |
| 1 | 日记系统（内核 + 适配器） | ✅ 已完成（真机已验） |
| 2 | 小本本（两类条目 + 上限 + 注入 + 可见性） | ✅ 已完成（真机已验） |
| 3 | 状态系统（情绪 / 作息 / 熟悉度） | ✅ 已完成 |
| 4 | 主动消息（七触发器 + 闸门 + 两段式 + 暗号） | ✅ 已完成 |
| 5 | WebUI 观察面板（Dashboard 插件页） | ✅ 已完成（真机已验：页面 + 接口全 200） |
| 5.1 | 出站文本清洗（表情包标记不再漏给用户） | ✅ 已完成 |
| 5.2 | 全部配置项搬进 WebUI 设置页（schema 驱动、改完即生效） | ✅ 已完成（真机已验设置读写 + 一键恢复默认） |
| 5.3 | 立绘上传（用户自上传 + 单图展示 / 切换） | ✅ 已完成（真机已验 22/22：真 multipart / 真 `save` / 落盘 / 拒绝路径） |
| 5.4 | 危险区「一键恢复全部默认」+ 两步确认 | ✅ 已完成（真机已验 10/10：改 → 落盘 → 热生效 → 恢复 → 回默认 → 空操作） |
| 6.1 | 日历 / 单页日记本的数据面（`days` 清单 + 结构化 `entries` + `editor` 提示） | ✅ 后端已完成（前端照 README 契约接） |
| 7 | 启用范围总闸（owner / private / all 三档；记录、注入、写工具与主动对象全跟档位走） | ✅ 后端已完成 |
| 8 | 「对谁」显示名字（contacts 落昵称 + 名字(数字) 显示串 + 候选跟档位标注） | ✅ 后端已完成（前端照契约接） |
| 9 | 面板文案与主次（全中文、大字给中文状态、数值降为副行） | ✅ 已完成（真浏览器 11/11 零报错） |
| 10 | **1.0 品牌化**：改名「情绪日记」+ logo.png + 标题自定义（`panel.brand`）+ 默认立绘换成「这里可以放图」占位 | ✅ 已完成（真机 20/20，含宿主认到 logo） |

当前 **664 项单测全绿**（第 5.2 步 60 + 「后端对齐」47 + 「立绘后端」30 + 「宿主契约」7 + 第 6.1 步数据面 11 + 第 7 步范围闸 34 + 第 8 步名字 10）。WebUI 面板由插件侧提供（`web_api/` + `pages/diary/`）：
**读走 store 层、不走门面**（面板是主人视角，不受聊天可见性规则约束），
写只走 store 的原子写方法。数据面在 `core/webui_data.py`（纯函数、可离线单测），
设置页的数据面在 `core/webui_settings.py`（schema 递归展开 + 以 `load_settings` 的 warnings 为准绳）。

| 步 | 内容 | 状态 |
| :--- | :--- | :--- |
| 0 | 存储契约（`core/storage.py` + `core/settings.py`） | ✅ 已交付 |
| 1 | 日记系统（内核 + 适配器） | ✅ 已完成（真机已验） |
| 2 | 小本本（两类条目 + 上限 + 注入 + 可见性） | ✅ 已完成（真机已验） |
| 3 | 状态系统（情绪 / 作息 / 熟悉度） | ✅ 已完成 |
| 4 | 主动消息（七触发器 + 闸门 + 两段式 + 暗号） | ✅ 已完成 |
| 5 | WebUI 观察面板（Dashboard 插件页） | ✅ 已完成（真机已验：页面 + 接口全 200） |
| 5.1 | 出站文本清洗（表情包标记不再漏给用户） | ✅ 已完成 |
| 5.2 | 全部配置项搬进 WebUI 设置页（schema 驱动、改完即生效） | ✅ 已完成（真机已验设置读写 + 一键恢复默认） |
| 5.3 | 立绘上传（用户自上传 + 单图展示 / 切换） | ✅ 已完成（真机已验 22/22：真 multipart / 真 `save` / 落盘 / 拒绝路径） |
| 5.4 | 危险区「一键恢复全部默认」+ 两步确认 | ✅ 已完成（真机已验 10/10：改 → 落盘 → 热生效 → 恢复 → 回默认 → 空操作） |
| 6.1 | 日历 / 单页日记本的数据面（`days` 清单 + 结构化 `entries` + `editor` 提示） | ✅ 后端已完成（前端照 README 契约接） |
| 7 | 启用范围总闸（owner / private / all 三档；记录、注入、写工具与主动对象全跟档位走） | ✅ 后端已完成 |
| 8 | 「对谁」显示名字（contacts 落昵称 + 名字(数字) 显示串 + 候选跟档位标注） | ✅ 后端已完成（前端照契约接） |

当前 **664 项单测全绿**（第 5.2 步 60 + 「后端对齐」47 + 「立绘后端」30 + 「宿主契约」7 + 第 6.1 步数据面 11 + 第 7 步范围闸 34 + 第 8 步名字 10）。WebUI 面板由插件侧提供（`web_api/` + `pages/diary/`）：
**读走 store 层、不走门面**（面板是主人视角，不受聊天可见性规则约束），
写只走 store 的原子写方法。数据面在 `core/webui_data.py`（纯函数、可离线单测），
设置页的数据面在 `core/webui_settings.py`（schema 递归展开 + 以 `load_settings` 的 warnings 为准绳）。

## 目录

```
astrbot_plugin_denia_diary/
├─ metadata.yaml / _conf_schema.json / README.md
├─ main.py              适配层之一：9 个工具 + 提示挂载点 + 生命周期 + WebUI 注册两行
│                         （另一个适配层是 web_api/；`astrbot.api.web` 的**唯一**导入点在 web_api/_web.py）
├─ web_api/             WebUI 的 HTTP 适配层（第 5 步；在 core/ 之外）
│  ├─ __init__.py       对外只暴露 register_all
│  ├─ _web.py           astrbot.api.web 的唯一导入点（离线时降级成桩）
│  ├─ routes.py         路由表 + 表驱动 register_all（含 hasattr 兼容守卫）
│  └─ handlers.py       薄壳 handler：解析 request → 调 core 纯函数 → json_response
├─ pages/diary/         插件页（第 5 步）：单目录自包含，零构建零依赖
│  ├─ index.html        左导航五 tab + stage 骨架 + bridge 缺失守卫
│  ├─ app.js            bridge 就绪 / endpoint 封装 / 视图切换 / "对谁"选择器
│  ├─ ui.js             h / clear / toast / 格式化 / 手写 SVG 折线
│  ├─ style.css         自写样式（不引外部 CSS）
│  ├─ preview-bridge.js 离线预览用的 stub bridge（只在 file:// 或 ?preview=1 生效）
│  └─ views/            五个视图：overview / diary / notebook / affinity / proactive
├─ tools/
│  └─ webui_selfcheck.py 静态对账：路由表 ↔ 前端 endpoint 双向一一对应 + 资源与编码体检
├─ core/                内核：零 AstrBot 依赖，可离线测试
│  ├─ settings.py       配置默认值、取值范围、校验与降级
│  ├─ storage.py        存储契约：目录布局 / 原子写 / mtime 缓存 / schema_version
│  ├─ compose.py        状态渲染（唯一出口）：按优先级拼段 + 注入预算（400 字、五类）
│  ├─ session.py        会话身份（umo → 标注 / 分流 / 可见性的索引；person_id 收口"眼前人"）
│  ├─ diary/
│  │  ├─ format.py      格式契约：头行正则 / 分段 / 保尾换行 / 改删的纯文本手术
│  │  ├─ store.py       文件层：两本的位置 / 追加 / 局部替换 / trash 备份 / 每文件一把锁
│  │  └─ api.py         语义入口：read_for / write_for / edit_for / list_days / prompt_line / event_hint
│  ├─ notebook/
│  │  ├─ store.py       文件层：notebook.json 读写 / 每类上限（锁内判定）/ trash 回收站
│  │  └─ api.py         语义入口：note_add / note_complete / note_forget / note_list / facts_for /
│  │                     promises_for / promises_due / promise_line / fact_line
│  └─ state/
│     ├─ store.py       文件层：state.json / affinity.json 的读写（更新闭包整体关进锁内）
│     ├─ affinity.py    熟悉度：漏积分器（懒衰减）+ 粗档位 + 互动轨迹
│     └─ api.py         State 语义入口：observe / mood_line / rhythm_line / is_late_night / snapshot
├─ core/proactive/      主动消息（第 4 步）：三件套互不知情
│  ├─ store.py          proactive.json 的写穿读写（决策 15min/确认更低频，无写放大，不做合并写）
│  ├─ gate.py           闸门：配额 / 免打扰 / 最小间隔 + 暗号独立四道闸（先闸门后内容）
│  ├─ triggers.py       七触发器（候选 = 说给谁 + 原文片段 + 优先级；有内容才发；群聊克制）
│  └─ api.py            Proactive 门面：patrol 巡检决策 / 两段式写入 / note 组装 / contacts
└─ test/                单测（放插件根下；运行加 PYTHONDONTWRITEBYTECODE=1）
   ├─ support.py        公共设施（不被 discover 收集）
   └─ test_{settings,storage,session,diary,compose,notebook,state,proactive,main}.py
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
| `mood_report` | 心情自报（`word` 原词 + 可选 `valence` / `arousal` 字符串打分）；**没有 affinity_\* 工具**，她不碰熟悉度数值 |
| `on_llm_request`（priority=5） | 先 `affinity.touch`（私聊 private / 群聊 mention）+ `proactive.note_contact`（person→会话反查表），再把 `core.compose` 渲染的一段追加进 `system_prompt`；**主动轮不触发它**，那边走 `payload.note` |
| `on_using_llm_tool` | **确认点**：主动唤醒路径里她真调了 `send_message_to_user`（事件带 `cron_job` extra）→ 写 `last_sent_at`；被动轮同一工具不算 |
| `initialize` / `terminate` | 生命周期。`initialize()` 重建巡检 job（basic handler 只在内存注册表，热重载即失效 → 按 name 查旧 job 一律 delete 再 add）；`terminate()` 冲熟悉度合并写窗口（proactive.json 是写穿，无内存态） |

工具描述是**行为规范**（"别写成工作汇报"、"开头那行不动"、"只记两类"、"闲聊情绪玩笑不记"），不是注释——改文案等于改她的行为。

## 日记系统（第 1 步）

三层，职责不重叠：

| 层 | 文件 | 只做这些 |
| :--- | :--- | :--- |
| 格式契约 | `core/diary/format.py` | 头行正则、分段、**改/删的纯文本手术**、渲染。无文件 IO、无配置、不认识平台 |
| 文件层 | `core/diary/store.py` | 两本的位置、追加、局部替换、改删前备份、每文件一把 `asyncio.Lock` |
| 语义入口 | `core/diary/api.py` | `read_for` / `write_for` / `edit_for` / `prompt_line` / `event_hint`；**`enabled` 与可见性判定都在这里** |

六条已落地的约定（原包实测过的坑，见 `qq-bridge-diary-v1.0-分析报告.md` 的 P 编号）：

1. **写入侧与解析侧同源**：标注一律经 `format.make_head_line()` + `TAG_MAX=40` 截断，超长不再让整段消失（P1-b）。
2. **读取入口唯一**：恋爱日记只对 `love_peers` 名单内那个人的私聊可见；群聊与其他人读不到（P1-a）。
3. **`enabled=false` 时读也读不到**（原包只挡了写，P2-b）。
4. **群聊标注带显式 `群·` 前缀**：读取侧不再靠"以『和』开头"猜（旧条目仍兼容旧启发式）。
5. **头行是历史不可改**，改只换正文；改/删**先备份原文**到 `diary-trash/`。
6. **未来日期与"太旧"口径一致**：`list` 说不可改的，`edit` 也不会改（P3-b）。

时间统一走 `settings.zone()`（默认 `Asia/Shanghai`），禁止裸 `datetime.now()`。

## 小本本（第 2 步）

**只记重要的**，两类条目（方案 §4.2）："日常闲聊、情绪、玩笑不记"由工具描述声明 + 数量硬上限 + 管理员最终删除权（WebUI，第 4 步）三道闸落地。

| 型 | 字段 | 注入策略 |
| :--- | :--- | :--- |
| 约定型 | `id` / `text` / `about` / `created_at` / `due_at`（可空）/ `done_at` | 私聊：关于眼前人的未完成约定全文（过期排前，带期限标记）；群聊：只出一条脱敏计数行 |
| 事实型 | `id` / `text` / `created_at` / `updated_at` | **只在与该人的私聊**注入（同一个群 ≠ 该人在场，群里一律不念） |

已定稿的口径（第 2 步实施，落进 `core/notebook/api.py`）：

1. **记录不限会话**：群聊也能记，`about` 缺省＝"眼前人"（`Session.person_id()`：私聊＝对端、群聊＝发言者、主动轮从 umo 兜底推导）。
2. **注入判定写在 api 入口内部**，不靠工具自觉：`facts_for` 群聊一律空；`promise_line` 群聊只报全局未完成数（不带内容）。
3. **改动按条目归属**：`about == 眼前人` 才能完成 / 删（私聊群聊同一套）；别人的条目一律"没找到"（不泄露存在性）。
4. **上限**：约定 20 / 事实 15（每人每类，配置项）；约定按**未完成**计（完成即腾坑）。满了返回「本子满了，先删掉或合并几条再来」——不报错；"合并"没有专门工具，就是删旧（`note_forget`）+ 写新（`note_add`）。
5. **回收站**：删掉的条目进 `notebook.json` 的 `trash`（上限 50，超了丢最旧），WebUI 管理员可查。
6. `note_complete` 对事实 id 返回"事实没有「完成」一说"；约定销账写 `done_at` 后不再注入、不占上限。

注入预算（第 3 步起总量 400 字、五类）：约定（优先级 30）→ 事实 + 熟悉度档位词（20）→ 互动轨迹（15）→ 状态（10）→ 日记提醒（0）；超预算按砍序**整档**丢弃（`core/compose.py`），"先砍基调保当下"这类类目内部规则留在各自系统里。

## 状态系统（第 3 步）

三合一（方案 §4.3）：**情绪**（当下 + 近期基调 + 自报原词）、**作息**（config 时段表 → 状态词）、**熟悉度**（漏积分器单标量 + 粗档位词）。三者最终产物只有注入里的一两行 + 触发器用的机器可读出口；**数值永不进注入**（决策 #10）。

已定稿的口径（第 3 步实施，落进 `core/state/`）：

1. **坐标由她自报，系统永不做「原词 → 坐标」映射**（v0.4 复核 #1，§七#9 关闭）：`mood_report` 与 `diary_write` 的 `valence` / `arousal` 是**字符串**参数（`""`＝没给），给了才动坐标（-2~+2 越界夹取），没给只沉淀原词。坐标给衰减与暗号阈值；注入用她的原词（没有原词才用象限词：挺开心 / 很平静 / 有点烦躁 / 有点低落）。
2. **两层情绪**：当下（快层，自报驱动，**6 小时可见期**，坐标按 **3 小时半衰期**衰减回中性——"三小时前有点烦"不该一直挂着）；近期基调（慢层，每次观测向目标挪一步地沉淀，**7 天半衰期**）。注入合成一行："最近她心情不错，不过这会儿有点烦。"
3. **基调限幅**（复核 #2）：单次最多移动一格、单日最多一格——防"一篇很丧的日记把基调拽低一整周"。状态档 50 字里情绪句 ≤44 字，超出**先砍基调、保当下**（规则留在 `State.mood_line` 内部）。
4. **作息是纯函数不落盘**：config 表每行「HH:MM|状态词」（默认 5 段，支持周末第二张表，留空沿用工作日）；到点的最近一段生效，凌晨归入最后一段。`is_late_night()` 按 `state.late_night`（默认 `23:30-06:30`，可跨午夜）喂关怀触发器与主动消息免打扰。
5. **熟悉度 = 漏积分器**（§七#6 待定参数第 3 步定稿、返工轮调整）：`score ×= λ^(Δt/单位) + Δ`，**λ=0.5、单位=21 天（三周半衰期，待调）**——判据：一周不聊不掉档、一个月不见才明显降（7 天半衰期会让稳定互动者贴着熟稔线跑，一天不聊就掉档，这是返工轮修掉的病根）；Δ 两档——**私聊 1.0 / 群里被叫醒并接话 0.6**（没有"群聊普通发言"档，复核 #4；`kind="group"` 只是 `mention` 的别名）；**单日增量封顶 3.0**；档位阈值 **熟稔 ≥10 / 泛泛 ≥3 / 其余陌生**（每天私聊两周多进熟稔；Δ 与阈值返工轮复核后维持）。
6. **衰减按经过时间折算**（复核 #3）：落盘时把上次落盘以来的衰减折进去，读取时把当前时刻的衰减现算出来（score / band / top），不写回——落盘频率再怎么变，数字都可复现。**互动计数合并写**（返工轮，框架 §4.3 写放大）：`touch()` 只更新内存 overlay，窗口安静 1.5 秒后由后台任务统一原子落盘——同会话连续互动只落一次盘；读侧恒为"文件 + overlay"，本进程内读写一致；正常关闭（`terminate`）强制 flush，硬断电最多丢窗口内（≤1.5 秒）的增量。权重梯子里没有"群聊普通发言"，信号源只有"她被叫醒"（`on_llm_request` → `affinity.touch`）。
7. **互动轨迹（trace 档）零新存储**（§2.6#4）：`f(affinity.last_ts, proactive.last_sent_at)` 两个时间戳的比较——"她上次主动找他是何时、对方回了没有 + 距上次互动多久"。`proactive.json` 由第 4 步写入：**文件不存在 / 字段缺失 / 解析失败 → 整档返回空串**（第 3 步阶段必然如此），不报错、不占预算；字段名 `last_sent_at`（ISO8601，按会话，先按 umo 再按 session_id 查）已冻结。
8. **情绪历史独立追加文件**（§2.5 v0.7）：`state_history.jsonl` 一行一个 JSON（`ts` / `layer` / `valence` / `arousal` / `word`），坐标没变不写、变了按 `layer` 各写一行、保留 180 天——WebUI 情绪曲线的唯一数据源，形状见「存储契约」一节。
9. **注入预算 400 字五类**（§2.6）：`PROMPT_BUDGET` 300→400，`PRIORITY` 新增 `trace`（插在 fact 与 state 之间）；熟悉度档位词并进 **fact 档**（与"关于当前人的事实"同一配额）。不回归保险丝：`state=None` 且 `affinity=None` 时输出与第 2 步逐字节相同（有测试钉住）。
10. **开关**：`subsystems.state` 一刀切管三合一（关了 `observe` 拒写、注入行全空、`is_late_night` 恒 False）；`score` / `band` / `top` 作为纯数据口仍可读（WebUI 本来就直接读文件）。

## 主动消息（第 4 步）

三件套职责分离、互不知情（方案 §4.4 v0.6 定稿），全部收在 `core/proactive/`，cron 与出站 API 在 `main.py`：

1. **触发器（有什么可说）**：七个，各自只产候选（说给谁 / **原文片段** / 优先级），无权决定发不发。优先级 `暗号 > 约定跟进 > 纪念日 > 关怀 > 久未联系 > 早安/晚安 > 日记驱动`——软内容排最后，频率上限先挤掉最像打卡的那类。**有内容才发**：候选必须带具体素材（约定原文 / 事实原文 / 最后互动那天的日记 / 作息词 / 真实的时间事实），取不到就不发。所有目标会话从 `contacts`（`on_llm_request` 里顺手记录的 person → 会话反查表）查 umo；对目标人取料走 `facts_for` / `read_for`，第 1/2 步的可见性判定原样生效——恋爱日记的内容只会送到它唯一的读者手里。
2. **闸门（该不该说）**：**先闸门后内容**——免打扰是全局闸（常规内容整个静默），配额 / 最小间隔是会话闸（先算出"今天还能对谁说话"，触发器只对这些会话取料，巡检 15min 一天 96 次不白翻日记原文）；然后按优先级逐个问触发器，**一 tick 至多放行一条**。三条硬约束：免打扰时段（`is_late_night`）、私聊每日 2 条 / 群聊每日 1 条、同会话最小间隔 2 小时。**群聊克制**：除约定跟进 / 纪念日外，其余触发器只在私聊出（纪念日的内容源 `facts_for` 本就只在私聊可见，实际能进群的只有约定跟进）。
3. **出站（怎么说）**：默认**唤醒她本人**——`add_active_job(payload={"session": umo, "note": …}, run_once=True)`；`note` = `compose_prompt` 的完整注入（人格一致，不另写渲染器）+ ≤100 字上下文包（`【类目】指令 素材：原文片段`，压掉换行——note 经宿主 `json.dumps` 进 system prompt，预算按转义后算）。**直发只用于心情暗号**：整条消息就是那个字符（默认 `。`，可配 `signal_char`），`StarTools.send_message` 返回 `True` 才算发出（平台没找到 / 异常都不写 `last_sent_at`）。

**两段式 `last_sent_at`**：决策时只扣 `today_count` / `last_slot` / `slots_today` / 暗号两键；`last_sent_at` 等**真的发出去**才写——唤醒路径的确认点是 `on_using_llm_tool`（她真调了 `send_message_to_user` 且事件带 `cron_job` extra），直发路径是 `send_message` 返回 `True`。中间失败 = 配额白吃一次，换轨迹永不替她说"她找过你了"。日志里"已派发"与"已发出"分开记。

**心情暗号四道闸**（决策 #16：破免打扰，稀有性由四道闸保证）：① 只发 `love_peers` 名单内的**私聊**（没记录过 contact = 不知道往哪发，不发）；② 当下 valence 落最低档——判**衰减后**坐标（`snapshot`），阈值常量 **-0.7（待调）**：自报 -2 后约 4.5h、-1 后约 1.5h 内判中，语义是"她**现在**还在难受"；从没给过坐标的期间不触发；③ 独立冷却 3 天；④ 每天最多一次。

**暗号穿透配额**（验收裁定，与"破免打扰"同理）：`_commit` 里 `slot == "signal"` **跳过配额复核**——防打扰的闸门不该挡住求助信号；稀有性已由四道闸保证。`today_count` 照常自增（暗号也是一条主动消息，计数不失真），所以私聊配额 2 时暗号最多 1 条 + 常规 1 条。

**发送记录（`proactive-log.jsonl`，决策 #19）**：确认发出后追加一行 `{ts, umo, slot, fragment}`（ts 是确认时刻；slot / fragment 取决策暂存，重启丢失则 slot 回落 `last_slot`、fragment 记空串——"她什么时候找过谁"不丢）；append-only，保留最近 **200 行或 90 天**（裁剪时整文件重写一次）。它是 WebUI 三期「主动消息记录」的唯一数据源。

**待回窗口**（§3.3）：`trace_line` 在 `pending_window_hours`（默认 6h）内且对方未回时只说"她刚主动找过你"，窗口外才说"你还没回她"——她刚发完五分钟，不演被冷落。

**同日防重（`slots_today`）**：早安 / 晚安 / 约定跟进 / 纪念日 / 关怀 / 久未联系 / 日记驱动各 slot 当天只发一次（早安晚安各算一个 slot）——2 小时最小间隔挡不住"窗口比间隔长"的类目（纪念日全天、早安窗 2.5h），一天一条的约束收口在这里。

**劝睡窗与免打扰的重叠是正确行为**：劝睡窗 22:30–23:30，`late_night` 默认 23:30 起——23:30 后连劝睡都被免打扰挡掉是设计（深夜连劝睡都不该发）；把 `late_night` 配得更早就更短。各触发器的时段窗口（劝睡 / 劝饭 / 早晚安 / 日记驱动）是代码常量，**待调**；频率七项是配置（见「配置」一节）。

**数据形状**：`proactive.json` = 冻结的 `sessions`（键 umo：`last_sent_at`（冻结键）/ `today_date` / `today_count` / `last_slot`）+ 本步扩展（`slots_today` / `signal_last_at` / `signal_date` / 顶层 `contacts`）——`trace_line` 的读取面只认 `sessions[*].last_sent_at`，扩展键它一律忽略。

## WebUI 面板（第 5 步）

Dashboard → 插件管理 → `astrbot_plugin_denia_diary` → 插件页。**新增/删除页面目录后必须重载插件**（官方 §0#8）；只改静态资源刷新页面即可。

> 视觉参考：[`astrbot_plugin_daily_life`](https://github.com/siciyuanweilai/astrbot_plugin_daily_life)（MIT，作者 四次元未来）——第 6.3 步的仪表条、设计令牌与 bento 网格手法取自它；**只取手法不取体量**，其环境粒子层与光标跟随便签均未采用。

### 六个 tab

| tab | 数据 | 读写 |
| :--- | :--- | :--- |
| 总览（她此刻） | 当下情绪 + 基调 + 作息词 + 今日主动计数 + 四个子系统开关 + 每个数据文件 `{exists, bytes, mtime}` + 版本 | 读 |
| 日记 | 两本的篇数/字数/最近更新；按日期或"最近 N 条"读正文。**恋爱日记默认收起**（决策 #18） | 只读 |
| 小本本 | 事实与约定（按顶栏"对谁"过滤）+ 上限；可标记完成 / 删除 | 读 + 写 |
| 熟悉度 | 榜（衰减后分数降序）+ 档位词 + `last_ts` + **当前 `love_peers`（只显示）** | 读 |
| 曲线与主动 | 手写 SVG 折线（valence / arousal，当下令 + 基调点）+ 主动消息计数与发送记录（倒序） | 读 |
| 设置 | **全部配置项**（schema 驱动展开，见「设置页」一节） | 读 + 写 |

### 数据面裁定（重要）

**读与写都走 `store` 层，不走门面。** 门面方法全都吃一个 `Session`，而 WebUI 是 Dashboard 登录态、**没有会话上下文**；聊天里的可见性规则与归属判定是**对话安全规则**，套到面板上会让主人自己反而看不到、改不了。所以 `core/webui_data.py` 直接读 `DiaryStore` / `NotebookStore` / `StateStore` / `AffinityStore` / `ProactiveStore`，**不需要 `umo` 也能画出全部内容**。

衰减与档位**不自算**：情绪与作息照抄 `State.snapshot()` 的 `mood`/`rhythm` 子表，榜与档位词照抄 `Affinity.top()` / `Affinity.band()`。

### 日历清单与结构化条目（第 6.1 步）

前端要画日历、渲染单页日记本，数据面为此加了两份**只增不改**的键（老键一个没动）：

- `GET diary/list` 每本多出 `days` / `first_date` / `days_truncated`：
  - `days` 是 `{"date": "YYYY-MM-DD", "count": n, "chars": m}` 每天一项，**按 `date` 升序**，同一天多条聚合成一项（`count`/`chars` 是那天合计）；
  - 上限 **730 天**（`MAX_CALENDAR_DAYS`，约两年）：超了只保留**最近的 730 天**并把 `days_truncated` 置 `true`——前端据此提示"更早的不在日历里"。`first_date` 一律取**截断后** `days[0].date`（没有日记就是 `""`），别按"最早一天"单独理解，否则截断时前端会跳进空月份；
  - 顶层 `entries` / `chars` / `latest_date` 仍是**全量口径**，不随截断变。
- `GET diary/content` 多出 `entries` 数组：`{"date", "time", "mood", "who", "chars", "text"}` 每条一项，顺序与 `picked`（`parse_entries` 出来的顺序）完全一致、条数等于 `count`。`mood` / `who` **原样透传**（`mood` 可能是空串；`who` 是标注原文，昵称替换是前端拿 `who_options` 干的活）；`entries[].text` 是那一条的正文原文（可含换行，前端按空行分段渲染）。
  - **`text` 原样保留**（整段视图，老前端与既有测试在读它）；非空 `picked` 时恒有 `"\n\n".join(e["text"] for e in entries) == text`（测试钉住）。

### 「对谁」显示名字（第 8 步）

顶栏「对谁」下拉不再是一串裸数字。三件事：

1. **昵称落盘**：`proactive.json` 的 `contacts[<person_id>]` 加 **`name`** 键（加键不改名，
   不用迁移）——互动时从 `session.sender_name` 写入；**空名字一个字不动**（不写空串、
   不擦已有值），换新名字才覆盖。
2. **显示串 = `名字(数字)`**：`display_name(settings, person, contacts)` 按
   `name_preference[person] → contacts[person]["name"] → 空` 三级回落（与
   `Session.label()` 同序），有名字且名字 ≠ id 就拼 `名字(id)`，否则就是 id 本身
   （**绝不出现** `1411638634(1411638634)`）。**不传 `contacts` 退回旧行为**
   （只认 `name_preference`，不加后缀）——熟悉度榜等没有联系人表的既有调用零破坏。
   `who_options*` 每项带 `name`（昵称本体，可能空）与 `label`（显示串），
   **`id` 恒为 person_id**（前端按 id 去重）。
3. **候选跟档位标注（一个都不删）**：`who_options*` 每项加 `is_owner`（在 `love_peers`
   里）与 `out_of_scope`（当前档下她不会在这个人的会话里工作；按第 7 步口径只有
   `owner` 档会产生档位外的人），`owner` 档主人**置顶**（稳定排序）；三档候选数量相同。
   `status_payload` / `notebook_payload` 另加 **`scope_mode`**（前端显示"当前启用范围：只主人"）。

### 路由表

endpoint **不带插件名前缀**、**不带前导斜杠**（前端写 `"status"`）；注册时由 `web_api/routes.py` 补成 `/astrbot_plugin_denia_diary/status`。

| endpoint | 方法 | 说明 |
| :--- | :--- | :--- |
| `status` | GET | 总览；`?who=` 可选 |
| `diary/list` | GET | 两本概览 |
| `diary/content` | GET | `?book=&date=&tail=` |
| `notebook` | GET | `?who=` 过滤 |
| `notebook/complete` | POST | `{"id": "..."}` |
| `notebook/delete` | POST | `{"id": "..."}`（先进 `notebook.json` 的 `trash`） |
| `affinity` | GET | 榜 + `love_peers`（只显示） |
| `history` | GET | 情绪曲线数据点；`?days=` 默认 30，上限 365 |
| `proactive` | GET | 主动消息计数 + 发送记录；`?limit=` |
| `settings` | GET | 设置页字段表：`sections`（大类树）/ `groups` / `fields` / `values` / `defaults` / `warnings` / `editable_paths` / `problems`（schema 对齐自检） |
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

1. **一律经 `web_api/_web.py` 的 `error_response`，别直接调宿主的**。宿主签名很窄
   （`error_response(message, *, status_code=400, data=None, headers=None)`，`status_code`
   关键字限定、不接任意 kwarg），历史上 21 处 `error_response(msg, 400, endpoint=…)` 在真机上
   全 `TypeError` → 连 `logged_handler` 的兜底 500 一起炸穿 → **所有错误路径全是 500**，而离线
   单测全绿（离线桩比真机宽）。归一层的内部签名是
   `error_response(message, status_code=400, *, errors=None, problems=None, endpoint="", headers=None)`，
   附加信息统一进信封的 `data`。**回归测试在 `test/test_web_api_contract.py`**（照真机签名造
   假宿主 + 静态核对调用点形状）。
2. **400 信封里的 `data` 到不了 iframe**——官方 bridge 的失败只透出一个字符串
   （`plugin_page_bridge.js` `pending.reject(new Error(message.error))`）。所以 toast 用
   `error.message` 没问题；**服务端的逐项错误想在前端"就地显示"，只能走「200 + `{ok:false, errors:[…]}`」
   的业务结果**，别指望 400 的 `data`。

### 兼容与降级

- `context` 上没有 `register_web_api`（AstrBot < 4.24.2）时**静默跳过注册**，插件本体照常在聊天里工作；
- 拿不到 bridge（直接 `file://` 打开、或 bridge 未就绪）显示"请从插件详情页打开本页面"，**不白屏**；
- 任一视图读数失败渲染可读错误框，其余 tab 照常可用。

### 离线自测

```powershell
$env:PYTHONDONTWRITEBYTECODE=1; $env:PYTHONUTF8=1
py -m unittest discover -s test -t .      # 620 项
py tools/webui_selfcheck.py                # 静态对账 + 资源/编码体检
```

`tools/webui_selfcheck.py` 查：页面存在、资源齐全且全相对、无 CDN / 无 `../`、`core/` 零 astrbot import、**后端路由表 ↔ 前端 endpoint 双向一一对应**、新文件 UTF-8 无 BOM + 纯 LF。改了一边忘了另一边就在这里报错。

**离线看界面**（不必起 AstrBot）：直接用浏览器打开 `pages/diary/index.html`——`preview-bridge.js` 只在 `file://` 或 `?preview=1` 时装假 bridge，正式 iframe 里它什么也不做。

## 设置页（第 5.2 步）

WebUI 第 6 个 tab「设置」：**全部配置项**都在这里改，改完立刻生效，**不用重载插件**。

### 字段表由 schema 驱动

`GET settings` 把 `_conf_schema.json` **递归展开**成字段表——前端**不抄一份字段清单**，
`web_api/handlers.py` 也不维护第二份映射：

- `groups`：顶层节点（保持 schema 书写顺序），组是 `object` 并带 `children`；
- `fields`：扁平叶子表，`path` 是**点分路径**（`diary.max_chars`），就是提交时 `changes` 的键名；
- 叶子带 `type`（覆盖 `bool / string / int / list / dict` 五种）、`description`、`hint`、`default`；
  `int` 项还带 `min` / `max`（取自 `core.settings.INT_LIMITS`，**不重抄一份**）；
- `values` 是**当前实际生效的值**（`self.settings` 那份校验后的快照，**不是**原始 config）——
  越界被夹紧、非法已回落的面板上都显示夹紧/回落后的值，不会骗人；
- `defaults` 是 schema 默认值，给「恢复默认值」用；
- `warnings` 原样带出配置降级项，**配置有问题时面板要看得见**。

控件按类型自动选：`bool`→开关、`int`→数字框（带 min/max）、默认带换行的 `string`→textarea（作息表）、
`string`→单行、`list`→一行一项、`dict`→JSON 文本域（**解析失败就地报错、绝不提交**）。
底部固定「保存 / 撤销改动 / 恢复默认值」，有未保存改动会提示；保存后把后端返回的
`applied` / `backup` / `reloaded` / `warnings` 显示出来。

### 字段提示与字符串格式（第 6.1 步写死）

`describe_schema` 的每个 field 都带一个 `editor` 字符串，前端据此把特定字段换成更好的输入控件
（**其它键一个没少**；`_conf_schema.json` 不动，`POST settings` 的载荷格式也不变——值仍然是字符串）：

| 字段 | `editor` | 前端控件 |
| :--- | :--- | :--- |
| `state.rhythm` | `"rhythm"` | 作息表编辑器（见下方格式） |
| `state.rhythm_weekend` | `"rhythm"` | 同上 |
| `state.late_night` | `"time_range"` | 双时间框（可跨午夜） |
| `scope.mode` | `"scope"` | 三段切换（owner / private / all） |
| 其余全部 | `""` | 按类型自动选 |

提示表住在 `core/webui_settings.py` 的 `FIELD_EDITORS` 常量里；表里写了 schema 上不存在的路径
会被 `verify_schema_alignment` 报出来（启动 warning + `GET settings` 的 `problems`）——写错要喊，别静默失效。

两个自定义编辑器要解析的**字符串格式**如下（此前只散在 `core/settings.py` / `core/state/api.py`
的注释里，这里写死为准，与 `_segment_word` / `_parse_window` 的实现一一对应）：

- **作息表**（`state.rhythm` / `state.rhythm_weekend`）：每行 `HH:MM|状态词`；
  - `#` 开头是注释行；全角 `｜` 也当分隔符；解析不了的行、或词为空的行**忽略（不报错）**；
  - **到点的最近一段生效**；**凌晨（第一段之前）归入最后一段**（跨天收尾，如 `23:00|该睡了` 对凌晨 02:00 依然有效）；
  - 行顺序无关（内部按时间排序）；**空表 = 没有作息词**，注入里就不会有作息行；
  - `rhythm_weekend` **留空 = 沿用工作日那张**，只有周六日才读它。
- **深夜窗**（`state.late_night`）：`HH:MM-HH:MM`，**可跨午夜**（如 `23:30-06:30`，判 `分钟≥起点 或 <终点`）；
  起止相同或留空 = 永不深夜；解析不了 = 永不深夜（不报错）。

### 校验准绳是 `load_settings` 的 warnings

面板**不复刻**一套配置规则——`core/settings.py` 才是配置的**唯一解释者**（越界夹紧、非法回落、
正则零宽拦截都在那儿）。所以流程是：

1. `changes` 合并进当前 config 的**深拷贝** → 未知点分路径 / 只读项 / 类型不符 → **整单拒绝，一个字节都不写**；
2. `core.settings.load_settings(前后各一次)` → **新增**的 warning 必然来自被改的键 → 整单拒绝；
3. 过了才落盘。既有 warning **不**算新问题（本来就坏的配置不该让面板每个改动都改不动）。

### 落盘、热生效与备份

写盘走插件**自己的活配置对象** `self.config`（`AstrBotConfig` 是 dict 子类，构造时注入）：
`clear()` 后 `save_config(updated)`；`save_config` 不可用时回落 `update()`。
**不走** Dashboard 的 `/api/v1/plugins/{id}/config`——那条路是"写盘 + 热重载插件"，
会把整个插件整个重启掉。

成功落盘后**就地热生效**（`main.py` 的 `apply_settings`）：

- **就地重建 `self.settings`**，并把 Diary / Notebook / State / Affinity / Proactive 五个门面的
  `settings` 引用一起换掉——门面在构造时各拿了一份快照，只换 `self.settings` 等于"改了不生效"；
- `proactive.patrol_minutes` 变了 → 重建巡检 job（老规矩：按 name 删旧再 `add_basic_job` 新建）；
- `timezone` 变了 → **也重建**（第 11 步改判）：判定用 `settings.zone()` 每次现算没错，
  但 job 上的 `timezone` 是建的时候那个，不重建就是"调度按旧时区、判断按新时区"两套真相。
  ⚠️ 说实话：`*/N * * * *` 这种纯分钟步长的**触发时刻与时区无关**（整小时偏移下完全一样），
  所以真危害只是面板上那个 job 的时区显示是旧的——这是一次"对齐"，不是修了个严重 bug。

**安全**：写盘前把旧配置文件整份备份到数据目录 `config_backup_<YYYYmmdd-HHMMSS>.json`；
备份失败**就整单放弃**（备份是安全网，备份不下来时"这次没改成"好过"配置写坏"）；
落盘失败回落到内存旧值并回报错误。写操作全程 `try/except` + `logged_handler`，记 `request.username` 审计。

## 品牌化与面板外观（第 10 步 · 1.0）

插件显示名是**情绪日记**（`metadata.yaml` 的 `display_name`）。注意 `name:` 仍是
`astrbot_plugin_denia_diary`——它同时是**目录名与模块名**，改它等于换安装目录，
已经装过的用户点「更新」会变成又装一份。仓库名同理。

- **logo**：插件**根目录**的 `logo.png`（256×256）。宿主只在根目录找这个固定文件名
  （`star_manager` 的 `logo_fname`），认到之后详情页回 `/api/file/<token>`。
- **左上角标题**：配置 `panel.brand` / `panel.brand_sub`（设置页 → 基础 → 面板外观）。
  默认 `情绪日记` / `观察面板`；**留空串＝那一行不显示**（空串是合法值，不是"没填"）。
  超 60 字在读取时就地截断并记 warning；从面板保存超长会被整单拒绝（口径见设置页那节）。
  前端只认后端回的值，`index.html` 里那份字面量是"拿不到配置时"的兜底。
  ⚠️ 藏那两行要靠 CSS 的 `[hidden]` 兜底——`.brand` 是 `display:flex`，
  UA 的 `[hidden]{display:none}` 特异性不够，光设 `hidden` 属性藏不住（真浏览器检查抓到的）。
- **不打包任何立绘**：1.0 起删掉了随包的默认图，没图时首页显示「这里可以放图」占位；
  图只在用户自己传过之后才出现（见下一节）。离线预览想空着看：
  `pages/diary/index.html?preview=1&portrait=empty`。

## 立绘上传（第 5.3 步）

首页立绘支持**用户自己上传**（前端单图槽 + 切换 / 上传 / 删除）。**包里一张图都不带**
（1.0 起删掉了默认图），没用过就是那块「这里可以放图」的占位。存储在数据目录
`portraits/`，**文件名服务端生成** `p_<12 位 hex>.<ext>`——用户原始文件名只作展示
来源且存净化版（去目录、去危险字符、限长 60），路径穿越无从谈起。同目录
`index.json` 记 `{"current", "items"}`（每项 `{id, name, mime, bytes, created_at}`）。

**接口**（见路由表）：
- `GET portrait` → `current`（含 `data_url`，可直接塞 `<img src>`）+ `items`（只有元数据——
  单张 8 MB 的 base64 不允许乘以张数塞进一个响应）；一张都没有是 200 + `{"current": null, "items": []}`；
- `GET portrait?id=` → 单张含 `data_url`；`POST portrait/select` 响应直接带新的 `current`（含
  `data_url`），前端切换不必再发一次请求；`POST portrait/delete` 连文件一起删。

**安全与校验（官方"不要信任 Page 传来的路径、文件名、格式或数值范围"）**：
- 格式按**魔数**判定（PNG / JPEG / GIF87a|89a / RIFF+WEBP），`content_type` 一概不信，扩展名由魔数定；
- 单张 ≤ **8 MB**、总数 ≤ **20 张**、请求体 `content_length` **预检**（超大直接 400 不读进内存），
  能设 `request.max_content_length` 就设（宿主没有这属性就 try/except 兜住）；
- 违规 → **400 且一个字节不落盘**。

**落盘纪律**：`index.json` 走 `storage.atomic_write_text` + `KeyedLocks`；**先落图再写
index**，写 index 失败**回滚删图**；删除连文件一起删；`GET` 时"index 有、盘上没文件"的
条目跳过并顺手清 index（自愈，current 被清就落到剩余第一张）。

**宿主兼容**（契约 §四 03:20 裁定 #6，照 meme_manager 的垫）：`PluginUploadFile.save`
在不同 AstrBot 版本上同步 / 异步不一样——`inspect.iscoroutinefunction` 为真就 `await`，
否则 `await asyncio.to_thread(save, path)`；走「save 到临时文件再读字节」，不赌 `read()`
的兼容面。上传对象以鸭子类型进 core（`core/` 依旧零 astrbot import）。

### 只读项

- **`data_dir` 只显示不可改**：改它＝搬走全部日记与本子，Layout 在插件启动期就建好了。
  返回结构化错误说明原因，要改请走原生插件面板 + 重载插件。
- **`enabled`（总开关）允许改**，但响应里会带回提示：关掉后日记、小本本、状态、主动消息全部不可用（数据保留）。
- `love_peers` 与 `name_preference` **都能改**（用户明确要求全部配置项进面板）；
  这**推翻了任务书 §2 裁定 2 的「`love_peers` 只显示」**。

### 大类 → 分组 → 配置项（对齐参考实现）

`GET settings` 的 `sections` 是一棵**展示树**：大类（`基础 / 内容 / 主动消息 / 出站`）→
分组（`_conf_schema.json` 的 11 个顶层键）→ 组内叶子的点分路径。归属写在
`core/webui_settings.py` 的一张常量表里（`CONFIG_GROUPS` 声明分组与展示顺序、
`CONFIG_SECTIONS` 声明大类），**前端只按树渲染，不写死分组**；归属写错/漏分组由
`verify_schema_alignment` 报出来，不影响存储契约。

### schema 对齐自检

`verify_schema_alignment(schema) -> [problems]`：`_conf_schema.json` 与
`core/settings.py` 默认值表**双向**核对——顶层键集合、组内键集合、`dict` 叶子与
分组的形状、`int` 项有没有 `INT_LIMITS` 取值范围、大类归属是否覆盖/越界。
`initialize()` 启动时跑一次，有就**逐条写 warning 日志**；`GET settings` 的
`problems` 数组原样带出（显示是前端的事，后端只出数据）。AstrBot 会剔除
schema 里不存在的键，两边不一致时用户保存的值会在重载时静默丢失——这就是自检必须存在的原因。

### 逐项 coerce 与错误清单

`POST settings` 的校验分两层，错误**逐字段**返回（`errors: [{"path", "error"}]`）：

1. **类型对齐**（`coerce_value(field, raw) -> (value, error)`，`review_changes` 逐项
   收集**全部**错误）：未知路径 / 只读项 / 类型不符——严格度与旧实现一致
   （bool 冒充 int、字符串冒充 bool 在这里就拒）；取值范围**不在这层查**
   （那是 `load_settings` 的活，两层各写一半规则必然漂移）。有任何错误 →
   **400 + `errors` 逐字段 + 一个字节不写**（整单拒绝的硬约束不变，但每个坏
   字段都能在自己的位置看到原因，不再是一个笼统的 message）。
2. **唯一准绳**（`verify_settings`）：`load_settings` 前后各跑一次，新增 warning
   → 400 + `errors`（`path` 从 warning 文案开头的字段路径提取，整体性的挂空 path）
   + `problems` 原样。

合法响应在既有 `applied` 之外加了一个别名 `changed`（与 `applied` 同值，对齐参考命名）。

### 恢复默认（`POST settings/reset`，无 body）

默认值**只从 schema 取**（`defaults_by_path`），只回**可编辑且真的变了**的项
（`data_dir` 永不进 reset）；无变化 → `{ok, changed: []}` 且**不落盘、不备份**；
有变化 → 走与保存完全相同的链（逐项校验 → 备份 → 落盘 → 热生效 → 重建巡检 job），
返回 `changed` 列表（就是 `applied`）。

### 错误口径

| 情况 | 响应 |
| :--- | :--- |
| body 不是 JSON 对象 / 缺 `changes` / `changes` 为空 | **400** |
| 未知点分路径、只读项、类型不符、组路径本身 | **400** |
| 值被 `load_settings` 判为非法（越界/坏正则/坏时区/零宽） | **400** + `problems` 逐条列出，**不写盘** |
| 备份或落盘失败 | 200 + `{ok: false, error}` |

## 启用范围（第 7 步）

一个总闸决定"她在哪里工作"：**记录 / 观察 / 注入 / 她的写工具**按会话范围收放，
**主动消息的对象**也跟着同一档位走。判定只住在 `core/scope.py`（纯函数，零 astrbot），
三个入口（`main.py` 注入钩子、三个写工具、`core/proactive/`）都只调它。

三档语义（用户拍板，写死；设置页 `scope.mode`，editor 提示 `"scope"`）：

| 档 | 记录 / 观察 / 注入 / 她的写工具 | 主动消息目标 |
| :--- | :--- | :--- |
| `owner`（只主人） | **只在 `love_peers` 里那个人的私聊**里工作；群聊一律不工作 | 主人私聊（现状不变） |
| `private`（只私聊·**默认**） | **任何私聊**都工作；群聊不工作 | **任何私聊过的人**（不再只有主人）+ 名单内的人 |
| `all`（全部启用） | 私聊 + **群聊**都工作 | 任何私聊过的人 **+ 群聊**（受护栏，见下） |

「工作」= 注入了她的动态上下文 / 写了日记或小本本 / 观察了情绪。**范围外就是整轮不介入**：
不注入、`diary_write` / `note_add` / `mood_report` 拒绝并回一句人话（「这个会话不在启用范围内，
我不在这里记」）、连熟悉度计数与联系人表都不留痕。

⚠️ **面板与 WebUI 接口永远可用**，不受这个闸影响——否则切到 `owner` 之后，在群里打开面板会被自己锁死。

### 升级注意（对现有行为的变化）

**从这一步起默认只在私聊工作（`private`），群聊里的记录 / 注入 / 写工具默认全部停止**——
这是用户明确要的。需要群聊请把 `scope.mode` 切到「全部启用」（`all`）。

### `love_peers` 的语义收窄

`love_peers` = **主人身份**：`owner` 档的判定 + 恋爱日记可见性（这两条不变）。
主动消息的对象从这一步起由**档位**决定（`scope.proactive_targets`），名单里的人只是
`owner` 档下的全部、`private` 档下的子集。心情暗号是例外：它是主人之间的约定信号
（决策 #16），对象保持 `love_peers` 私聊不变。

### 主动消息的护栏（`private` 放开对象、`all` 再放开群聊）

- **对象必须"跟她互动过"**：目标集合的唯一来源是 `proactive.contacts`（每个被动轮由
  `note_contact` 记录"谁在哪跟我说话"）。从没互动过的人、没见过的群，**任何档位都进不来**；
- **配额沿用按会话的现成计数**：`sessions[<umo>].today_count` + `daily_limit_private`（默认 2）/
  `daily_limit_group`（默认 1）——扩对象天然每人一份配额，A 发满不影响 B，没有新增计数器；
- 深夜 / 免打扰 / 冷却 / 素材这些闸一个不少；
- **群里不许裸发**：群聊目标的唤醒 note 开头就要求她先 @ 提及对方（`all` 档）。

## 出站文本清洗（第 5.1 步）

**问题**：她发出去的消息里带的 `&&sleep&&` 会**原样漏给用户**。原因有三层：

1. `send_message_to_user` 只认 `plain / image / record / video / file / mention_user` 六种 type，**没有表情包类型**；
2. 表情包插件唯一的处理点在 `on_decorating_result`，而它第一行就是 `event.get_result()`——**直发路径没有 result 对象，拦不到**；
3. `on_using_llm_tool` 拿到的 `tool_args` 紧接着就被 `execute(**valid_params)` 展开成**同一个 dict** → 在钩子里改真的生效。

**做法**：在现有的 `on_using_llm_tool` 钩子（第 4 步的确认点就在那儿）最前面加一步清洗，
`tool_args["messages"] = clean_messages(...)` **原地改**。判断在 `core/outbound.py`（纯函数、零 astrbot 依赖）。

| 规则 | 说明 |
| :--- | :--- |
| 只处理 | 工具名为 `send_message_to_user` 的调用，**主动轮与被动轮都洗** |
| 只动 | `messages` 里 `type == "plain"` 的 `text`；其它 type 与同一个 dict 里的别的键一个字节都不碰 |
| 只剥 | `&&名字&&`（`[x]` / `(x)` / `:x:` / `meme:id` **一律不动**——`(x)` 会吃掉正常括号动作、`:x:` 会吃掉颜文字，**宁可漏不可误伤**） |
| 剥法 | 整段删掉（不替换成文字），并压掉因此产生的多余空格与换行前的悬空空格 |
| 剥空 | 剥完只剩空白 → **保持原文**，绝不发出空串 |
| 绝不抛异常 | 整段 `try/except`；清洗坏了就按原文发送——**清洗坏了不能连累她说话** |

**配置**（`_conf_schema.json` 顶层 `outbound` 组，`core/settings.py` 校验，非法一律回落默认并记 warning）：

| 键 | 默认 | 说明 |
| :--- | :--- | :--- |
| `strip_meme_marks` | `true` | 总开关 |
| `marker_pattern` | `&&[^&\s]{1,24}&&` | `re` 语法；非字符串 / 编译失败 / **能匹配空串（会把消息清空）** 三种情况都回落默认 |

## 真机验证（2026-10-04）

环境：WSL Ubuntu 24.04 + AstrBot **4.28.1**（独立实例、空闲端口，不动已运行的实例）。
做法与配方见 `陪伴系统-框架方案.md` §十一.16。
| 核对项 | 结果 |
| :--- | :--- |
| 被真加载器识别 | `Loading plugin astrbot_plugin_denia_diary ...` → `Plugin astrbot_plugin_denia_diary (0.1.0) by 50841` |
| `core/` 子包 + 相对导入 | ✅ 真机加载不报错 |
| 4 个工具注册 | `Added llm tool: diary_write / diary_read / diary_list / diary_edit` |
| 描述 → JSON schema | 用 AstrBot 自己的 `register_llm_tool` 解析：类型 string / number / boolean 与描述都正确 |
| 提示挂载点 | `inject_diary_hint`，`priority=5` |
| 数据目录 | `<AstrBot>/data/plugin_data/astrbot_plugin_denia_diary/` 被创建 |
| 整体启动 | `AstrBot started.`，无 Traceback / ERROR |

真机发现并修掉的三个问题（前两个已加回归测试）：

1. `metadata.yaml` 的 `author: 50841` 被 YAML 当数字 → AstrBot 要求非空**字符串**，回退默认元数据。已改成 `"50841"`。
2. AstrBot 的 `spec_to_func` **不生成 `required`** → 模型可以一个参数都不带就调用；`diary_write` 的 `text` 原本没有默认值会 `TypeError`。已给全部工具参数加默认值，缺参数时返回人话。
3. 环境类：隔离实例与新实例会撞 dashboard 端口；且 AstrBot 写出的 `data/cmd_config.json` 带 UTF-8 BOM（读它要用 `utf-8-sig`）。

## 真机验证（2026-10-05，第 2 步）

环境：WSL Ubuntu 24.04 + 隔离 AstrBot 实例（脚本 `../wsl-verify-step2.sh`，空闲端口、只 kill 自己的 PID）。
按分工只做**隔离实例 + AstrBot 自解析 schema + handler 直调**，不做真 LLM 对话（真对话由用户/实施 Agent 在线上 bot 上线后验证）。

| 核对项 | 结果 |
| :--- | :--- |
| 隔离实例启动（两次） | `Plugin astrbot_plugin_denia_diary (0.2.0) by 50841` → `AstrBot started.`，**无 Traceback / ERROR** |
| 8 个工具注册 | `Added llm tool` × 8（4 个日记 + 4 个小本本） |
| 工具 → JSON schema | AstrBot 自己的 `spec_to_func` 解析：4 个新工具参数类型/描述正确，`required=None`（§九#17，参数全带默认值） |
| handler 直调（记一条事实和一条约定） | 私聊记 fact + promise、群聊记 fact（`about` 缺省＝发言者）→ `notebook.json` 落盘（`schema_version=1`，含 `trash`） |
| 注入差异 | 私聊：事实全文 + 约定全文都在 system_prompt；群聊：只有脱敏计数行（"答应过的事"，无内容）；**别人私聊：全无** |
| 完成 / 删除 / 上限 | `note_complete` 写 `done_at` 且**完成即停注**；`note_forget` 进 `trash`；上限满返回「本子满了」不报错 |
| 重启后 | 带着已有 `notebook.json` 再起一次：数据完好、无报错 |

## 真机验证（2026-10-05 15:27，线上 bot + 真 LLM）

环境：用户自己的 AstrBot（生产数据，非隔离实例），0.2.0 上线后由真模型驱动。

| 核对项 | 结果 |
| :--- | :--- |
| 8 个工具上线 | `astr_agent_prepare.tools` 里 `note_add/note_list/note_complete/note_forget` 全在 |
| 真 LLM 调 `note_list`（空参数） | 「小本本 · 关于508416913：约定 1 条 + 事实 1 条（上限 20/15）…」 |
| 真 LLM 调 `diary_read {"tail": 5}` | `（2 / 共 2 段）`，头行 / 心情 / 标注都在 |
| 注入位置与形状 | 落在 `system_prompt` 末尾：先「答应过的事」→ 再「记着」→ 再日记一行；**不含 id、不含上限数字** |

真机抓出并修掉的问题（回归见 `test_session.py` / `test_diary.py`）：

- **主动轮 / 定时唤醒不带真实 sender**：事件的 `sender_name` 是合成值 `Scheduler`、`sender_id` 为空。
  旧写法会让头行落成 `〔和Scheduler〕`，且 `love_peer()` 判空 → 那段日记掉进普通日记那本。
  已把 `Session.label()` 与 `Diary.love_peer()` 都收口到 `person_id()`；面板里配 `name_preference: {"<id>": "希"}` 即可让标注显示 `和希`。

## 真机验证（2026-10-05，第 3 步 · 比赛 A 位）

环境：WSL Ubuntu 24.04 + **复制到本格子的**隔离 AstrBot 实例（`_race/a/astrbot/`，端口 6199，只 kill 自己的 PID；脚本 `../wsl-verify-step3.sh`）。
按任务书 §1.3 只做**隔离实例 + AstrBot 自解析 schema + handler 直调**，不做真 LLM 对话、不碰共享实例（`._analyze`）与线上 bot。

| 核对项 | 结果 |
| :--- | :--- |
| 隔离实例启动（两次） | `Plugin astrbot_plugin_denia_diary (0.3.0) by 50841` → `AstrBot started.`，**无 Traceback / ERROR** |
| 9 个工具注册 | `Added llm tool` × 9（4 日记 + 4 小本本 + `mood_report`） |
| 工具 → JSON schema | AstrBot 自己的 `spec_to_func` 解析：`mood_report` 三个参数全为 **string**、`required=None`（§九#17）；`diary_write` 增的 `valence`/`arousal` 也是 string；**无任何 `affinity_*` 工具** |
| handler 直调（心情自报） | `mood_report(word, valence, arousal)` → `state.json` 落盘（`schema_version=1`，word/坐标/baseline 齐全）；基调单次只挪一格（-2 → baseline -1）；空自报返回人话报错；只报词不动坐标 |
| 情绪历史（§2.5 v0.7） | 坐标变了 → `state_history.jsonl` 追加 `now`/`baseline` 各一行、键形恰为 `ts/layer/valence/arousal/word`；同值重复自报 → 行数不变 |
| handler 直调（日记沉淀） | `diary_write(…, valence="-2")` → 日记照写 + 坐标落盘；`valence="很差"`（坏打分）→ 日记照写不受影响 |
| 注入差异 | 私聊：熟悉度档位词（陌生）+ 作息行 + 情绪词 + 日记行，**总量 ≤400**；群聊：档位词在（mention 档）、事实不出现；state 关闭 → 注入全静默 |
| 熟悉度 | 两次钩子 touch（私 1.0 + 群 0.6）→ `affinity.json` 单人 1.6，单日封顶生效 |
| 互动轨迹 | 无 `proactive.json` → 整档降级（提示里无"主动找过"）；补上 `last_sent_at` → "她前天主动找过你，后来你们又聊过"；last_ts 早于 last_sent_at → "你还没回她" |
| 重启后 | 带着已有 `state.json` / `affinity.json` / `state_history.jsonl` 再起一次：数据完好、无报错 |

以上 handler 直调共 **22 项检查全 PASS**（脚本 `_race/a/wsl-verify-step3.sh` 可重跑）。

## 真机验证（2026-10-05，第 4 步）

环境：WSL Ubuntu 24.04 + **复制到本格子的**隔离 AstrBot 实例（`_race/a/astrbot/`，端口 6199，只 kill 自己的 PID；脚本 `../wsl-verify-step4.sh`）。隔离实例无 provider，"真唤醒"验证到**派发 + 确认点钩子层**（与第 2/3 步"真对话由线上 bot 验证"的分工一致）。

| 核对项 | 结果 |
| :--- | :--- |
| 启动两次（带数据重启） | 两次 `AstrBot started.` 均**无 Traceback / ERROR**；两轮日志都有 `主动消息巡检已重建：每 15 分钟一次`（`initialize()` 重建 job） |
| 9 个工具 + schema | `Added llm tool` × 9；AstrBot 自解析 `mood_report` 三参数全 string、`required=None`（回归） |
| 巡检派发（basic job handler 直调） | `add_active_job` 收到 `payload={"session": umo, "note": 注入+上下文包}`、`cron_expression=None` 显式、`run_once=True`；日志留触发源 + 原文片段（`主动消息已派发（slot=promise → umo）：周三考试`） |
| 两段式 | 决策后 `today_count=1`、`last_slot=promise`、**无** `last_sent_at`；确认点后 `last_sent_at` 落盘 |
| 确认点（`on_using_llm_tool`） | 主动轮事件（带 `cron_job` extra）+ `send_message_to_user` → 写 `last_sent_at` + 日志 `主动消息已发出（确认点命中）`；被动轮同一工具、其它工具都不写 |
| 同日防重 | 同一 tick 第二次巡检不再发同 slot（`slots_today`） |
| 互动轨迹待回窗口 | last_sent 1h 前未回复 → `她今天刚主动找过你…`；20h 前 → `…你还没回她` |
| 三不发 | `subsystems.proactive=False` / 免打扰覆盖全天 / 配额扣满 2/2 → 三种都 `None` |
| 心情暗号 | valence=-2 自报 → `direct` 决策（`char="。"`）；直发失败（实例无平台，`StarTools` 未初始化）**不写** `last_sent_at` |
| 注入预算 | 仍 ≤400（回归） |

## 存储契约（第 0 步冻结）

数据目录由宿主在运行时给出（AstrBot：`StarTools.get_data_dir("astrbot_plugin_denia_diary")`）。

| 文件 | 用途 | 格式 |
| :--- | :--- | :--- |
| `日记.txt` | 日记正文（产品主张：记事本能直接打开） | 纯文本，无版本头 |
| `恋爱日记.txt` | 恋爱日记正文 | 纯文本，无版本头 |
| `notebook.json` | 小本本（约定型 / 事实型 + 回收站） | JSON + `schema_version` |
| `state.json` | 情绪当前值（全局一条，不按人分） | JSON + `schema_version` |
| `state_history.jsonl` | 情绪历史（WebUI 情绪曲线的唯一数据源，任务书 §2.5 v0.7） | JSONL 追加文件，一行一个 JSON，无版本头 |
| `affinity.json` | 熟悉度（按人一个漏积分器标量） | JSON + `schema_version` |
| `proactive.json` | 主动消息状态（`sessions`：按会话的 `last_sent_at` / 每日计数 / 暗号冷却；`contacts`：person → 会话反查表） | JSON + `schema_version` |
| `proactive-log.jsonl` | 主动消息发送记录（`{ts, umo, slot, fragment}` 一行一条，**确认发出后**才追加；保留最近 200 行或 90 天，裁剪时整文件重写）——WebUI「主动消息记录」的唯一数据源 | JSONL 追加，无版本头 |
| `names.json` | id → 昵称 / 群名缓存 | JSON + `schema_version` |
| `diary-nudge.json` | 日记提醒冷却（按会话） | JSON + `schema_version` |
| `diary-trash/` | 改/删日记前的原文备份 | 纯文本片段 |

`notebook.json` 形状（第 2 步定稿；`schema_version` 由 `atomic_write_json` 自动补；`id` 为 12 位 hex，生成后不变，WebUI 可引用）：

```json
{
  "schema_version": 1,
  "facts": { "<peer_id>": [ { "id": "...", "text": "...", "created_at": "ISO8601", "updated_at": "ISO8601" } ] },
  "promises": [ { "id": "...", "text": "...", "about": "<peer_id>", "created_at": "ISO8601", "due_at": "YYYY-MM-DD 或 null", "done_at": "ISO8601 或 null" } ],
  "trash": [ { "kind": "fact 或 promise", "deleted_at": "ISO8601", "entry": { "…被删的原条目…" } } ]
}
```

`state.json` 形状（第 3 步定稿；作息是"config 表 + 当前时间"的纯函数**不落盘**，这里只存情绪）：

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

`affinity.json` 形状（第 3 步定稿；`day` / `gained_today` 是"单日增量封顶"的记账键）：

```json
{
  "schema_version": 1,
  "people": { "<person_id>": { "score": 0.0, "last_ts": "ISO8601", "day": "YYYY-MM-DD", "gained_today": 0.0 } }
}
```

（骨架键以任务书 §2.5 为准——`word_at` / `day` / `gained_today` / `moved_*` 是"可加"的记账键，键名只增不改。）

`state_history.jsonl` 形状（任务书 §2.5 v0.7 定稿：**文件路径与行形状就是接口**，黑盒验收直接读文件）：

```json
{"ts": "2026-10-05T17:12:03+08:00", "layer": "now", "valence": -1, "arousal": 2, "word": "有点烦躁"}
```

三条写入规则（落进 `core/state/store.py` 的 `append_history` 与 `core/state/api.py` 的 `observe`）：

1. **追加型，不塞进 `state.json`**：`state.json` 里没有任何 history 字段（当前值与时间序列分文件）。
2. **坐标真的变了才追加**：`observe()` 里坐标动了写 `layer=now` 行、基调真挪了写 `layer=baseline` 行（当下与基调各写各的行，曲线同时看快慢两层）；**同值重复自报不写**——只刷新 `state.json` 的时间戳；只报词不动坐标也不写。
3. **保留最近 180 天**：模块常量 `HISTORY_KEEP_DAYS = 180`（不做配置项）。追加时发现最老行超 180 天 → 整文件重写一次（唯一重写时机，走 `atomic_write_text`）；正常路径是单行纯追加（`open("a")` + fsync），没有写放大。

七条约定：

1. **原子写**——同目录临时文件 → `os.replace`；Windows 上对目标被占用做有限重试；失败不留半截文件。
2. **编码与换行**——UTF-8（无 BOM）；行尾恒为 `\n`。写文本一律 `newline=""`，禁止 Windows 把 `\n` 翻成 `\r\n`（日记格式以 `\n` 为不变式）。
3. **读取**——按 `(mtime_ns, size)` 失效的进程内文本缓存；文件被外部进程原子替换后自动感知，不需要重启插件，也不每次请求重复解析。
4. **JSON 信封**——顶层必带 `schema_version`（当前为 1）；版本比本程序**新**时按原样使用、**绝不改写**；版本比当前旧且缺迁移函数时原样使用并记警告。全程不抛异常。
5. **日记正文不带版本头**——它的"schema"是头行正则，第 1 步冻结，与写入侧同源。
6. **文件权限**——原子写用 `mkstemp`，默认 0600（Windows 忽略该位）；需要跨用户读取时自行调整。
7. **进程内串行**——每文件一把 `asyncio.Lock`（`KeyedLocks`），**只保护本进程**；WebUI 是外部进程，靠"它也原子写 + 我们按 mtime 重读"协作。

## 配置

`_conf_schema.json` 与 `core/settings.py` 一一对应。校验策略：**越界夹紧、非法回落默认、未知项忽略**，原因写进 `Settings.warnings`，不抛异常。

| 字段 | 默认 | 范围 |
| :--- | :--- | :--- |
| `enabled` | true | 总开关；关闭时所有子系统不可用 |
| `timezone` | `Asia/Shanghai` | 任何 IANA 时区；无效则回落 |
| `data_dir` | 空 | 空 = 用宿主给的目录 |
| `panel.brand` | `情绪日记` | 面板左上角标题（也是浏览器标签页标题）；留空＝那行不显示；>60 字截断 |
| `panel.brand_sub` | `观察面板` | 标题下面那行小字；留空＝那行不显示 |
| `subsystems.{diary,notebook,state,proactive}` | 全 true | 子系统开关 |
| `diary.edit_within_days` | 7 | 1–60 |
| `diary.max_chars` | 1200 | 200–20000 |
| `diary.nudge_after_hour` | 22 | 0–23 |
| `diary.soft_nudge_after_hour` | 19 | 0–23 |
| `diary.event_hint_cooldown_min` | 30 | 5–1440 |
| `diary.fallback_hint_cooldown_min` | 90 | 15–1440 |
| `diary.exchange_window_min` | 30 | 5–1440 |
| `notebook.promise_limit` | 20 | 1–200；约定上限（每人，按未完成计） |
| `notebook.fact_limit` | 15 | 1–200；事实上限（每人） |
| `state.rhythm` | 5 段默认表 | 每行「HH:MM\|状态词」；到点的最近一段生效，凌晨归入最后一段；留空＝不出作息行 |
| `state.rhythm_weekend` | 空 | 周六周日生效；留空＝与工作日同一张 |
| `state.late_night` | `23:30-06:30` | 深夜时段（可跨午夜）；留空＝永不深夜；非法值恒 False |
| `scope.mode` | `private` | 启用范围档位：`owner` / `private`（默认）/ `all`；非法值回落 `private` 并记 warning（判定在 `core/scope.py`） |
| `love_peers` | `[]` | 手工指定的"最亲密"名单（去空、去重、保序） |
| `name_preference` | `{}` | `{id: 称呼}`，空称呼丢弃 |
| `proactive.patrol_minutes` | `15` | 巡检间隔（1-59）；basic job 按 `*/N * * * *` 重建 |
| `proactive.daily_limit_private` | `2` | 私聊每日上限（1-10） |
| `proactive.daily_limit_group` | `1` | 群聊每日上限（0-10，0＝群里绝不主动） |
| `proactive.min_interval_minutes` | `120` | 同会话最小间隔（0-1440） |
| `proactive.signal_cooldown_days` | `3` | 暗号独立冷却（1-30） |
| `proactive.pending_window_hours` | `6` | 待回窗口（1-48），喂 `trace_line` |
| `proactive.quiet_days` | `3` | 久未联系阈值（1-30，待调） |
| `proactive.signal_char` | `。` | 暗号字符（1~4 个字符） |

熟悉度的衰减与档位参数（λ / Δ / 单日封顶 / 阈值）与主动消息的判定阈值（暗号 valence 阈值 -0.7、劝睡 / 劝饭 / 早晚安 / 日记驱动的时段窗口）是**代码常量**（`core/state/affinity.py`、`core/proactive/`，取值与理由见"状态系统""主动消息"两节），不进配置面板——它们是算法的一部分，不是用户该调的旋钮。

## 测试

```powershell
$env:PYTHONDONTWRITEBYTECODE=1; $env:PYTHONUTF8=1
py -m unittest discover -s test -t . -v
```

- 临时目录默认落在 `test/.tmp`（已 gitignore），可用环境变量 `DIARY_TEST_TMP` 覆盖；
- **不写系统 TEMP、不用 `mkdtemp`**：受限环境下系统 TEMP 可能不允许建嵌套目录，而 `mkdtemp` 在部分沙箱下建出的目录自身权限异常；
- 内核不 import astrbot，因此本步测试不需要任何桩。

## 来源与致谢

本插件是**移植**，不是从零发明：

| 部分 | 来源 |
| :--- | :--- |
| 行为约定（头行格式、四种读模式、改删边界、提醒与冷却策略、心情词表、面板筛选） | `qq-bridge-diary`（原包 v1.0，JS/ESM）——逐条对齐 |
| 头行正则 `HEAD_RE` 与 `TAG_MAX=40` | 逐字取自原包的 `DIARY_HEAD_RE` |
| 4 个工具的描述文案 | 改编自原包的 `mcp/diary-tools.snippet.js`（去掉 `key`/`token` 参数：AstrBot 的 event 自带会话身份） |
| Python 实现、存储契约、`core/` 分层、适配器、测试 | 本仓新写 |
| AstrBot API 用法 | 对着上游源码核对，逐条出处见 `陪伴系统-框架方案.md` §九 |

原包自带的实测 bug 复盘（P1-a / P1-b / P2-b / P3-b）已在本仓转成回归测试，见 `test/test_diary.py`。

## 不做

- 安装器 / 往别人的文件里插接线（做成真插件后不需要）；
- 消息启发式的自动情感分析、自动记笔记（噪音毁信任）；
- 签到 / 打卡 / 积分 / 熟悉度游戏化；
- 独立的待办备忘系统（并入日记）。
