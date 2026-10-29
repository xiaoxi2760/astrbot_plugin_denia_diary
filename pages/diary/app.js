/* 面板入口：bridge 就绪、endpoint 封装、视图切换、"对谁"选择器。

   三条硬约束（任务书 §0）：
   - endpoint **不带前导斜杠**（写 "status" 而不是 "/status"）；
   - endpoint **不带插件名前缀**（Dashboard 转发时才补上）；
   - 前端资源**相对路径**（./app.js），Dashboard 会重写引用。

   拿不到 bridge 就显示"请从插件详情页打开本页面"，**不许白屏**。 */
(function (global) {
  "use strict";

  var UI = global.UI;

  /* 全部 endpoint 字面量集中在这里：离线路径对账脚本扫的就是这张表，
     与 web_api/routes.py 的 ROUTES 双向一一对应。 */
  var ENDPOINTS = {
    status: "status",
    life: "life",
    lifeGenerate: "life/generate",
    lifeReplace: "life/replace",
    lifeRecord: "life/record",
    diaryList: "diary/list",
    diaryContent: "diary/content",
    diaryRewrite: "diary/rewrite",
    diaryDelete: "diary/delete",
    diaryTrash: "diary/trash",
    diaryRestore: "diary/restore",
    diaryCard: "diary/card",
    diaryCardImage: "diary/card-image",
    diaryComments: "diary/comments",
    diaryCommentSave: "diary/comments/save",
    diaryCommentPublish: "diary/comments/publish",
    diaryCommentWithdraw: "diary/comments/withdraw",
    diaryCommentDelete: "diary/comments/delete",
    notebook: "notebook",
    notebookComplete: "notebook/complete",
    notebookDelete: "notebook/delete",
    affinity: "affinity",
    history: "history",
    proactive: "proactive",
    social: "social",
    socialGroup: "social/group",
    socialParticipant: "social/participant",
    socialPreview: "social/preview",
    voiceStatus: "voice/status",
    settings: "settings",
    settingsReset: "settings/reset",
    petDevices: "pet/devices",
    petDeviceIssue: "pet/devices/issue",
    petDeviceRevoke: "pet/devices/revoke",
    portrait: "portrait",
    portraitUpload: "portrait/upload",
    portraitSelect: "portrait/select",
    portraitDelete: "portrait/delete",
    imagery: "imagery",
    imageryImage: "imagery/image",
    imageryGenerate: "imagery/generate",
    /* ---- 三端整合 P0：实例（scope）清单 ----
       GET instances → {ok, isolation_enabled, legacy_scope_id,
                        items:[{scope_id,label,legacy}], default_scope_id?} */
    instances: "instances",
    /* 隔离模式下立绘上传走这个 JSON 接口（官方 bridge.upload 只收一个文件、
       且禁止拼 query，所以隔离模式必须换成 data_url 走 POST）。
       body: {scope_id, name, data_url} */
    portraitUploadData: "portrait/upload-data",
    coreSnapshot: "core/snapshot",
    coreAction: "core/action",
    petChatDevices: "pet/chat-devices",
    petChatDeviceIssue: "pet/chat-devices/issue",
    petChatDeviceRevoke: "pet/chat-devices/revoke",
  };

  /* **不带 scope 的接口**：实例清单本身（拿它才能知道有哪些实例）。
     宿主级管理动作（bot_isolation / per_instance 原始路由 / 总开关）由后端处理，
     前端不往这些业务接口里塞 scope_id。 */
  var HOST_ENDPOINTS = { instances: true };

  var TABS = [
    { key: "overview", title: "总览", note: "此刻状态" },
    { key: "life", title: "今天", note: "她的安排与进展" },
    { key: "imagery", title: "生图", note: "画一下现在的她（按需）" },
    { key: "diary", title: "日记", note: "改 · 删 · 回收站" },
    { key: "notebook", title: "小本本", note: "事实与约定" },
    { key: "affinity", title: "熟悉度", note: "榜与档位" },
    { key: "proactive", title: "曲线与主动", note: "情绪曲线 + 主动消息" },
    { key: "social", title: "群社交", note: "登记 · 话题 · bot 短聊" },
    { key: "core", title: "核心", note: "人设 · 历史 · 长期记忆 · 好感度与观测值" },
    { key: "devices", title: "桌宠连接", note: "统一聊天 · 设备配对与撤销" },
    { key: "data", title: "数据", note: "坐标 · 数据文件" },
    { key: "settings", title: "设置", note: "全部配置项，改完即生效" },
  ];

  var WHO_KEY = "denia.diary.who";
  /* 写回用的键。**全文件只此一处**出现这个路径——控件选哪个档、怎么显示，
     都只看 UI.SCOPE_MODES 和后端给的 editor，不按路径名判断。 */
  var SCOPE_PATH = "scope.mode";
  /* 实例（scope）选择记在这台设备上。 */
  var SCOPE_KEY = "denia.diary.scope";
  var state = {
    who: "", options: [], current: "overview", view: null, ctx: null, scope: "private",
    /* ---- 三端整合 P0：多实例 ----
       instances: [{scope_id,label,legacy}]（**只从后端给的 items 里选，不猜适配器类型**）
       isolationEnabled: 后端说隔离开了没。关着就完全走老行为（不发 scope_id）。
       scopeId: 当前选中的实例；空串＝还没选（隔离开启时所有实例业务请求都会被拒）。
       legacyScopeId: 后端指明的旧实例 id。 */
    instances: [], isolationEnabled: false, legacyScopeId: "", defaultScopeId: "",
    scopeId: "", scopeEpoch: 0,
    whoByScope: Object.create(null),
  };
  var viewGeneration = 0;
  var mountedKey = "";
  var scrollPositions = Object.create(null);
  /* 每个 scope 一份 ctx（含绑定的 api）。同一 scope 复用同一个 ctx
     ——视图里的草稿缓存（social 用 ctx 当 WeakMap 键）切 tab 才不丢。 */
  var ctxCache = Object.create(null);
  var scopeBuckets = Object.create(null);
  var scopeEpoch = 0;

  function bridge() { return global.AstrBotPluginPage || null; }

  /* ---- api 封装：统一把后端错误变成可读信息，绝不让 view 白屏 ----
     ⚠️ 三端整合 P0：scope 绑定是**按 ctx** 的，不是按全局当前值。
        每个 view 拿到的 ctx 在 mount 时冻结了一个 scope_id，
        之后它发出的每一次 GET/POST/upload 都固定带那一个 scope——
        切换实例后旧 ctx 再写，必须被拒绝（见 apiPostScoped）。 */
  function scopeParam(key, scopeId) {
    if (!state.isolationEnabled) return null;          /* 隔离开关关着＝老行为，一个字段都不加 */
    if (HOST_ENDPOINTS[key]) return null;               /* 实例清单本身不带 scope */
    if (!scopeId) return null;                         /* 没选实例＝请求被拒（见下面） */
    return scopeId;
  }

  async function apiGetScoped(scopeId, key, params) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    var p = Object.assign({}, params || {});
    var epoch = scopeEpoch;
    var scoped = scopeParam(key, scopeId);
    if (state.isolationEnabled && !HOST_ENDPOINTS[key]) {
      if (!scoped) throw new Error("还没选实例：请先在左上角选一个实例");
      p.scope_id = scoped;
    }
    try {
      var result = await b.apiGet(ENDPOINTS[key], p);
      /* ⚠️ 迟到保护：请求发出后实例被切走了，这份响应**作废**——
         旧实例的数据不许画到新实例的视图上。 */
      if (state.isolationEnabled && !HOST_ENDPOINTS[key] && (scopeId !== state.scopeId || epoch !== scopeEpoch)) {
        throw new Error("实例已切换，这次请求已作废");
      }
      return result;
    } catch (error) {
      throw new Error(String((error && error.message) || error || "请求失败"));
    }
  }

  async function apiPostScoped(scopeId, key, body) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    var payload = Object.assign({}, body || {});
    var epoch = scopeEpoch;
    if (state.isolationEnabled && !HOST_ENDPOINTS[key]) {
      if (!scopeId) throw new Error("还没选实例：请先在左上角选一个实例");
      /* 旧 ctx（切换实例前的）在新实例下发写请求 → 直接拒绝，
         绝不把 A 实例的草稿提交给 B。 */
      if (scopeId !== state.scopeId) throw new Error("实例已切换，这次操作没有提交");
      payload.scope_id = scopeId;
    }
    try {
      var result = await b.apiPost(ENDPOINTS[key], payload);
      if (state.isolationEnabled && !HOST_ENDPOINTS[key] && (scopeId !== state.scopeId || epoch !== scopeEpoch)) {
        throw new Error("实例已切换，原实例操作结果不会覆盖当前页面");
      }
      return result;
    } catch (error) {
      throw new Error(String((error && error.message) || error || "请求失败"));
    }
  }

  /* 立绘上传。官方 bridge 的 upload() 只发一个文件、字段名固定 file，
     所以这里只能传单个 File；多图靠多次调用。宿主没给 upload 就明说，
     不要静默失败让用户以为传上去了。
     ⚠️ 隔离开启时**不能**用它：官方 bridge 的 endpoint 禁止拼 query，
        ?scope_id=… 会被拒；upload() 也只收一个 file、没有 body。
        所以隔离模式改走 JSON 接口 portrait/upload-data：
        {scope_id, name, data_url}——FileReader 把 ≤8MiB 的图读成 data URL。
        非隔离模式仍走原 bridge.upload（原行为一字不变）。 */
  var MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

  function readAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      if (typeof global.FileReader !== "function") { reject(new Error("这个运行环境不能读取本地文件")); return; }
      var reader = new global.FileReader();
      reader.onerror = function () { reject(new Error("读取文件失败")); };
      reader.onload = function () { resolve(String(reader.result || "")); };
      reader.readAsDataURL(file);
    });
  }

  async function apiUploadScoped(scopeId, key, file) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    if (!file) throw new Error("没有拿到文件");
    if (Number(file.size || 0) > MAX_UPLOAD_BYTES) throw new Error("太大了：最多 8 MB");
    if (state.isolationEnabled) {
      var dataUrl = await readAsDataUrl(file);
      return await apiPostScoped(scopeId, "portraitUploadData", {
        scope_id: scopeId,
        name: String(file.name || "上传图片"),
        data_url: dataUrl,
      });
    }
    if (typeof b.upload !== "function") throw new Error("这个运行环境不支持上传");
    try {
      return await b.upload(ENDPOINTS[key], file);
    } catch (error) {
      throw new Error(String((error && error.message) || error || "上传失败"));
    }
  }

  /* ---- 实例（scope）选择 ----
     三端整合 P0：面板一次只对一个实例说话。所有实例业务请求都带 scope_id，
     隔离关闭时完全退回老行为（一个字段都不多发）。 */
  function readScopeFromUrl() {
    try {
      var search = String(global.location && global.location.search || "");
      var match = search.match(/[?&]scope=([^&]*)/);
      return match ? decodeURIComponent(match[1]) : "";
    } catch (error) { return ""; }
  }

  function storedScope() {
    try { return global.localStorage.getItem(SCOPE_KEY) || ""; } catch (error) { return ""; }
  }

  function storeScope(value) {
    try { global.localStorage.setItem(SCOPE_KEY, value || ""); } catch (error) { /* 忽略 */ }
  }

  function scopeItem(scopeId) {
    return state.instances.filter(function (one) { return one && one.scope_id === scopeId; })[0] || null;
  }

  function scopeLabelOf(scopeId) {
    var item = scopeItem(scopeId);
    if (!item) return String(scopeId || "");
    return String(item.label || item.scope_id || scopeId) + (item.legacy ? "（旧实例）" : "");
  }

  /* 每个 scope 一份 UI 状态：候选人 / 选中的人。切实例时互不串。 */
  function bucket(scopeId) {
    var key = String(scopeId || "");
    if (!scopeBuckets[key]) {
      scopeBuckets[key] = { options: [], who: "" };
    }
    return scopeBuckets[key];
  }

  /* 把某个 scope 的状态**接管**到当前面板：候选人、选中的人都换成它的。 */
  function activateScope(scopeId) {
    var b = bucket(scopeId);
    state.scopeId = String(scopeId || "");
    state.options = b.options;
    state.who = b.who || "";
  }

  /* 落位顺序：**存储/URL 里仍然有效的 scope → 显式 legacy → 有效默认**。
     绝不偷偷落到 legacy 上：只有用户明确选了 legacy 才用 legacy。 */
  function pickInitialScope(payload) {
    var items = (payload && payload.items) || [];
    if (!items.length) return "";
    var wanted = readScopeFromUrl() || storedScope();
    if (wanted && scopeItem(wanted)) return wanted;
    var legacyId = String((payload && payload.legacy_scope_id) || "");
    /* 显式 legacy：URL 里写了 legacy 关键字，或存储的就是 legacy_scope_id。 */
    if (wanted === "legacy" && legacyId && scopeItem(legacyId)) return legacyId;
    var def = String((payload && payload.default_scope_id) || "");
    if (def && scopeItem(def)) return def;
    /* 只有一条路可走时它就是"有效默认"（含唯一的那条 legacy）。 */
    if (items.length === 1) return String(items[0].scope_id || "");
    return "";
  }

  async function loadInstances() {
    try {
      var payload = await bridge().apiGet(ENDPOINTS.instances, {});
      var items = (payload && payload.items) || [];
      state.instances = items.filter(function (one) { return one && one.scope_id; }).map(function (one) {
        return {
          scope_id: String(one.scope_id),
          label: String(one.label || one.scope_id || ""),
          legacy: !!one.legacy,
        };
      });
      state.isolationEnabled = !!(payload && payload.isolation_enabled);
      state.legacyScopeId = String((payload && payload.legacy_scope_id) || "");
      state.defaultScopeId = String((payload && payload.default_scope_id) || "");
      /* 只有真隔离开了才选实例。隔离开关关着＝单实例老行为，一个 scope 都不选。 */
      var picked = state.isolationEnabled ? pickInitialScope(payload) : "";
      if (picked) {
        state.scopeId = picked;
        storeScope(picked);
      }
      return true;
    } catch (error) {
      state.instances = [];
      state.scopeId = "";
      state.instanceError = String((error && error.message) || error);
      return false;
    }
  }

  /* 实例下拉：只在隔离开启时出现。选项**只来自后端给的 items**，不猜适配器类型。 */
  function renderScopePicker() {
    var picker = document.getElementById("scope-picker");
    var hint = document.getElementById("scope-hint");
    var wrap = document.getElementById("scope-picker-wrap");
    if (!picker) return;
    if (wrap) wrap.hidden = !state.isolationEnabled;
    if (!state.isolationEnabled) {
      UI.clear(picker);
      if (hint) hint.textContent = "";
      return;
    }
    UI.clear(picker);
    if (!state.instances.length) {
      picker.appendChild(UI.h("option", { value: "", text: "（没有可用实例）" }));
    } else {
      if (!state.scopeId) {
        picker.appendChild(UI.h("option", { value: "", text: "（请选择实例）", selected: true }));
      }
      state.instances.forEach(function (item) {
        picker.appendChild(UI.h("option", {
          value: item.scope_id,
          text: scopeLabelOf(item.scope_id),
          title: item.scope_id,
          selected: item.scope_id === state.scopeId,
        }));
      });
    }
    if (picker.setAttribute) {
      picker.setAttribute("title", state.scopeId
        ? "当前实例：" + scopeLabelOf(state.scopeId)
        : "还没选实例：所有实例操作都要先选");
    }
    if (hint) {
      hint.textContent = state.scopeId
        ? "实例：" + scopeLabelOf(state.scopeId)
        : "还没选实例";
    }
  }

  /* 切换实例：清掉旧人的候选/统计/立绘/视图，重新按新实例加载。
     没选实例（隔离开启但没选）时，所有实例业务请求都会被拒——
     这里明确提示，不偷偷挑一个。 */
  async function switchInstance(nextId) {
    var next = String(nextId || "");
    var prev = state.scopeId;
    if (!next || next === prev) { renderScopePicker(); return false; }
    if (!state.isolationEnabled || !scopeItem(next)) { renderScopePicker(); return false; }
    if (state.view && typeof state.view.hasUnsavedChanges === "function" && state.view.hasUnsavedChanges()) {
      UI.toast("请先保存或取消当前表单，再切换实例。", true);
      renderScopePicker();
      return false;
    }
    /* 只有**真有未保存内容**时才问。草稿本身按 scope 存着（旧实例的草稿
       不会被提交到新实例，也不会丢），所以没有草稿时直接切，不必打断用户。 */
    if (hasUnsavedWork()) {
      var ok = await confirmDiscard("当前实例里有没保存的内容。换实例后它仍留在这个实例里，不会提交到新实例。继续？");
      if (!ok) { renderScopePicker(); return false; }
    }
    applyScopeSwitch(prev, next);
    var selectedEpoch = scopeEpoch;
    namePref = {};
    state.scope = "private";
    try {
      var settings = await apiGetScoped(next, "settings", {});
      if (selectedEpoch !== scopeEpoch) return false;
      loadNamePref(settings && settings.values);
      applyPanel(settings && settings.values && settings.values.panel);
      applyScopeMode(settings && settings.values && settings.values.scope && settings.values.scope.mode);
      var status = await apiGetScoped(next, "status", { who: state.who });
      if (selectedEpoch !== scopeEpoch) return false;
      mergeOptions(status && status.who_options);
    } catch (error) { /* The view displays the current request error. */ }
    if (selectedEpoch !== scopeEpoch) return false;
    await show(state.current);
    return true;
  }

  /* 切实例：换掉 scope、清掉旧人的候选/统计/立绘/视图，重新加载。 */
  function applyScopeSwitch(prev, next) {
    state.scopeId = next;
    storeScope(next);
    /* 旧实例的候选/选中的人存进桶里，切回来还在。 */
    if (prev) { bucket(prev).who = state.who; bucket(prev).options = state.options; }
    activateScope(next);
    scopeEpoch += 1;
    viewGeneration += 1;
    state.ctx = ctxFor(next);
    state.diaryDate = "";
    var image = document.getElementById("portrait-img");
    if (image) { image.hidden = true; image.src = ""; }
    var count = document.getElementById("portrait-count");
    if (count) count.textContent = "";
    renderScopePicker();
    renderWhoPicker();
  }

  /* 未保存内容检查：日记正文草稿 / 留言草稿（都存在共享 state.diaryUI 里）。
     有未保存内容时，换实例前必须问一句——不能悄悄把内容丢掉。 */
  function hasUnsavedWork() {
    var root = (state.diaryUI && typeof state.diaryUI === "object") ? state.diaryUI : null;
    if (!root) return false;
    var found = false;
    Object.keys(root).forEach(function (who) {
      if (state.isolationEnabled && who.indexOf(state.scopeId + "|") !== 0) return;
      var box = root[who];
      if (!box || found) return;
      ["edits", "comments"].forEach(function (field) {
        var map = box[field];
        if (!map || typeof map !== "object") return;
        Object.keys(map).forEach(function (k) {
          if (String(map[k] || "").trim()) found = true;
        });
      });
    });
    return found;
  }

  /* 页面内确认（不是 window.confirm）：返回 true＝继续，false＝取消。 */
  function confirmDiscard(message) {
    if (!message) return Promise.resolve(true);
    var box = document.getElementById("scope-confirm");
    if (!box) return Promise.resolve(true);
    var text = document.getElementById("scope-confirm-text");
    if (text) text.textContent = message;
    box.hidden = false;
    return new Promise(function (resolve) {
      function done(value) {
        box.hidden = true;
        box.__resolve = null;
        resolve(value);
      }
      box.__resolve = done;
      if (!box.__wired) {
        box.__wired = true;
        var ok = document.getElementById("scope-confirm-ok");
        var cancel = document.getElementById("scope-confirm-cancel");
        if (ok) ok.addEventListener("click", function () { if (box.__resolve) box.__resolve(true); });
        if (cancel) cancel.addEventListener("click", function () { if (box.__resolve) box.__resolve(false); });
      }
    });
  }

  /* ---- "对谁"：URL ?who= 覆盖 localStorage（方便直接分享某一页） ---- */
  function readWhoFromUrl() {
    try {
      var search = String(global.location && global.location.search || "");
      var match = search.match(/[?&]who=([^&]*)/);
      return match ? decodeURIComponent(match[1]) : "";
    } catch (error) { return ""; }
  }

  function storedWho() {
    try { return global.localStorage.getItem(WHO_KEY + (state.isolationEnabled ? ":" + state.scopeId : "")) || ""; } catch (error) { return ""; }
  }

  function storeWho(value) {
    try { global.localStorage.setItem(WHO_KEY + (state.isolationEnabled ? ":" + state.scopeId : ""), value || ""); } catch (error) { /* 无痕模式，忽略 */ }
  }

  /* 档位外那组。**候选一个都不删**——面板是主人视角、永远可用，
     这里只是把"她在这些会话里不会工作"讲清楚，不是把人藏起来。 */
  var OUT_SCOPE_LABEL = "档位外（她不会在这些会话里工作）";

  /* 同一 id 后来居上：/status 那轮可能只有 {id,name}，/notebook 那轮才带上
     label / is_owner / out_of_scope。**不按字段名挑**，有更全的就补上。 */
  function mergeOptions(options) {
    (options || []).forEach(function (item) {
      if (!item || !item.id) return;
      var id = String(item.id);
      var hit = state.options.filter(function (one) { return one.id === id; })[0];
      if (!hit) { state.options.push({ id: id }); hit = state.options[state.options.length - 1]; }
      if (item.name) hit.name = String(item.name);
      if (item.label) hit.label = String(item.label);
      if (item.is_owner !== undefined) hit.is_owner = !!item.is_owner;
      if (item.out_of_scope !== undefined) hit.out_of_scope = !!item.out_of_scope;
    });
    state.options.sort(function (a, b) { return a.id < b.id ? -1 : 1; });
    applyWhoDefault();
    /* 选中的人存回当前 scope 的桶：切实例再切回来还是他。 */
    bucket(state.scopeId).who = state.who;
    bucket(state.scopeId).options = state.options;
    /* 候选可能在任意一次 view.refresh() 里才冒出来（/status 只给 love_peers∪contacts，
       完整集要等 /notebook），所以每次合并后都要重绘下拉，否则用户一直看到"没有可选项"。 */
    renderWhoPicker();
  }

  /* 默认选中主人（真机反馈 2026-10-07）：面板按人组织，「没选」不是个有用的状态——
     有主人可选时就默认落在主人身上（已手动选过人、或存的选择仍然有效，则不动）。
     只在候选合并时跑一次，不进 renderWhoPicker：否则 setWho("") 会被立刻
     重新默认成主人，用户永远选不回「未选」。
     存储的 id 已经不在候选里（名单改过 / 数据清了）也一并落到默认，防止
     <select> 又把第一个选项假充选中。 */
  function applyWhoDefault() {
    if (!state.options.length) return;
    if (state.who && currentOption()) return;
    var owner = state.options.filter(function (one) { return one.is_owner; })[0];
    var next = owner ? owner.id : "";
    if (next !== state.who) {
      state.who = next;
      storeWho(state.who);
    }
  }

  /* 显示文本：优先后端给的 label（名字(数字)），老后端没有就退回 name || id。
     is_owner 再缀一个「 · 主人」，一眼看出谁是主人。 */
  function optionText(item) {
    return (item.label || item.name || item.id) + (item.is_owner ? " · 主人" : "");
  }

  /* title 里带完整 id，鼠标悬停能确认到底是哪个号 */
  function optionTitle(item) {
    return (item.name && item.name !== item.id ? item.name + "（" + item.id + "）" : item.id) +
      (item.out_of_scope ? " · 当前档位外" : "");
  }

  function currentOption() {
    return state.options.filter(function (one) { return one.id === state.who; })[0] || null;
  }

  function renderWhoPicker() {
    var picker = document.getElementById("who-picker");
    var hint = document.getElementById("who-hint");
    if (!picker) return;
    UI.clear(picker);
    /* 主人置顶；组内仍按 id 排（不按名字排，否则每次刷新顺序都跳）。 */
    var list = state.options.slice().sort(function (a, b) {
      var ao = a.is_owner ? 0 : 1, bo = b.is_owner ? 0 : 1;
      if (ao !== bo) return ao - bo;
      return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
    });
    function addOpt(item) {
      picker.appendChild(UI.h("option", {
        value: item.id, text: optionText(item), title: optionTitle(item),
        selected: item.id === state.who,
      }));
    }
    /* 没选人时必须放一个占位项：<select> 在没有任何 option 标 selected 时
       会悄悄把第一个选项显示成"已选中"——主人置顶，看起来就像选好了主人，
       实际 state.who 还是空串，总览的「对谁」卡片如实显示「未选」，
       用户以为选过了，怎么等都不更新（真机反馈 2026-10-07）。 */
    if (!state.who && state.options.length) {
      picker.appendChild(UI.h("option", { value: "", text: "（未选）", selected: true }));
    }
    list.filter(function (x) { return !x.out_of_scope; }).forEach(addOpt);
    var out = list.filter(function (x) { return x.out_of_scope; });
    if (out.length) {
      var group = UI.h("optgroup", { label: OUT_SCOPE_LABEL, title: OUT_SCOPE_LABEL });
      out.forEach(function (item) {
        group.appendChild(UI.h("option", {
          value: item.id, text: optionText(item), title: optionTitle(item),
          selected: item.id === state.who,
        }));
      });
      picker.appendChild(group);
    }
    if (!state.options.length) {
      picker.appendChild(UI.h("option", { value: "", text: "（没有可选项）" }));
    }
    if (picker.setAttribute) picker.setAttribute("title", "按人组织，不按会话 · 当前启用范围：" + UI.scopeLabel(state.scope));
    if (hint) hint.textContent = whoHintText();
  }

  /* 侧栏底部那行（第 9 步）：**删掉「当前启用范围：X」**——用户说不需要，
     顶栏那个三段开关本身就写着档位，不必再说一遍。
     但「TA 在当前档位外」这种提示**保留**：它有信息量。
     结果是空串就把节点收起来，别留一行空白。 */
  function whoHintText() {
    var parts = [];
    var cur = currentOption();
    if (state.who) parts.push("当前对：" + optionText(cur || { id: state.who }));
    if (cur && cur.out_of_scope) {
      parts.push("TA 在当前档位外：她不会在 TA 的会话里工作，但面板照常能看能改");
    }
    return parts.join(" · ");
  }

  /* payload 里的 scope_mode 是后端说的真话；老后端没这个字段就别覆盖控件。 */
  function applyScopeMode(mode) {
    var v = String(mode === undefined || mode === null ? "" : mode);
    if (!UI.scopeValue || !v) return;
    state.scope = UI.scopeValue(v);
    if (scopeCtl) scopeCtl.set(state.scope);
  }

  /* ---- 启用范围（第 7 步 · 主入口）----
     顶栏一个三段控件，点一下就 POST settings **即时生效**，不必去设置页点保存。
     设置页里 editor === "scope" 的那个字段是同一份 UI.scopeControl，两个入口共用一个控件。
     ⚠️ 面板本身**永远可用**，不受这一档影响：切到「只主人」后在群里照样能看能改。
     失败必须**回滚显示**——绝不停在"看起来切了其实没切"的状态。 */
  var scopeCtl = null;

  function renderScopeBar() {
    var bar = document.getElementById("scope-bar");
    if (!bar) return;
    scopeCtl = UI.scopeControl(state.scope, function (next, prev) { switchScope(next, prev); });
    bar.appendChild(scopeCtl.el);
  }

  async function switchScope(next, prev) {
    /* 先乐观切过去（控件内部已经切了），失败再回滚到 prev。 */
    var scopeId = state.scopeId, epoch = scopeEpoch;
    try {
      var changes = {};
      changes[SCOPE_PATH] = next;
      var result = await apiPostScoped(scopeId, "settings", { changes: changes });
      if (epoch !== scopeEpoch) return;
      if (result && result.ok === false) throw new Error(String(result.error || "未知原因"));
      state.scope = next;
      UI.toast("启用范围：" + UI.scopeLabel(next));
      await reloadAfterScope(scopeId, epoch);
    } catch (error) {
      if (epoch !== scopeEpoch) return;
      state.scope = prev;
      if (scopeCtl) scopeCtl.set(prev);      /* 回滚显示，别停在假切换态 */
      UI.toast("启用范围没切：" + String((error && error.message) || error), true);
    }
  }

  /* 切档之后「对谁」要跟着动（第 8 步）：重拉 /status 拿新的 who_options +
     scope_mode（分区、主人置顶随之重建），再把当前 tab 整个刷一遍。
     拉不到也不该把面板弄坏——退化成只重画当前 tab。 */
  async function reloadAfterScope(scopeId, epoch) {
    try {
      var st = await apiGetScoped(scopeId, "status", { who: state.who });
      if (epoch !== scopeEpoch) return;
      applyScopeMode(st && st.scope_mode);
      mergeOptions(st && st.who_options);
    } catch (error) { /* 老后端可能还没 scope_mode，继续刷 tab */ }
    if (epoch !== scopeEpoch) return;
    try {
      await show(state.current);
    } catch (error) { /* 视图自己会兜底 */ }
    if (epoch !== scopeEpoch) return;
    var cur = currentOption();
    if (cur && cur.out_of_scope) {
      UI.toast("「" + optionText(cur).replace(" · 主人", "") + "」在当前档位外：她不会在 TA 的会话里工作，面板照常能看", true);
    }
  }

  /* ---- 「起个名」（第 9 步）----
     昵称是"她下次跟你说话时"才落盘的，而默认档是「只私聊」、群聊里根本不记 contacts
     ——所以主人那位往往只有一个数字。这里给个内联入口直接写称呼偏好。
     ⚠️ 受限 iframe 的 sandbox 没有 allow-modals：**绝不能用 window.prompt / confirm**。
     ⚠️ 写回必须**合并**现有 name_preference，不能把别人的名字冲掉。 */
  var NAME_PATH = "name_preference";
  var namePref = {};          /* GET settings 读回来的现有称呼偏好 */

  function loadNamePref(values) {
    var v = values && values[NAME_PATH];
    namePref = (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
  }

  function nameEditorOpen() {
    var box = document.getElementById("who-name-edit");
    var input = document.getElementById("who-name-input");
    if (!box || !input) return;
    box.hidden = false;
    input.value = String(namePref[state.who] || "");
    input.setAttribute("placeholder", state.who ? "给 " + state.who + " 起个名字（留空＝删掉）" : "先在上面选一个人");
  }

  function closeNameEditor() {
    var box = document.getElementById("who-name-edit");
    if (box) box.hidden = true;
  }

  async function saveName() {
    var input = document.getElementById("who-name-input");
    var who = state.who;
    var scopeId = state.scopeId, epoch = scopeEpoch;
    if (!input || !who) { UI.toast("先在上面选一个人", true); return; }
    var text = String(input.value || "").trim();
    /* 合并：别人的名字一条都不能少。留空 = 删掉这个人这一条。 */
    var merged = Object.assign({}, namePref);
    if (text) merged[who] = text; else delete merged[who];
    var changes = {};
    changes[NAME_PATH] = merged;
    try {
      var result = await apiPostScoped(scopeId, "settings", { changes: changes });
      if (epoch !== scopeEpoch) return;
      if (result && result.ok === false) throw new Error(String(result.error || "未知原因"));
      namePref = merged;
      if (state.who === who) closeNameEditor();
      /* 立刻把新名字显示出来：下拉文本是 label(后端给的) || name || id，
         这里手动把 name 补上，**不等**后端把 contacts 写完。 */
      var hit = state.options.filter(function (one) { return one.id === who; })[0];
      if (hit) {
        hit.name = text;
        if (text) hit.label = text + "(" + who + ")"; else delete hit.label;
        renderWhoPicker();
      }
      UI.toast(text ? "名字已记为「" + text + "」" : "已删掉这个名字");
    } catch (error) {
      if (epoch !== scopeEpoch) return;
      /* 失败就不改显示**，让下拉维持原样。 */
      UI.toast("没改成：" + String((error && error.message) || error), true);
    }
  }

  /* ---- 左上角标题（第 10 步 · 用户自定义）----
     收的是**panel 这一组自己**（`{brand, brand_sub}`），不是整份 values——
     两个调用点（启动那次、设置页保存后）手里都是这一组，别再各自往里挖一层。
     值来自配置 `panel.brand` / `panel.brand_sub`，由后端过完默认值再给，
     所以这里不自己造默认值：**只认"给了什么就显示什么"，空串＝那一行不显示**。
     ⚠️ 藏那两行必须靠 CSS 里的 `[hidden]` 兜底——`.brand` 是 display:flex，
     UA 的 `[hidden]{display:none}` 特异性不够，光设 hidden 属性是藏不住的
     （第 10 步真浏览器检查抓到的就是这个）。
     index.html 里那份字面量是"还没拿到配置时"的兜底（连不上后端也不能空着）。 */
  var FALLBACK_BRAND = "情绪日记";

  function applyPanel(panel) {
    var box = (panel && typeof panel === "object") ? panel : {};
    var brand = String(box.brand === undefined ? FALLBACK_BRAND : box.brand).trim();
    var sub = String(box.brand_sub === undefined ? "" : box.brand_sub).trim();
    var brandEl = document.getElementById("brand");
    var subEl = document.getElementById("brand-sub");
    if (brandEl) { brandEl.textContent = brand; brandEl.hidden = !brand; }
    if (subEl) { subEl.textContent = sub; subEl.hidden = !sub; }
    /* 标签页标题跟着走：两行都空也留个能认出来的名字，不然书签全一样 */
    document.title = (brand || FALLBACK_BRAND) + (sub ? " · " + sub : "");
  }

  /* ---- 视图 ctx：按 scope 绑定 ----
     **一个 scope 一个 ctx**（缓存复用）：
       - 同一 scope 切 tab 回来还是同一个 ctx → 视图里的草稿缓存不丢；
       - 不同 scope 是不同 ctx → 草稿、滚动、候选天然隔开。
     ctx 里的 api 永远只说这一个 scope 的事（scope_id 在创建时就冻住）。 */
  function ctxFor(scopeId) {
    var key = String(scopeId || "");
    if (!ctxCache[key]) {
      ctxCache[key] = makeCtx(key);
    }
    return ctxCache[key];
  }

  function makeCtx(scopeId) {
    var ctx = {
      UI: UI,
      state: state,
      /* 冻结的 scope：这次会话只说这个实例的事 */
      scopeId: function () { return scopeId; },
      /* 跨 scope 的草稿键前缀：视图按人存状态时加上它，两个实例的同名用户不串。 */
      scopeKey: function () { return scopeId ? scopeId + "|" : ""; },
      /* 只有 legacy 实例支持桌宠设备 v1 */
      isLegacyScope: function () {
        if (!state.isolationEnabled) return true;
        if (!scopeId) return false;
        if (state.legacyScopeId && scopeId === state.legacyScopeId) return true;
        var item = scopeItem(scopeId);
        return !!(item && item.legacy);
      },
      apiGet: function (key, params) { return apiGetScoped(scopeId, key, params); },
      apiPost: function (key, body) { return apiPostScoped(scopeId, key, body); },
      apiUpload: function (key, file) { return apiUploadScoped(scopeId, key, file); },
      who: function () { return scopeId === state.scopeId ? state.who : bucket(scopeId).who; },
      setWho: function (value) {
        if (scopeId !== state.scopeId) return;
        if (state.view && state.view.hasUnsavedChanges && state.view.hasUnsavedChanges()) {
          UI.toast("请先保存或放弃当前表单，再切换用户。", true);
          renderWhoPicker();
          return;
        }
        state.who = String(value || "");
        storeWho(state.who);
        bucket(state.scopeId).who = state.who;
        renderWhoPicker();
        show(state.current);
      },
      mergeOptions: function (options) { if (scopeId === state.scopeId) mergeOptions(options); },
      show: show,
      openDiaryDate: function (date) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(String(date || ""))) return;
        state.diaryDate = date;
        return show("diary");
      },
      toast: UI.toast,
      /* 设置页保存后把新档位推给顶栏，两个入口永远显示同一个值 */
      setScope: function (value) {
        applyScopeMode(value);
        renderWhoPicker();          /* 「当前启用范围：X」那句跟着走 */
      },
      /* 设置页保存后把新的面板标题推过来（保存的是同一个 panel 组） */
      setPanel: function (panel) { applyPanel(panel); },
    };
    return ctx;
  }

  /* ---- 视图切换 ---- */
  function factoryFor(key) {
    var makers = {
      overview: global.createOverviewView,
      life: global.createLifeView,
      imagery: global.createImageryView,
      diary: global.createDiaryView,
      notebook: global.createNotebookView,
      affinity: global.createAffinityView,
      proactive: global.createProactiveView,
      social: global.createSocialView,
      core: global.createCoreView,
      devices: global.createChatDevicesView,
      data: global.createDataView,
      settings: global.createSettingsView,
    };
    return makers[key] || null;
  }

  async function show(key) {
    if (state.view && state.view.hasUnsavedChanges && state.view.hasUnsavedChanges()) {
      UI.toast("请先保存或放弃当前表单，再离开或刷新。", true);
      return;
    }
    var make = factoryFor(key);
    var body = document.getElementById("stage-body");
    var title = document.getElementById("stage-title");
    var note = document.getElementById("stage-note");
    if (!make || !body) return;
    var generation = ++viewGeneration;
    if (mountedKey) scrollPositions[mountedKey] = { top: body.scrollTop, left: body.scrollLeft };
    /* 滚动位置按 scope + 人 + 页三者分开：换实例回到各自读到的位置。 */
    mountedKey = JSON.stringify([state.scopeId, state.who, key]);
    var position = scrollPositions[mountedKey] || { top: 0, left: 0 };
    /* 从曲线明确跳到某天时，从那天的页首看起。 */
    if (key === "diary" && state.diaryDate) position = { top: 0, left: 0 };
    state.current = key;
    /* 这次挂载**冻结**的 scope：视图里发出的每个请求都归它。 */
    var scopeAtMount = state.scopeId;
    var app = document.getElementById("app");
    if (app) app.setAttribute("data-view", key);
    /* 立绘只在总览出现（它是 index.html 里的静态节点，见那里的注释）。
       stage-body 每次都被 UI.clear 清空，所以它必须待在 body 外面。 */
    var portraits = document.getElementById("portraits");
    if (portraits) portraits.hidden = key !== "overview";
    var tab = TABS.filter(function (item) { return item.key === key; })[0] || {};
    if (title) title.textContent = tab.title || key;
    if (note) note.textContent = tab.note || "";
    document.querySelectorAll("#tabs .tab").forEach(function (el) {
      var on = el.dataset.key === key;
      el.className = el.className.replace(" active", "") + (on ? " active" : "");
      el.setAttribute("aria-selected", on ? "true" : "false");   /* 无障碍：选中态跟着切换走 */
    });
    if (state.view && typeof state.view.unmount === "function") {
      try { state.view.unmount(); } catch (error) { /* 忽略卸载异常 */ }
    }
    UI.clear(body);
    body.scrollTop = 0;
    body.scrollLeft = 0;
    /* 视图拿到的 ctx 绑定**当前 scope**：这次挂载期间的每个请求都归这个实例。 */
    var view = make(ctxFor(state.scopeId));
    state.view = view;
    view.mount(body);
    try {
      await view.refresh();
      /* 旧页的请求结束后，既不能改新页，也不能抢走用户刚滚到的位置。
         ⚠️ 换了实例也一样：迟到的旧 scope 响应**不许**画到新 scope 的视图上。 */
      if (generation === viewGeneration && state.view === view && scopeAtMount === state.scopeId &&
          body.scrollTop === 0 && body.scrollLeft === 0) {
        body.scrollTop = position.top;
        body.scrollLeft = position.left;
      }
    } catch (error) {
      if (generation !== viewGeneration || state.view !== view) return;
      UI.clear(body);
      body.appendChild(UI.errorBox(String((error && error.message) || error)));
    }
  }

  function renderTabs() {
    var holder = document.getElementById("tabs");
    if (!holder) return;
    UI.clear(holder);
    TABS.forEach(function (item) {
      holder.appendChild(UI.h("button", {
        class: "tab",
        "data-key": item.key,
        type: "button",
        role: "tab",
        id: "tab-" + item.key,
        "aria-controls": "stage-body",
        "aria-selected": item.key === state.current ? "true" : "false",
        title: item.note || "",
        text: item.title,
        onclick: function () { show(item.key); },
      }));
    });
  }

  /* 立绘挂了就地变成一个虚线占位，不能在总览开天窗。
   绑在 document 的**捕获**阶段：img 的 error 不冒泡，且图片可能在我们绑定
   之前就已经加载失败，捕获阶段两种情况都接得住。
   类加在 img 自己身上——它的父节点是整个 frame，加在父节点上会把所有图一起藏掉。 */
