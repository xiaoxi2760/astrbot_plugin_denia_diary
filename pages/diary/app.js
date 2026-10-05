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
    diaryList: "diary/list",
    diaryContent: "diary/content",
    notebook: "notebook",
    notebookComplete: "notebook/complete",
    notebookDelete: "notebook/delete",
    affinity: "affinity",
    history: "history",
    proactive: "proactive",
    settings: "settings",
    settingsReset: "settings/reset",
    portrait: "portrait",
    portraitUpload: "portrait/upload",
    portraitSelect: "portrait/select",
    portraitDelete: "portrait/delete",
  };

  var TABS = [
    { key: "overview", title: "总览", note: "达妮娅此刻" },
    { key: "diary", title: "日记", note: "只读" },
    { key: "notebook", title: "小本本", note: "事实与约定" },
    { key: "affinity", title: "熟悉度", note: "榜与档位" },
    { key: "proactive", title: "曲线与主动", note: "情绪曲线 + 主动消息" },
    { key: "data", title: "数据", note: "坐标 · 数据文件" },
    { key: "settings", title: "设置", note: "全部配置项，改完即生效" },
  ];

  var WHO_KEY = "denia.diary.who";
  var state = { who: "", options: [], current: "overview", view: null, ctx: null };

  function bridge() { return global.AstrBotPluginPage || null; }

  /* ---- api 封装：统一把后端错误变成可读信息，绝不让 view 白屏 ---- */
  async function apiGet(key, params) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    try {
      return await b.apiGet(ENDPOINTS[key], params || {});
    } catch (error) {
      throw new Error(String((error && error.message) || error || "请求失败"));
    }
  }

  async function apiPost(key, body) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    try {
      return await b.apiPost(ENDPOINTS[key], body || {});
    } catch (error) {
      throw new Error(String((error && error.message) || error || "请求失败"));
    }
  }

  /* 立绘上传。官方 bridge 的 upload() 只发一个文件、字段名固定 file，
     所以这里只能传单个 File；多图靠多次调用。宿主没给 upload 就明说，
     不要静默失败让用户以为传上去了。 */
  async function apiUpload(key, file) {
    var b = bridge();
    if (!b) throw new Error("没有 bridge");
    if (typeof b.upload !== "function") throw new Error("这个运行环境不支持上传");
    try {
      return await b.upload(ENDPOINTS[key], file);
    } catch (error) {
      throw new Error(String((error && error.message) || error || "上传失败"));
    }
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
    try { return global.localStorage.getItem(WHO_KEY) || ""; } catch (error) { return ""; }
  }

  function storeWho(value) {
    try { global.localStorage.setItem(WHO_KEY, value || ""); } catch (error) { /* 无痕模式，忽略 */ }
  }

  function mergeOptions(options) {
    (options || []).forEach(function (item) {
      if (!item || !item.id) return;
      if (!state.options.some(function (one) { return one.id === item.id; })) {
        state.options.push({ id: String(item.id), name: String(item.name || "") });
      }
    });
    state.options.sort(function (a, b) { return a.id < b.id ? -1 : 1; });
    /* 候选可能在任意一次 view.refresh() 里才冒出来（/status 只给 love_peers∪contacts，
       完整集要等 /notebook），所以每次合并后都要重绘下拉，否则用户一直看到"没有可选项"。 */
    renderWhoPicker();
  }

  function renderWhoPicker() {
    var picker = document.getElementById("who-picker");
    var hint = document.getElementById("who-hint");
    if (!picker) return;
    UI.clear(picker);
    state.options.forEach(function (item) {
      picker.appendChild(UI.h("option", {
        value: item.id,
        text: item.name || item.id,
        selected: item.id === state.who,
      }));
    });
    if (!state.options.length) {
      picker.appendChild(UI.h("option", { value: "", text: "（没有可选项）" }));
    }
    if (hint) hint.textContent = state.who ? "当前：" + state.who : "按人组织，不按会话";
  }

  /* ---- 视图切换 ---- */
  function factoryFor(key) {
    var makers = {
      overview: global.createOverviewView,
      diary: global.createDiaryView,
      notebook: global.createNotebookView,
      affinity: global.createAffinityView,
      proactive: global.createProactiveView,
      data: global.createDataView,
      settings: global.createSettingsView,
    };
    return makers[key] || null;
  }

  async function show(key) {
    var make = factoryFor(key);
    var body = document.getElementById("stage-body");
    var title = document.getElementById("stage-title");
    var note = document.getElementById("stage-note");
    if (!make || !body) return;
    state.current = key;
    /* 立绘只在总览出现（它是 index.html 里的静态节点，见那里的注释）。
       stage-body 每次都被 UI.clear 清空，所以它必须待在 body 外面。 */
    var portraits = document.getElementById("portraits");
    if (portraits) portraits.hidden = key !== "overview";
    var tab = TABS.filter(function (item) { return item.key === key; })[0] || {};
    if (title) title.textContent = tab.title || key;
    if (note) note.textContent = tab.note || "";
    document.querySelectorAll("#tabs .tab").forEach(function (el) {
      el.className = el.className.replace(" active", "") + (el.dataset.key === key ? " active" : "");
    });
    UI.clear(body);
    if (state.view && typeof state.view.unmount === "function") {
      try { state.view.unmount(); } catch (error) { /* 忽略卸载异常 */ }
    }
    state.view = make(state.ctx);
    state.view.mount(body);
    try {
      await state.view.refresh();
    } catch (error) {
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

    state.ctx = {
      UI: UI,
      apiGet: apiGet,
      apiPost: apiPost,
      apiUpload: apiUpload,
      state: state,
      who: function () { return state.who; },
      setWho: function (value) {
        state.who = String(value || "");
        storeWho(state.who);
        renderWhoPicker();
        show(state.current);
      },
      mergeOptions: mergeOptions,
      show: show,
      toast: UI.toast,
    };

    renderTabs();
    renderWhoPicker();

    document.getElementById("refresh").addEventListener("click", function () { show(state.current); });
    document.getElementById("who-picker").addEventListener("change", function (event) {
      state.ctx.setWho(event.target.value);
    });

    state.who = readWhoFromUrl() || storedWho();
    renderWhoPicker();

    /* 外观：URL ?theme= 覆盖 > 本地存的偏好 > 默认（少女心）。
       不跟随宿主主题——用户明确要默认粉萌少女心，宿主明暗不该替达妮娅做这个决定。 */
    setTheme(themeFromUrl() || storedTheme() || DEFAULT_PRESET);
    renderThemePicker();
    document.getElementById("theme-picker").addEventListener("change", function (event) {
      var value = String(event.target.value || DEFAULT_PRESET);
      setTheme(value);
      storeTheme(value);
      renderThemePicker();
    });

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
  };
})(window);
