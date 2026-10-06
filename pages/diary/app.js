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
  /* 写回用的键。**全文件只此一处**出现这个路径——控件选哪个档、怎么显示，
     都只看 UI.SCOPE_MODES 和后端给的 editor，不按路径名判断。 */
  var SCOPE_PATH = "scope.mode";
  var state = { who: "", options: [], current: "overview", view: null, ctx: null, scope: "private" };

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
    /* 候选可能在任意一次 view.refresh() 里才冒出来（/status 只给 love_peers∪contacts，
       完整集要等 /notebook），所以每次合并后都要重绘下拉，否则用户一直看到"没有可选项"。 */
    renderWhoPicker();
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
    try {
      var changes = {};
      changes[SCOPE_PATH] = next;
      var result = await apiPost("settings", { changes: changes });
      if (result && result.ok === false) throw new Error(String(result.error || "未知原因"));
      state.scope = next;
      UI.toast("启用范围：" + UI.scopeLabel(next));
      await reloadAfterScope();
    } catch (error) {
      state.scope = prev;
      if (scopeCtl) scopeCtl.set(prev);      /* 回滚显示，别停在假切换态 */
      UI.toast("启用范围没切：" + String((error && error.message) || error), true);
    }
  }

  /* 切档之后「对谁」要跟着动（第 8 步）：重拉 /status 拿新的 who_options +
     scope_mode（分区、主人置顶随之重建），再把当前 tab 整个刷一遍。
     拉不到也不该把面板弄坏——退化成只重画当前 tab。 */
  async function reloadAfterScope() {
    try {
      var st = await apiGet("status", { who: state.who });
      applyScopeMode(st && st.scope_mode);
      mergeOptions(st && st.who_options);
    } catch (error) { /* 老后端可能还没 scope_mode，继续刷 tab */ }
    try {
      await show(state.current);
    } catch (error) { /* 视图自己会兜底 */ }
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
    if (!input || !who) { UI.toast("先在上面选一个人", true); return; }
    var text = String(input.value || "").trim();
    /* 合并：别人的名字一条都不能少。留空 = 删掉这个人这一条。 */
    var merged = Object.assign({}, namePref);
    if (text) merged[who] = text; else delete merged[who];
    var changes = {};
    changes[NAME_PATH] = merged;
    try {
      var result = await apiPost("settings", { changes: changes });
      if (result && result.ok === false) throw new Error(String(result.error || "未知原因"));
      namePref = merged;
      closeNameEditor();
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
      /* 失败就不改显示**，让下拉维持原样。 */
      UI.toast("没改成：" + String((error && error.message) || error), true);
    }
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
      var on = el.dataset.key === key;
      el.className = el.className.replace(" active", "") + (on ? " active" : "");
      el.setAttribute("aria-selected", on ? "true" : "false");   /* 无障碍：选中态跟着切换走 */
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
      /* 设置页保存后把新档位推给顶栏，两个入口永远显示同一个值 */
      setScope: function (value) {
        applyScopeMode(value);
        renderWhoPicker();          /* 「当前启用范围：X」那句跟着走 */
      },
    };

    renderTabs();
    renderWhoPicker();
    renderScopeBar();

    document.getElementById("refresh").addEventListener("click", function () { show(state.current); });
    document.getElementById("who-picker").addEventListener("change", function (event) {
      state.ctx.setWho(event.target.value);
    });
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
       不跟随宿主主题——用户明确要默认粉萌少女心，宿主明暗不该替达妮娅做这个决定。 */
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
    try {
      var st0 = await apiGet("settings", {});
      loadNamePref(st0 && st0.values);
      var v = (((st0 || {}).values || {}).scope || {}).mode;
      if (v) { applyScopeMode(v); renderWhoPicker(); }
    } catch (error) { /* 读不到就按默认档显示，不打扰用户 */ }

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