document.addEventListener("error", function (event) {
  var target = event.target;
  if (target && target.tagName === "IMG" && target.closest && target.closest("#portraits")) {
    target.classList.add("is-missing");
  }
}, true);

/* ---- 启动 ---- */
  function showGuard() {
    document.getElementById("guard").hidden = false;
    document.getElementById("app").hidden = true;
  }

  async function start() {
    var b = bridge();
    if (!b || typeof b.apiGet !== "function") { showGuard(); return; }
    if (typeof b.ready === "function") {
      try { await b.ready(); } catch (error) { showGuard(); return; }
    }
    document.getElementById("guard").hidden = true;
    document.getElementById("app").hidden = false;

    /* 先拿实例清单：它决定这次面板**对谁说话**（哪个 scope）。
       拿不到＝后端没这个接口＝没开隔离＝老行为照旧。 */
    var hasInstances = await loadInstances();
    if (!hasInstances) {
      var failedBody = document.getElementById("stage-body");
      if (failedBody) { UI.clear(failedBody); failedBody.appendChild(UI.errorBox("读取实例列表失败：" + state.instanceError)); }
      return;
    }
    if (hasInstances && state.scopeId) activateScope(state.scopeId);

    state.ctx = ctxFor(state.scopeId);

    renderTabs();
    renderWhoPicker();
    renderScopeBar();
    renderScopePicker();

    document.getElementById("refresh").addEventListener("click", function () { show(state.current); });
    document.getElementById("who-picker").addEventListener("change", function (event) {
      ctxFor(state.scopeId).setWho(event.target.value);
    });
    /* 实例下拉：切实例＝清掉旧人的候选/统计/立绘/视图，重新加载。 */
    var scopePicker = document.getElementById("scope-picker");
    if (scopePicker) {
      scopePicker.addEventListener("change", function (event) {
        switchInstance(String(event.target.value || ""));
      });
    }
    document.getElementById("who-rename").addEventListener("click", function () {
      var box = document.getElementById("who-name-edit");
      if (box && !box.hidden) { closeNameEditor(); return; }   /* 再点一下收起 */
      nameEditorOpen();
    });
    document.getElementById("who-name-ok").addEventListener("click", saveName);
    document.getElementById("who-name-cancel").addEventListener("click", closeNameEditor);

    state.who = readWhoFromUrl() || storedWho();
    renderWhoPicker();

    /* 外观：URL ?theme= 覆盖 > 本地存的偏好 > 默认（少女心）。
       不跟随宿主主题——用户明确要默认粉萌少女心，宿主明暗不该替这个决定做主。 */
    setTheme(themeFromUrl() || storedTheme() || DEFAULT_PRESET);
    renderThemePicker();
    document.getElementById("theme-picker").addEventListener("change", function (event) {
      var value = String(event.target.value || DEFAULT_PRESET);
      setTheme(value);
      storeTheme(value);
      renderThemePicker();
    });

    /* 顶栏三段的初始档位：从 settings 的 values 里点分路径读。
       读不到就留在默认档（private）并且**不报错**——面板永远可用，
       这只是显示当前值，不该因为它把整个面板拖下水。 */
    var initialEpoch = scopeEpoch;
    try {
      var st0 = await apiGetScoped(state.scopeId, "settings", {});
      if (initialEpoch !== scopeEpoch) return;
      loadNamePref(st0 && st0.values);
      applyPanel(st0 && st0.values && st0.values.panel);
      var v = (((st0 || {}).values || {}).scope || {}).mode;
      if (v) { applyScopeMode(v); renderWhoPicker(); }
    } catch (error) { /* 读不到就按默认档显示，不打扰用户 */ }
    if (initialEpoch !== scopeEpoch) return;

    await show("overview");
  }

  /* 外观预设：**唯一来源**。样式表里每个 id 对应一组 token（见 style.css 顶部）。
     顺序 = 下拉里的顺序；第一项是"默认"，也是没存过偏好时的落点。 */
  var PRESETS = [
    { id: "sakura", name: "少女心（粉萌）" },
    { id: "moon", name: "月夜（深色）" },
    { id: "mist", name: "晨雾（清爽）" },
    { id: "mint", name: "薄荷" },
    { id: "cream", name: "奶油（暖）" },
  ];
  var DEFAULT_PRESET = PRESETS[0].id;
  var THEME_KEY = "denia.diary.theme";

  function presetIds() {
    return PRESETS.map(function (item) { return item.id; });
  }

  function setTheme(id) {
    var name = presetIds().indexOf(id) >= 0 ? id : DEFAULT_PRESET;
    /* 同值不重写：bridge.onContext 可能反复回调，重写属性会白白弄脏 DOM。 */
    var root = document.documentElement;
    if (root.getAttribute("data-theme") !== name) root.setAttribute("data-theme", name);
  }

  function storedTheme() {
    try { return global.localStorage.getItem(THEME_KEY) || ""; } catch (error) { return ""; }
  }

  function storeTheme(id) {
    try { global.localStorage.setItem(THEME_KEY, id || ""); } catch (error) { /* 无痕模式，忽略 */ }
  }

  function themeFromUrl() {
    try {
      var search = String(global.location && global.location.search || "");
      var match = search.match(/[?&]theme=([^&]*)/);
      return match ? decodeURIComponent(match[1]) : "";
    } catch (error) { return ""; }
  }

  function renderThemePicker() {
    var picker = document.getElementById("theme-picker");
    if (!picker) return;
    var current = document.documentElement.getAttribute("data-theme") || DEFAULT_PRESET;
    UI.clear(picker);
    PRESETS.forEach(function (item) {
      picker.appendChild(UI.h("option", {
        value: item.id, text: item.name, selected: item.id === current,
      }));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { start(); });
  } else {
    start();
  }

  global.DiaryPanel = {
    ENDPOINTS: ENDPOINTS, TABS: TABS, PRESETS: PRESETS,
    DEFAULT_PRESET: DEFAULT_PRESET, show: show, setTheme: setTheme,
    applyPanel: applyPanel,
    /* 三端整合 P0：测试与后续接线用到的入口 */
    state: state, ctxFor: ctxFor, switchScope: switchInstance,
    loadInstances: loadInstances, renderScopePicker: renderScopePicker,
    scopeItem: scopeItem,
  };
})(window);
