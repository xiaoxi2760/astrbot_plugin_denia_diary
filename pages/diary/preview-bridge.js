/* 离线预览用的 stub bridge（照 meme_manager 的 preview-bridge 做法）。
   **只在 file:// 或 ?preview=1 时安装**：正式 iframe 里 window.AstrBotPluginPage
   已由 Dashboard 注入（§0#2），这里什么都不做，"bridge 缺失"的红线行为保持原样。

   用法：直接用浏览器打开 pages/diary/index.html 就能看到界面、切五个 tab，不必起 AstrBot。 */
(function (global) {
  "use strict";

  var params = String(global.location && global.location.search || "");
  var isPreview = global.location && global.location.protocol === "file:" ||
    params.indexOf("preview=1") >= 0;
  if (!isPreview) return;
  if (global.AstrBotPluginPage) return;

  function iso(dayOffset, hour) {
    var d = new Date();
    d.setDate(d.getDate() - dayOffset);
    d.setHours(hour, 12, 0, 0);
    return d.toISOString();
  }

  var today = new Date();
  var todayStr = today.getFullYear() + "-" +
    String(today.getMonth() + 1).padStart(2, "0") + "-" +
    String(today.getDate()).padStart(2, "0");

  var FAKE = {
    status: {
      version: "0.4.0",
      now: new Date().toISOString(),
      who: "u_1001",
      who_name: "希",
      who_options: [{ id: "u_1001", name: "希" }, { id: "u_1002", name: "" }],
      subsystems: { diary: true, notebook: true, state: true, proactive: true },
      mood: {
        word: "安心",
        valence: 0.42,
        arousal: -0.18,
        updated_at: iso(0, 9),
        baseline: { valence: 0.2, arousal: 0.0, updated_at: iso(6, 21) },
      },
      rhythm: { word: "在看书", late_night: false },
      files: {
        "日记.txt": { exists: true, bytes: 18422, mtime: iso(0, 8) },
        "恋爱日记.txt": { exists: true, bytes: 3105, mtime: iso(1, 23) },
        "notebook.json": { exists: true, bytes: 2140, mtime: iso(0, 9) },
        "state.json": { exists: true, bytes: 320, mtime: iso(0, 9) },
        "state_history.jsonl": { exists: true, bytes: 88120, mtime: iso(0, 9) },
        "affinity.json": { exists: true, bytes: 640, mtime: iso(0, 9) },
        "proactive.json": { exists: true, bytes: 428, mtime: iso(0, 7) },
        "proactive-log.jsonl": { exists: true, bytes: 174, mtime: iso(0, 7) },
        "names.json": { exists: true, bytes: 84, mtime: iso(2, 20) },
        "diary-nudge.json": { exists: false, bytes: 0, mtime: "" },
      },
      today: { date: todayStr, by_session: 2, total: 3 },
      log_total: 6,
    },
    "diary/list": (function () {
      /* 桩也要有 days / first_date / days_truncated，否则日历是一片空白，
         离线根本没法验第 6.2 步的新界面。日期按"今天往前推"造，覆盖同一天多条。 */
      function daysAgo(n) { var d = new Date(); d.setDate(d.getDate() - n); return d.toISOString().slice(0, 10); }
      function fakeDays() {
        var out = [{ date: daysAgo(1), count: 2, chars: 420 }, { date: daysAgo(3), count: 1, chars: 210 },
                   { date: daysAgo(4), count: 3, chars: 700 }, { date: daysAgo(9), count: 1, chars: 180 },
                   { date: daysAgo(17), count: 2, chars: 350 }];
        return out;
      }
      var normalDays = fakeDays();
      return {
        ok: true,
        love_collapsed: true,
        books: [
          { book: "normal", display: "日记", is_love: false, entries: 42, chars: 18220,
            updated_at: iso(0, 8), latest_date: normalDays[0].date,
            days: normalDays, first_date: normalDays[0].date, days_truncated: true },
          { book: "love", display: "恋爱日记", is_love: true, entries: 7, chars: 3080,
            updated_at: iso(1, 23), latest_date: daysAgo(1),
            days: [normalDays[1]], first_date: normalDays[1].date, days_truncated: false },
        ],
      };
    })(),
    "diary/content": (function () {
      function daysAgo(n) { var d = new Date(); d.setDate(d.getDate() - n); return d.toISOString().slice(0, 10); }
      var entries = [
        { date: daysAgo(1), time: "09:12", mood: "安心", who: "10001", chars: 46,
          text: "今天把窗户推开了一条缝，外面的风终于不冷了。\n\n她记下了这句，没写为什么。" },
        { date: daysAgo(1), time: "21:13", mood: "", who: "10001", chars: 28,
          text: "晚上读到一半就困了，书还摊在桌上。" },
        { date: daysAgo(3), time: "22:40", mood: "有点累", who: "99999", chars: 30,
          text: "今天没怎么说话。\n\n但也没有不开心。" },
      ];
      return {
        ok: true, book: "normal", date: "", count: entries.length, total: 42,
        text: entries.map(function (e) { return e.text; }).join("\n\n"),
        entries: entries,
      };
    })(),
    notebook: {
      ok: true, who: "u_1001", who_name: "希",
      who_options: [{ id: "u_1001", name: "希" }, { id: "u_1002", name: "" }],
      facts: [
        { id: "f1", text: "达妮娅不喝咖啡，只喝热牛奶", created_at: iso(20, 10) },
        { id: "f2", text: "达妮娅怕打雷", created_at: iso(12, 22) },
      ],
      promises: [
        { id: "p1", about: "u_1001", text: "周末一起去看展", created_at: iso(3, 19), done_at: "" },
        { id: "p2", about: "u_1001", text: "把那本书读完讲给达妮娅听", created_at: iso(9, 21), done_at: iso(1, 20) },
      ],
      limits: { promise_limit: 20, fact_limit: 15 },
    },
    affinity: {
      ok: true, total_known: 3,
      love_peers: ["u_1001"],
      people: [
        { id: "u_1001", name: "希", score: 12.4, band: "熟稔", last_ts: iso(0, 9) },
        { id: "u_1002", name: "u_1002", score: 4.1, band: "泛泛", last_ts: iso(5, 18) },
        { id: "u_1003", name: "u_1003", score: 0.8, band: "陌生", last_ts: iso(20, 11) },
      ],
    },
    history: {
      ok: true, days: 30, truncated: false,
      points: Array.from({ length: 30 }, function (_, i) {
        var v = Math.sin(i / 3.2) * 0.6;
        var a = Math.cos(i / 4.1) * 0.4;
        return { date: iso(29 - i, 20).slice(0, 10), layer: "now", valence: Number(v.toFixed(3)), arousal: Number(a.toFixed(3)), n: 1 };
      }).concat([
        { date: iso(20, 21).slice(0, 10), layer: "baseline", valence: 0.21, arousal: 0.02, n: 1 },
        { date: iso(9, 22).slice(0, 10), layer: "baseline", valence: 0.14, arousal: -0.05, n: 1 },
      ]),
    },
    proactive: {
      ok: true, log_total: 6,
      sessions: [
        { umo: "platform:qq:private:1001", today_count: 2, last_slot: "greeting", last_sent_at: iso(0, 7) },
        { umo: "platform:qq:group:2002", today_count: 1, last_slot: "night", last_sent_at: iso(1, 23) },
      ],
      log: [
        { ts: iso(0, 7), umo: "platform:qq:private:1001", slot: "greeting", fragment: "早，今天也要好好的。" },
        { ts: iso(1, 23), umo: "platform:qq:group:2002", slot: "night", fragment: "这么晚还没睡？" },
        { ts: iso(2, 12), umo: "platform:qq:private:1001", slot: "idle", fragment: "刚吃完饭，窗外那只猫又来了。" },
      ],
    },
    /* 设置（第 5.2 步）：桩数据。字段表在真机上来自 GET settings（schema 驱动），
       这里手写一小份，**只为离线预览时能看见六种控件长什么样**——真数据以后端为准。 */
    settings: {
      ok: true,
      groups: [
        { path: "enabled", key: "enabled", type: "bool", description: "总开关",
          hint: "关闭后日记、小本本、状态、主动消息全部不可用（数据保留）" },
        { path: "timezone", key: "timezone", type: "string", description: "时区",
          hint: "IANA 时区名。部署在 UTC 云主机上必须显式设置" },
        { path: "data_dir", key: "data_dir", type: "string", description: "数据目录",
          hint: "留空使用 AstrBot 的插件数据目录", editable: false,
          note: "数据目录只能在这里看。改它＝搬走全部日记与本子，必须在 AstrBot 插件面板改完再重载插件。" },
        { path: "diary", key: "diary", type: "object", description: "日记参数", hint: "",
          children: [
            { path: "diary.max_chars", key: "max_chars", type: "int", description: "单条上限（字）",
              hint: "200-20000", default: 1200, min: 200, max: 20000, editable: true, note: "" },
            { path: "diary.nudge_after_hour", key: "nudge_after_hour", type: "int", description: "深夜催促时刻",
              hint: "0-23", default: 22, min: 0, max: 23, editable: true, note: "" },
          ] },
        { path: "state", key: "state", type: "object", description: "状态参数（情绪 / 作息）",
          hint: "作息表是「时段 → 状态词」的纯函数，不落盘",
          children: [
            { path: "state.rhythm", key: "rhythm", type: "string", description: "作息表（工作日）",
              hint: "每行「HH:MM|状态词」", default: "06:00|刚醒\n09:00|精神不错\n23:00|该睡了",
              multiline: true, editable: true, note: "", editor: "rhythm" },
            { path: "state.rhythm_weekend", key: "rhythm_weekend", type: "string", description: "作息表（周末）",
              hint: "留空＝沿用工作日", default: "", multiline: true, editable: true, note: "", editor: "rhythm" },
            { path: "state.late_night", key: "late_night", type: "string", description: "深夜时段",
              hint: "HH:MM-HH:MM，可跨午夜；留空＝永不深夜", default: "23:30-06:30",
              editable: true, note: "", editor: "time_range" },
          ] },
        { path: "scope", key: "scope", type: "object", description: "启用范围",
          hint: "面板本身永远可用，这一档只管达妮娅在哪儿工作",
          children: [
            { path: "scope.mode", key: "mode", type: "string", description: "启用范围",
              hint: "启用范围：只主人 / 只私聊 / 全部启用",
              default: "private", editable: true, note: "", editor: "scope",
              options: ["owner", "private", "all"] },
          ] },
        { path: "love_peers", key: "love_peers", type: "list", description: "最亲密名单",
          hint: "填用户 id（字符串）。名单内的人的私聊日记进入“恋爱日记”",
          default: [], editable: true, note: "" },
        { path: "name_preference", key: "name_preference", type: "dict", description: "称呼偏好",
          hint: "{\"用户id\": \"称呼\"}。只影响日记标注里怎么写", default: {}, editable: true, note: "" },
      ],
      fields: [],
      values: {
        enabled: true, timezone: "Asia/Shanghai", data_dir: "",
        diary: { max_chars: 1200, nudge_after_hour: 22 },
        state: { rhythm: "06:00|刚醒\n09:00|精神不错\n13:00|有点犯困\n18:00|晚饭后放松\n23:00|该睡了",
                 rhythm_weekend: "", late_night: "23:30-06:30" },
        scope: { mode: "private" },
        love_peers: ["u_1001"], name_preference: { u_1001: "希" },
      },
      defaults: {
        enabled: true, timezone: "Asia/Shanghai", data_dir: "",
        diary: { max_chars: 1200, nudge_after_hour: 22 },
        state: { rhythm: "06:00|刚醒\n09:00|精神不错\n23:00|该睡了" },
        scope: { mode: "private" },
        love_peers: [], name_preference: {},
      },
      warnings: ["proactive.patrol_minutes=999 超出 [1, 59]，已夹紧"],
      editable_paths: ["enabled", "timezone", "diary.max_chars", "diary.nudge_after_hour",
        "state.rhythm", "scope.mode", "love_peers", "name_preference"],
      notices: ["关掉总开关后日记、小本本、状态、主动消息全部不可用（数据保留）。"],
    },
  };

  /* 立绘桩：内存仓库，够点切换 / 上传 / 删除走通整条链路。
     用 SVG data_url 而不是真 jpg——file:// 下 fetch 本地文件会被拦，
     内联 SVG 不依赖任何外部读取。（真机走的是接口回的 data_url，同一条路。） */
  /* 嵌套 → 点分路径（settings/reset 桩要算「哪些项真的变了」） */
  function flattenValues(value, prefix, out) {
    out = out || {};
    prefix = prefix || "";
    Object.keys(value || {}).forEach(function (key) {
      var item = value[key];
      var path = prefix ? prefix + "." + key : key;
      if (item && typeof item === "object" && !Array.isArray(item)) {
        flattenValues(item, path, out);
      } else {
        out[path] = item;
      }
    });
    return out;
  }

  function stubPortrait(label, w, h, fill) {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">' +
      '<rect width="' + w + '" height="' + h + '" fill="' + fill + '"/>' +
      '<text x="50%" y="50%" font-size="26" text-anchor="middle" fill="#8b7480">' + label + "</text></svg>";
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }

  var portraitStore = {
    current: "p_preview1",
    items: [
      { id: "p_preview1", name: "预览图一.svg", mime: "image/svg+xml", bytes: 512,
        created_at: "2026-10-06T01:00:00", data_url: stubPortrait("预览立绘 1（横）", 480, 300, "#f7dee7") },
      { id: "p_preview2", name: "预览图二.svg", mime: "image/svg+xml", bytes: 512,
        created_at: "2026-10-06T01:05:00", data_url: stubPortrait("预览立绘 2（竖）", 300, 480, "#e8f2ef") },
    ],
  };

  function copyPortrait() {
    return JSON.parse(JSON.stringify(portraitStore));
  }

  /* 「对谁」候选（第 8 步契约）：每项带 label / is_owner / out_of_scope。
     **out_of_scope 随当前档位变**，所以切档之后前端重拉就能看见下拉分区真的变了。
     没名字的那项 label 就等于 id —— 不会出现 "1411638634(1411638634)"。 */
  function currentScope() {
    return (((FAKE.settings.values || {}).scope || {}).mode) || "private";
  }

  function whoOptions(rich) {
    var mode = currentScope();
    var list = [
      { id: "u_1001", name: "希", label: "希(u_1001)", is_owner: true, out_of_scope: false },
      /* u_1002 故意**不给 label**（模拟老后端）→ 前端该退回 name || id，
         而它没名字，于是就是裸 id —— 正是用户截图里看到的那一档。 */
      { id: "u_1002", name: "", is_owner: false, out_of_scope: false },
      { id: "u_1003", name: "小满", label: "小满(u_1003)", is_owner: false, out_of_scope: mode === "owner" },
    ];
    /* /notebook 那轮信息更全：用来验"同一 id 后来居上"。 */
    if (rich) list[1] = { id: "u_1002", name: "路人甲", label: "路人甲(u_1002)", is_owner: false, out_of_scope: false };
    return list;
  }

  global.AstrBotPluginPage = {
    ready: function () { return Promise.resolve(); },
    apiGet: function (endpoint, params) {
      if (endpoint === "portrait") return Promise.resolve(copyPortrait());
      var data = FAKE[endpoint];
      if (!data) return Promise.reject(new Error("预览桩没有这个 endpoint：" + endpoint));
      var out = JSON.parse(JSON.stringify(data));
      /* /status 与 /notebook 都要带候选和当前档位（第 8 步契约）；
         桩里**随档位变**，切档后前端重拉能看见下拉分区真的变了。 */
      if (endpoint === "status" || endpoint === "notebook") {
        out.who_options = whoOptions(endpoint === "notebook");
        out.scope_mode = currentScope();
      }
      return Promise.resolve(out);
    },
    apiPost: function (endpoint, body) {
      if (endpoint === "portrait/select") {
        var want = body && body.id;
        var hit = portraitStore.items.some(function (x) { return x.id === want; });
        if (!hit) return Promise.reject(new Error("没有这张："));
        portraitStore.current = want;
        return Promise.resolve({ ok: true, current: want });
      }
      if (endpoint === "portrait/delete") {
        var gone = body && body.id;
        portraitStore.items = portraitStore.items.filter(function (x) { return x.id !== gone; });
        if (portraitStore.current === gone) {
          portraitStore.current = portraitStore.items.length ? portraitStore.items[0].id : null;
        }
        return Promise.resolve({ ok: true });
      }
      if (endpoint === "notebook/complete" || endpoint === "notebook/delete") {
        return Promise.resolve({ ok: true, kind: "promise", id: (body && body.id) || "", text: "（预览桩）" });
      }
      if (endpoint === "settings") {
        var changes = (body && body.changes) || {};
        /* 桩也**真的**写回 values（点分路径），这样顶栏切档 / 设置页保存之后
           重新拉设置能看见值真的变了，才算走通，而不是只弹个假响应。 */
        Object.keys(changes).forEach(function (path) {
          var parts = String(path).split(".");
          var node = FAKE.settings.values;
          for (var i = 0; i < parts.length - 1 && node && typeof node === "object"; i += 1) {
            if (!(parts[i] in node)) node[parts[i]] = {};
            node = node[parts[i]];
          }
          if (node && typeof node === "object") node[parts[parts.length - 1]] = changes[path];
        });
        return Promise.resolve({
          ok: true, applied: Object.keys(changes), warnings: [], reloaded: false,
          backup: "config_backup_20261006-010203.json",
          notices: ["（预览桩：写在内存里，没真的落盘）"],
        });
      }
      if (endpoint === "settings/reset") {
        /* 桩也**真的**把 values 换回 defaults：这样「两步确认 → 落盘 → 重拉设置」
           这条链在预览里能看见表单真的变了，才算走通，而不是只弹个假响应。 */
        var flatNow = flattenValues(FAKE.settings.values);
        var flatDef = flattenValues(FAKE.settings.defaults);
        var changed = Object.keys(flatDef).filter(function (k) {
          return JSON.stringify(flatNow[k]) !== JSON.stringify(flatDef[k]);
        });
        FAKE.settings.values = JSON.parse(JSON.stringify(FAKE.settings.defaults));
        return Promise.resolve({
          ok: true,
          changed: changed,
          errors: [],
          warnings: [],
          reloaded: changed.length > 0,
          backup: changed.length ? "astrbot_config.json.bak-20261006-023311" : "",
          notices: [changed.length
            ? "（预览桩）已把 " + changed.length + " 项恢复为默认值（默认值只来自 _conf_schema.json）。"
            : "所有配置本来就是默认值，什么都没改。"],
        });
      }
      return Promise.reject(new Error("预览桩没有这个 endpoint：" + endpoint));
    },
    /* 上传桩：真读文件、转 data_url、塞进仓库，这样上传链路能离线跑通。
     字段名固定 file、只发一个文件——与官方 bridge.upload 的约定一致。 */
    upload: function (endpoint, file) {
      if (endpoint !== "portrait/upload") {
        return Promise.reject(new Error("预览桩不支持上传到 " + endpoint));
      }
      if (!file) return Promise.reject(new Error("没拿到文件"));
      if (file.size > 8 * 1024 * 1024) return Promise.reject(new Error("太大了（桩上限 8 MB）"));
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onerror = function () { reject(new Error("读不了这个文件")); };
        reader.onload = function () {
          var id = "p_preview" + (portraitStore.items.length + 1) + "_" + Date.now();
          var item = {
            id: id,
            name: file.name || "上传图片",
            mime: file.type || "image/jpeg",
            bytes: file.size,
            created_at: new Date().toISOString(),
            data_url: String(reader.result || ""),
          };
          portraitStore.items = portraitStore.items.concat([item]);
          portraitStore.current = id;
          resolve({ ok: true, id: id, item: item });
        };
        reader.readAsDataURL(file);
      });
    },
    onContext: function (cb) { try { cb && cb({ theme: "dark" }); } catch (e) { /* 预览桩忽略 */ } },
    getContext: function () { return { theme: "dark" }; },
    subscribeSSE: function () { return 0; },
    unsubscribeSSE: function () { /* 预览桩忽略 */ },
  };
})(window);
