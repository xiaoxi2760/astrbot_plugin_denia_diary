# 桌宠状态同步 v1（第一阶段契约）

冻结日期：2026-10-27。双方实现与测试共用此目录，不独立复制另一份协议。

GET `/api/pet/v1/snapshot`，专用监听器，Authorization: Bearer <device credential>。不使用 Dashboard 管理 token，不接受 query token，不依赖 SSH，不允许浏览器跨源调用。设备凭据由已登录插件面板签发与撤销，服务端绑定 principal_id、agent_id、device_id；主体必须是配置的主人。设备仅拥有当前角色的最小只读状态权限。

成功直接返回 snapshot.schema.json 所定义对象；错误为 error.schema.json。Content-Type application/json；Cache-Control no-store；单响应 UTF-8 最多 32768 字节。401/403 清空显示和缓存、停止自动重试；404/不兼容协议停止快速重试；429 遵守 Retry-After；网络/5xx 使用有限退避。服务器不回显 token、内部异常或私有文件路径。缺省 refresh=15s、ttl=60s，实际 refresh 为 1..60 秒并提前覆盖活动边界、午夜、情绪到期；禁止以 life.revision 相同而跳过应用。快照 valid_until = server_time + ttl_seconds。客户端进程内内存缓存，不默认持久化快照；单请求、请求代次、连接切换隔离；失效及活动边界必须及时显示待刷新。

活动 id/title/location/start_at/end_at/status，location 空值统一 null；current.status=current，next.status=planned。ISO 时间须带偏移；24:00 变为次日 00:00。life.enabled=false 时 status=disabled；当前可读且有今日活动时 ready（即使此刻有空档）；无活动且生成中 planning，其余 empty；读失败 unavailable（revision=null），禁用/失败 current/next=null。规划过程已有可用计划时继续 ready。date 使用服务端时区，读失败仍可提供当天 date。

mood.status=fresh|stale|empty|disabled|unavailable；只传情绪词，不传坐标。observed_at 使用 word_at，兼容旧数据才回落 updated_at；valid_until=observed_at+6h。单改坐标不能延长旧词生命。非 fresh 时 word=null；stale 可保留 observed_at/valid_until；无词或日期无效为 empty，未来词时间不得当成 fresh。禁用/失败时三个值均 null。presentation.expression 第一阶段固定 neutral（客户端可安全回退），不对中文词另造判定。

额外未知字段允许客户端忽略，但服务端必须以白名单投影，不传日记、小本本、群消息、完整计划、关系或配置。绑定不匹配拒绝显示，不将本地角色展示名视为认证；远端 agent_id 在已配对配置中显式匹配。example-*.json 与 error-*.json 为双方共同测试夹具，不含真实凭据。

插件配置组 pet_bridge：enabled=false, host=127.0.0.1, port=6196, agent_id=yaya。首次开启或改地址热重启专用监听器，关闭/总开关关闭/terminate 释放监听。局域网使用需主人显式配置可达绑定地址；不自动修改防火墙或宿主全局鉴权。设备管理在原 Dashboard 登录保护下，凭据仅签发响应显示一次；磁盘只存哈希，列表不返回哈希或原始凭据。最多16个活跃设备，支持撤销；坏设备文件禁止读取和签发，保留原文件。

桌宠单独 stateSync，不修改现有聊天后端或 recent，不调用模型、语音、生图、QQ；UI 明示状态同步与当前聊天后端的区别。实现细节按 handoff/05、06 和客户端评审；如与此契约冲突，以本目录冻结契约为准，变更须双方同步。
