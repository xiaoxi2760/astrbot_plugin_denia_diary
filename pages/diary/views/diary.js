/* 日记（第 6.2 步）：日历 + 单页日记本；第 15 步加了改 / 删 / 回收站。
   恋爱日记默认收起（决策 #18，保留）：列表只显示"恋爱日记 · N 篇 · 最近更新"。

   这屏换掉了两处老设计：
     - 手打 YYYY-MM-DD 的输入框 → 月视图日历（有日记的日子一眼看得出）
     - 一坨 <pre> → "一页纸"日记本（按空行分段 + 分页）

   **向后兼容**：后端没给 `entries`（老版本）时退回 `text`，没给 `seg_id` /
   `editable` / `edit_window` 时**一律不给改删按钮**——宁可没有按钮，也不要一个
   按下去必失败的按钮（老后端没这些接口）。

   **改删靠 seg_id 定位，不是靠正文里有哪几个字**：`seg_id` 是这一段的内容地址，
   文件被人手改过或上一条改删已经动过它，id 就对不上，后端会回"这段已经变了"。
   前端因此在每次写操作后**整屏重拉**，绝不拿本地旧内容接着改。 */
(function (global) {
  "use strict";

  var WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"];

  /* ---- 日期小工具（全部走本地时区，不用 toISOString——它按 UTC，会差一天） ---- */
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function ym(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1); }
  function todayYmd() { return ymd(new Date()); }
  /* "2026-10-05" → 本地 Date；解析不出来返回 null（不 new Date(乱串)，那是 NaN） */
  function parseYmd(text) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
    if (!m) return null;
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    if (d.getFullYear() !== Number(m[1]) || d.getMonth() !== Number(m[2]) - 1) return null;
    return d;
  }
  function shiftMonth(monthKey, delta) {
    var parts = String(monthKey).split("-");
    var d = new Date(Number(parts[0]), Number(parts[1]) - 1 + delta, 1);
    return ym(d);
  }
  function weekdayCn(ymdText) {
    var d = parseYmd(ymdText);
    return d ? "星期" + WEEK_CN[(d.getDay() + 6) % 7] : "";
  }
  function shortDate(ymdText) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(ymdText || ""));
    return m ? m[2] + "-" + m[3] : String(ymdText || "");
  }

  global.createDiaryView = function createDiaryView(ctx) {
    var holder = null;
    /* 两本日记各自一套状态：翻月 / 选中哪天 / 分页 / 折叠，都按 book 分开存 */
    var state = {
      book: "normal", tail: 20, collapsed: {},
      per: { normal: { month: "", date: "", page: 0 }, love: { month: "", date: "", page: 0 } },
      /* 改删的临时态：editing = 正在编辑的 seg_id，confirming = 待确认删除的 seg_id。
       都不是 id 就不生效，所以点别的条目不会互相干扰。 */
      editing: "", confirming: "", trashOpen: false, window: null,
      /* 第 16 步：view = "text" 看正文，"card" 看卡片预览。cardDate 是预览翻到哪天。 */
      view: "text", cardDate: "", cardStyle: "",
    };

    /* ---- 卡片预览（第 16 步；第 18 步补"按内容自适应高度 + PC 端缩小"）--------
       后端把模板**渲成 HTML** 再给我们（``core/diary/card.py`` 与 /看日记 出图
       共用同一份），所以这里拿到的就是真卡片——不是"大概长这样"的近似图。
       用 iframe + srcdoc 装：卡片自带 body 背景与整页尺寸，塞进当前页会串味。

       高度由 ``--card-h`` 驱动、缩放交给 ``.np-cardstage`` 的媒体查询：卡片模板里
       ``.sheet`` 是 ``min-height: calc(100vh - 92px)``，所以「iframe 多高、正文就
       多高」正好互相咬住，写一次就停，不会越滚越长。 */
    function fitCardHeight(stage, frame) {
      try {
        var doc = frame.contentDocument;
        if (!doc) return;
        var h = Math.max(
          (doc.documentElement && doc.documentElement.scrollHeight) || 0,
          (doc.body && doc.body.scrollHeight) || 0
        );
        if (h > 0) stage.style.setProperty("--card-h", h + "px");
      } catch (error) { /* 拿不到就沿用 CSS 里的兜底高度，不白屏 */ }
    }

    /* 窗口尺寸变了要重算一次：舞台宽度变了，卡片里换行的位置跟着变，高度也会变。
       去抖 150ms——拖窗口时不必每帧都去读 iframe 文档。 */
    function refitCard() {
      if (cardStage && cardFrame) fitCardHeight(cardStage, cardFrame);
    }

    function onWindowResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(refitCard, 150);
    }

    async function loadCard(book) {
      var UI = ctx.UI;
      if (cardSlot) cardSlot.remove();
      var box = UI.h("div", { class: "panel np-panel" }, [UI.h("div", { class: "muted", text: "渲染卡片…" })]);
      cardSlot = box;
      holder.appendChild(box);
      try {
        var data = await ctx.apiGet("diaryCard", {
          book: book, date: state.cardDate, style: state.cardStyle,
          who: ctx.who(),          /* 顶栏「对谁」：面板也按人筛（第 17 步） */
        });
        UI.clear(box);
        if (data.empty) {
          box.appendChild(UI.empty("这本还没有日记，先陪她聊两句吧 ✎"));
          return;
        }
        state.cardDate = data.date || "";
        state.cardStyle = data.style || "";

        /* 左卡片 + 右按钮栏，同一个 flex 行里并排 */
        var stage = UI.h("div", { class: "np-cardstage" });
        var scale = UI.h("div", { class: "np-cardscale" });
        var frame = UI.h("iframe", { class: "np-cardframe", title: "日记本预览" });
        frame.setAttribute("sandbox", "allow-same-origin");
        frame.addEventListener("load", function () { fitCardHeight(stage, frame); });
        frame.srcdoc = data.html || "";
        scale.appendChild(frame);
        stage.appendChild(scale);
        box.appendChild(UI.h("div", { class: "np-notebook" }, [stage, cardBar(UI, data)]));

        cardStage = stage;
        cardFrame = frame;
        if (!resizeBound) {
          resizeBound = true;
          window.addEventListener("resize", onWindowResize);
        }
      } catch (error) {
        UI.clear(box);
        box.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    /* ---- 日记本页：左卡片 + 右侧竖排按钮栏 --------------------------------------
   左边是卡片（收窄后更靠边），右边一整条竖排的按钮栏，按钮从上到下依次是：
   上一页 / 下一页（并排一行） → 导出为图片 → 「风格」标题 → 六款版式 → 保存配置。
   保存配置单独落在**最底下**，它是这一栏里唯一会写配置的控件，压轴放最不容易误按。 */
    function cardBar(UI, data) {
      var days = data.days || [];
      var index = Number(data.index);
      var total = days.length;
      var at = index >= 0 ? index : 0;
      var prevDay = at > 0 ? days[at - 1] : "";
      var nextDay = at < total - 1 ? days[at + 1] : "";

      var exportBtn = UI.h("button", {
        class: "btn np-export", type: "button", text: "导出为图片",
        title: "把这页日记本存成 PNG（和聊天里 /看日记 出的是同一张图）",
        onclick: function () { exportCard(exportBtn); },
      });

      return UI.h("aside", { class: "np-rail" }, [
        UI.h("div", { class: "np-rail-row" }, [
          UI.h("button", {
            class: "btn", type: "button", text: "‹ 上一页",
            disabled: at <= 0, title: prevDay ? "翻到 " + prevDay : "已经是最新的一页",
            onclick: function () { if (!prevDay) return; state.cardDate = prevDay; loadCard(state.book); },
          }),
          UI.h("button", {
            class: "btn", type: "button", text: "下一页 ›",
            disabled: at >= total - 1, title: nextDay ? "翻到 " + nextDay : "已经是最早的一页",
            onclick: function () { if (!nextDay) return; state.cardDate = nextDay; loadCard(state.book); },
          }),
        ]),
        exportBtn,
        UI.h("div", { class: "np-rail-label", text: "风格" }),
        cardStyleRail(UI, data),
        saveBtnFor(UI, data),
      ]);
    }

    /* 挑款式只改**预览**——想比个样子就挨个点，不用先想好再改配置。
       满意了按最下面的「保存配置」才落盘（``saveCardStyle``）。 */
    function cardStyleRail(UI, data) {
      var styles = data.styles || [];
      var labels = data.style_labels || {};
      var current = data.style || "";
      var rail = UI.h("div", { class: "np-rail-styles" });
      styles.forEach(function (style) {
        rail.appendChild(UI.h("button", {
          class: "btn" + (style === current ? " is-on" : ""),
          type: "button",
          text: labels[style] || style,
          title: "先预览这一款：" + style,
          onclick: function () { state.cardStyle = style; loadCard(state.book); },
        }));
      });
      return rail;
    }

    function saveBtnFor(UI, data) {
      var labels = data.style_labels || {};
      var btn = UI.h("button", {
        class: "btn np-save", type: "button", text: "保存配置",
        title: "把现在这一款设为默认（私聊里 /看日记 也用它）",
        onclick: function () { saveCardStyle(btn, data.style || "", labels); },
      });
      return UI.h("div", { class: "np-rail-save" }, [
        btn,
        UI.h("span", { class: "np-hint", text: "先挑款式看看，满意了再保存" }),
      ]);
    }

    /* 导出为图片：**后端出图**（``diary/card-image``），拿回 data URL 让浏览器下载。
       不在前端拿 iframe 自己画：浏览器没有"把 DOM 画成 PNG"的原生 API，要么引第三方
       库（插件是零依赖的），要么只能截图糊弄——那样导出来的和 QQ 上那张就不是同一张图了。 */
    async function exportCard(btn) {
      if (btn.disabled) return;
      var oldText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "导出中…";
      try {
        var data = await ctx.apiGet("diaryCardImage", {
          book: state.book, date: state.cardDate, style: state.cardStyle, who: ctx.who(),
        });
        if (data && data.empty) { ctx.toast("这一页没有内容，导不出图", true); return; }
        if (!data || !data.data_url) throw new Error("后端没有回图片数据");
        var link = document.createElement("a");
        link.href = data.data_url;
        link.download = String(data.filename || "日记本.png");
        document.body.appendChild(link);
        link.click();
        link.remove();
        ctx.toast("已导出 " + (data.filename || "图片"));
      } catch (error) {
        ctx.toast(String((error && error.message) || error), true);
      } finally {
        btn.disabled = false;
        btn.textContent = oldText;
      }
    }

    /* 保存配置：把当前预览的这一款落成默认款式。
       走 ``settings`` 的 POST（校验 → 备份 → 落盘 → 热生效），所以面板和私聊
       ``/看日记`` 看到的是同一份配置，不会"面板改了聊天里没变"。
       body 只提交这一项 —— ``{changes: {"diary.card_style": …}}``；把整份配置原样发一遍
       是 settings.js 那条明确禁止的旧做法（后端 applied 会变成字段总数，用户看不出自己改了几项）。 */
    async function saveCardStyle(btn, style, labels) {
      if (btn.disabled) return;
      var name = (labels && labels[style]) || style;
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        var result = await ctx.apiPost("settings", { changes: { "diary.card_style": style } });
        if (!result || result.ok === false) {
          ctx.toast("没保存：" + String((result && result.error) || "未知原因"), true);
          return;
        }
        ctx.toast("已把「" + name + "」设为默认款式（/看日记 也用它）");
      } catch (error) {
        ctx.toast(String((error && error.message) || error), true);
      } finally {
        btn.disabled = false;
        btn.textContent = "保存配置";
      }
    }

    function slot(book) {
      if (!state.per[book]) state.per[book] = { month: "", date: "", page: 0 };
      return state.per[book];
    }

    /* 这本现在是不是展开的。**约定：true = 展开**；没设过就用默认（恋爱日记默认收起，
       其余默认展开）。别再玩 `!== false`——和写值那一处对不上，"展开"会按不动。 */
    function isOpen(info) {
      var stored = state.collapsed[info.book];
      if (stored !== undefined) return !!stored;
      return !info.is_love;
    }

    /* 收起一本之后正文归谁：还有别的展开本就留给它，都收起就一个面板都不留
       （render() 会清空整个 holder，不补内容的话正文会凭空消失）。 */
    function firstOpen() {
      return (state.books || []).filter(isOpen)[0] || null;
    }

    /* 整屏重画 + **立刻补回正文**。
       render() 自己不管内容区（它把 contentSlot 置空），所以谁调 render() 谁就必须
       紧接着 loadContent——否则正文面板凭空消失。收敛到这一个入口，杜绝再漏。
       凡改了 st.date / st.month / 可见性 的地方都走它。
       回收站也一起补：它在正文区后面，render 清空 holder 时一起被清掉了。 */
    function renderAndLoad(book) {
      render();
      var target = book || state.book;
      /* book 传 null = "两本都收起来了，正文区不加载"（老行为：那时只 render）。
         传空串 / 不传 = 照 state.book 来。别把 null 一起兜进去 —— 那样全收起时
         又会凭空冒出一本来。 */
      if (book !== null) {
        if (state.view === "card") loadCard(target); else loadContent(target);
      }
      loadTrash();
    }

    /* ---- 改 / 删的写操作（第 15 步）--------------------------------------------
       一律：POST → toast 报成败 → 整屏重拉。
       **绝不**拿本地内容接着改：seg_id 是内容地址，改完 id 就变了，留在本页的
       seg_id 全是过期的，再点一次必然"这段已经变了"。重拉一次最省心。 */
    async function mutate(key, body, okMsg) {
      try {
        var result = await ctx.apiPost(key, body);
        if (result && result.ok) {
          ctx.toast(okMsg);
          state.editing = "";
          state.confirming = "";
          await renderAndLoad(state.book);
          return true;
        }
        ctx.toast(String((result && result.error) || "操作失败"), true);
        /* 失败也重拉：多半是"找不到这一段"，本地那份已经是过期的画面了 */
        state.editing = "";
        state.confirming = "";
        await renderAndLoad(state.book);
        return false;
      } catch (error) {
        ctx.toast(String((error && error.message) || error), true);
        return false;
      }
    }

    /* ---- 昵称映射：who 是原字符串，映射不到就原样显示（绝不显示空） ----
       第 9 步：**优先显示中文名字**。取值顺序 name → label（去掉尾巴的"(数字)"）→ 原 id。
       正文里不该出现「小满(1411638634)」这种下拉格式，只该有「小满」。 */
    function whoName(raw) {
      var key = String(raw === undefined || raw === null ? "" : raw);
      if (!key) return "";
      var opts = (ctx.state && ctx.state.options) || [];
      for (var i = 0; i < opts.length; i += 1) {
        var o = opts[i];
        if (!o || String(o.id) !== key) continue;
        if (o.name) return String(o.name);
        if (o.label) return String(o.label).replace(/\s*\([^)]*\)\s*$/, "");
        return key;
      }
      return key;
    }

    /* ---- 日历 ---- */
    function dayIndex(days) {
      var out = {};
      (days || []).forEach(function (d) { if (d && d.date) out[d.date] = d; });
      return out;
    }

    function renderCalendar(UI, info, st) {
      var days = info.days || [];
      var index = dayIndex(days);
      var base = st.month || (info.latest_date ? String(info.latest_date).slice(0, 7) : ym(new Date()));
      if (!st.month) st.month = base;
      var parts = String(st.month).split("-");
      var year = Number(parts[0]), mon = Number(parts[1]);
      var first = new Date(year, mon - 1, 1);
      var lead = (first.getDay() + 6) % 7;            /* 周一开头 */
      var total = new Date(year, mon, 0).getDate();   /* 本月天数 */
      var today = todayYmd();

      var grid = UI.h("div", { class: "cal-grid" });
      WEEK_CN.forEach(function (w) { grid.appendChild(UI.h("div", { class: "cal-wd", text: w })); });
      for (var i = 0; i < lead; i += 1) grid.appendChild(UI.h("div", { class: "cal-cell is-blank" }));
      for (var day = 1; day <= total; day += 1) {
        (function (day) {
          var key = year + "-" + pad(mon) + "-" + pad(day);
          var rec = index[key];
          var cls = "cal-cell";
          if (key === today) cls += " is-today";
          if (key === st.date) cls += " is-pick";
          if (rec) cls += " has-diary";
          var el = UI.h("div", { class: cls, role: "button", tabindex: "0" }, [
            UI.h("span", { class: "cal-num", text: String(day) }),
          ]);
          if (rec) {
            var n = Number(rec.count) || 0;
            var badge = n >= 3 ? "3+" : String(n);
            el.appendChild(UI.h("span", { class: "cal-dot", text: badge }));
            el.setAttribute("title", shortDate(key) + " · " + n + " 篇 · " + (Number(rec.chars) || 0) + " 字");
          } else {
            el.setAttribute("title", shortDate(key) + " · 没有日记");
          }
          var choose = function () {
            /* 再点同一天 = 取消筛选，回"最近若干条" */
            st.date = (st.date === key) ? "" : key;
            st.page = 0;
            state.book = info.book;      /* 看哪本 = 哪本，正文区跟着这本走 */
            renderAndLoad(info.book);   /* 要重画：is-pick 是在 renderCalendar 里按 st.date 算的 */
          };
          el.addEventListener("click", choose);
          /* 键盘可达：格子上只挂 click 的话，tabindex="0" 能聚焦但按不出效果 */
          el.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
              if (event.preventDefault) event.preventDefault();
              choose();
            }
          });
          grid.appendChild(el);
        })(day);
      }

      var head = UI.h("div", { class: "cal-head" }, [
        UI.h("button", { class: "btn btn-sm", type: "button", text: "‹", title: "上个月",
          onclick: function () { st.month = shiftMonth(st.month, -1); renderAndLoad(info.book); } }),
        UI.h("span", { class: "cal-title", text: year + " 年 " + mon + " 月" }),
        UI.h("button", { class: "btn btn-sm", type: "button", text: "›", title: "下个月",
          onclick: function () { st.month = shiftMonth(st.month, 1); renderAndLoad(info.book); } }),
        UI.h("button", { class: "btn btn-sm", type: "button", text: "回到本月", title: "回到本月并选中今天",
          onclick: function () {
            st.month = ym(new Date()); st.date = todayYmd(); st.page = 0;
            state.book = info.book;
            renderAndLoad(info.book);
          } }),
      ]);
      if (info.first_date) {
        head.appendChild(UI.h("button", { class: "btn btn-sm", type: "button",
          text: "跳到最早一天", title: "跳到有日记的最早一天：" + info.first_date,
          onclick: function () {
            st.month = String(info.first_date).slice(0, 7);
            st.date = String(info.first_date);
            st.page = 0;
            state.book = info.book;
            renderAndLoad(info.book);
          } }));
      }

      var wrap = UI.h("div", { class: "cal-wrap" }, [head, grid]);
      if (info.days_truncated) {
        wrap.appendChild(UI.h("div", { class: "cal-note", text: "更早的不在日历里（每天只保留最近一段）" }));
      }
      if (!days.length) {
        wrap.appendChild(UI.h("div", { class: "cal-note", text: "还没有日记，先陪她聊两句吧 ✎" }));
      }
      return wrap;
    }

    /* ---- 单页日记本 ---- */
    function renderEntry(UI, book, entry, window_) {
      var head = [];
      if (entry.time) head.push(UI.h("span", { class: "np-time", text: entry.time }));
      if (entry.mood) head.push(UI.h("span", { class: "np-mood", text: entry.mood }));
      var who = whoName(entry.who);
      if (who) head.push(UI.h("span", { class: "np-who", text: "和 " + who }));

      /* 后端说能改才给按钮：``seg_id`` / ``editable`` 缺一个就当"不能改"，
           老后端（没这两个键）于是自动退回纯只读，不会画出按了必失败的按钮。 */
      var editable = !!(window_ && window_.can_edit) && !!entry.seg_id && entry.editable !== false;
      var node = UI.h("article", { class: "np-entry" });
      if (head.length) node.appendChild(UI.h("div", { class: "np-meta" }, head));

      if (editable && state.editing === entry.seg_id) {
        node.appendChild(editorFor(UI, book, entry));
        return node;
      }

      var body = UI.h("div", { class: "np-body" });
      /* 按空行分段：她写日记是分段的，糊成一段就没法读 */
      String(entry.text || "").split(/\n\s*\n/).forEach(function (para) {
        var t = para.replace(/\s+$/, "");
        if (t.trim() === "") return;
        body.appendChild(UI.h("p", { class: "np-p", text: t }));
      });
      if (!body.children.length) body.appendChild(UI.h("p", { class: "np-p muted", text: "（这条是空的）" }));
      node.appendChild(body);
      node.appendChild(entryActions(UI, book, entry, editable, window_));
      return node;
    }

    /* 不可改的原因：给 title 用，也是"为什么没有按钮"的解释。 */
    function lockedReason(window_) {
      if (!window_ || !window_.can_edit) return "这版后端还没有改删接口";
      if (window_.unlimited) return "";
      return "只能改最近 " + (window_.within_days || 0) + " 天的；要在面板里改任意一段，"
        + "去「设置」打开「面板不受可改天数限制」";
    }

    function entryActions(UI, book, entry, editable, window_) {
      var row = UI.h("div", { class: "row-actions np-actions" });
      if (!editable) {
        var why = lockedReason(window_);
        if (why) row.appendChild(UI.h("span", { class: "np-locked", text: "🔒 " + why }));
        return row;
      }
      var confirming = state.confirming === entry.seg_id;
      row.appendChild(UI.h("button", {
        class: "btn btn-sm", type: "button", text: "编辑",
        title: "改这一段的正文（开头那行日期与心情不动）",
        onclick: function () { state.editing = entry.seg_id; state.confirming = ""; renderAndLoad(book); },
      }));
      if (confirming) {
        /* 两步确认：删除可还原（进回收站），但仍不做一个键直接抹掉。 */
        row.appendChild(UI.h("span", { class: "np-confirm", text: "删掉这一段？" }));
        row.appendChild(UI.h("button", {
          class: "btn btn-sm btn-danger", type: "button", text: "确认删除",
          onclick: function () { doDelete(book, entry); },
        }));
        row.appendChild(UI.h("button", {
          class: "btn btn-sm", type: "button", text: "算了",
          onclick: function () { state.confirming = ""; renderAndLoad(book); },
        }));
      } else {
        row.appendChild(UI.h("button", {
          class: "btn btn-sm btn-danger", type: "button", text: "删除",
          title: "删掉这一段（先进回收站，可还原）",
          onclick: function () { state.confirming = entry.seg_id; state.editing = ""; renderAndLoad(book); },
        }));
      }
      return row;
    }

    /* 真删：确认态那颗按钮走这里。endpoint 写 ENDPOINTS 的**键**，不写路径。 */
    function doDelete(book, entry) {
      return mutate("diaryDelete", { book: book, seg_id: entry.seg_id }, "已删除（进了回收站，可还原）");
    }

    /* ---- 行内编辑器 ---- */
    function editorFor(UI, book, entry) {
      var area = UI.h("textarea", {
        class: "np-editor-text", rows: "6", spellcheck: "false",
        placeholder: "正文（空行分段；开头那行日期与心情不动）",
      });
      area.value = String(entry.text || "");
      var saving = false;
      function save() {
        if (saving) return;              /* 连点两下别发两次 */
        var text = area.value;
        if (!text.trim()) { ctx.toast("正文不能为空（想删掉请用删除）", true); return; }
        saving = true;
        mutate("diaryRewrite", { book: book, seg_id: entry.seg_id, text: text }, "已改好（原文已备份）");
      }
      var box = UI.h("div", { class: "np-editor" }, [
        area,
        UI.h("div", { class: "row-actions np-actions" }, [
          UI.h("button", { class: "btn btn-sm", type: "button", text: "保存", onclick: save }),
          UI.h("button", {
            class: "btn btn-sm", type: "button", text: "取消",
            onclick: function () { state.editing = ""; renderAndLoad(book); },
          }),
          UI.h("span", { class: "np-locked", text: "只改正文；开头那行是历史，不动。" }),
        ]),
      ]);
      /* Ctrl/Cmd+Enter 直接保存——写长一点的东西时少松一次鼠标 */
      area.addEventListener("keydown", function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
          if (event.preventDefault) event.preventDefault();
          save();
        }
      });
      return box;
    }

    function renderNotebook(UI, book, data, st, window_) {
      var wrap = UI.h("div", { class: "notebook" });
      var entries = Array.isArray(data.entries) ? data.entries : null;
      if (!entries) entries = null;

      /* 老后端没有 entries：退回 text，一条都不能少 */
      if (!entries) {
        wrap.appendChild(UI.h("div", { class: "np-pagehead" }, [
          UI.h("span", { class: "np-datestamp", text: (book === "love" ? "恋爱日记" : "日记") }),
        ]));
        wrap.appendChild(UI.h("pre", { class: "body", text: data.text || "（空）" }));
        return wrap;
      }

      if (!entries.length) {
        wrap.appendChild(UI.empty("这一段还没有内容 ✎"));
        return wrap;
      }

      /* 选哪几条上页：
         选中某天 = 那天全部顺次排在一页上；
         "最近若干条" = 一页放 1 条，别让人滚三屏。 */
      var pageEntries, pageIndex = 0, totalPages = 1;
      if (st.date) {
        pageEntries = entries;
      } else {
        pageEntries = [entries[Math.min(st.page, entries.length - 1)]];
        totalPages = entries.length;
        pageIndex = Math.min(st.page, entries.length - 1);
      }

      var stamp = pageEntries[0] && pageEntries[0].date ? pageEntries[0].date : (st.date || "");
      wrap.appendChild(UI.h("div", { class: "np-pagehead" }, [
        UI.h("span", { class: "np-datestamp", text: "— " + stamp + " " + weekdayCn(stamp) + " —" }),
        UI.h("span", { class: "np-count", text: "本次 " + (data.count || entries.length) + " 条 / 全书 " + (data.total || 0) + " 条" }),
      ]));
      pageEntries.forEach(function (entry) { wrap.appendChild(renderEntry(UI, book, entry, window_)); });
      wrap.appendChild(UI.h("div", { class: "np-foot", text: "· · ·" }));

      if (totalPages > 1) {
        wrap.appendChild(UI.h("div", { class: "np-pager" }, [
          UI.h("button", { class: "btn btn-sm", type: "button", text: "上一页", disabled: pageIndex <= 0,
            onclick: function () { st.page = Math.max(0, st.page - 1); loadContent(book); } }),
          UI.h("span", { class: "np-pageno", text: (pageIndex + 1) + " / " + totalPages }),
          UI.h("button", { class: "btn btn-sm", type: "button", text: "下一页", disabled: pageIndex >= totalPages - 1,
            onclick: function () { st.page = Math.min(totalPages - 1, st.page + 1); loadContent(book); } }),
        ]));
      }
      return wrap;
    }

    /* ---- 正文区 ----
       **每次都清掉上一块再挂新的**：早先是 appendChild 累加，refresh 时只调一次
       看不出来，但日历点日期 / 翻页会连点，一天下来叠出一堆旧面板。 */
    var contentSlot = null;
    var cardSlot = null;
    /* 卡片预览的舞台与 iframe 本体，外加 resize 的绑定状态（第 18 步）。
       换 tab 时 unmount 会把这几个清掉并摘监听，免得切一次挂一个。 */
    var cardStage = null;
    var cardFrame = null;
    var resizeBound = false;
    var resizeTimer = null;
    async function loadContent(book) {
      var UI = ctx.UI;
      var st = slot(book);
      if (contentSlot) contentSlot.remove();
      var box = UI.h("div", { class: "panel np-panel" }, [UI.h("div", { class: "muted", text: "读取中…" })]);
      contentSlot = box;
      holder.appendChild(box);
      try {
        var data = await ctx.apiGet("diaryContent", {
          book: book, date: st.date, tail: st.date ? 0 : state.tail,
          who: ctx.who(),
        });
        /* 窗口口径以本次拉到的为准（别缓存：设置里那个开关一改就变了） */
        var window_ = data.edit_window || null;
        state.window = window_;
        UI.clear(box);
        box.appendChild(renderNotebook(UI, book, data, st, window_));
        var tools = UI.h("div", { class: "row-actions np-tools" });
        if (st.date) {
          tools.appendChild(UI.h("button", { class: "btn btn-sm", type: "button", text: "看最近 " + state.tail + " 条",
            onclick: function () { st.date = ""; st.page = 0; renderAndLoad(book); } }));
        }
        if (tools.children.length) box.appendChild(tools);
      } catch (error) {
        UI.clear(box);
        box.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    /* ---- 回收站（第 15 步）--------------------------------------------------
       放在日记 tab 底部而不是另开一页：删完一条就得马上能看见它、马上能还原，
       换 tab 去找等于把"可撤销"这件事藏起来。
       只列面板删掉的那些**单段**记录（``seg-*.json``）；聊天侧 ``diary_edit`` 留的
       整本快照不在这里——整本回写会连带抹掉这之后的写入，按钮上写"还原"就是骗人。 */
    var trashSlot = null;
    async function loadTrash() {
      var UI = ctx.UI;
      if (trashSlot) trashSlot.remove();
      var box = UI.h("div", { class: "panel np-panel" }, [UI.h("div", { class: "muted", text: "读取回收站…" })]);
      trashSlot = box;
      holder.appendChild(box);
      try {
        var data = await ctx.apiGet("diaryTrash", {});
        UI.clear(box);
        var items = data.items || [];
        var head = UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: "🗑 回收站 · " + (data.count || 0) + " 条" }),
            UI.h("div", { class: "row-sub",
              text: "面板删掉的日记先落在这里（上限 " + (data.cap || 0) + " 条，超了丢最旧的）；还原会插回它原本的时间位置" }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: state.trashOpen ? "收起" : "展开",
              onclick: function () { state.trashOpen = !state.trashOpen; renderAndLoad(state.book); },
            }),
          ]),
        ]);
        box.appendChild(head);
        if (!items.length) {
          box.appendChild(UI.empty("回收站是空的"));
        } else if (state.trashOpen) {
          items.forEach(function (item) {
            box.appendChild(trashRow(UI, item));
          });
        }
      } catch (error) {
        UI.clear(box);
        box.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    function trashRow(UI, item) {
      var who = whoName(item.who);
      var when = item.deleted_at ? " · 删于 " + UI.shortTime(item.deleted_at) : "";
      return UI.h("div", { class: "row" }, [
        UI.h("div", { class: "row-main" }, [
          UI.h("div", { class: "row-title", text: (item.is_love ? "💗 " : "📖 ") + item.book_display + " · " + item.date + " " + item.time }),
          UI.h("div", { class: "row-sub",
            text: (item.mood ? "（" + item.mood + "）" : "") + (who ? " 和 " + who + " · " : "")
              + (item.preview || "（空）") + when }),
        ]),
        UI.h("div", { class: "row-actions" }, [
          UI.h("button", {
            class: "btn btn-sm", type: "button", text: "还原",
            title: "把这一段放回 " + item.book_display + " 里原本的时间位置",
            onclick: function () {
              mutate("diaryRestore", { id: item.id }, "已还原到 " + item.date);
            },
          }),
        ]),
      ]);
    }

    /* ---- 整屏重画：书头 + 日历 + 正文 ---- */
    function render() {
      var UI = ctx.UI;
      UI.clear(holder);
      contentSlot = null;      /* 整屏重画后旧引用已失效 */
      trashSlot = null;
      cardSlot = null;
      cardStage = null;        /* 同样失效：留着会让 resize 去量一个已卸载的 iframe */
      cardFrame = null;
      var books = state.books || [];
      books.forEach(function (info) {
        var st = slot(info.book);
        var card = UI.h("div", { class: "card panel" });
        var open = isOpen(info);

        card.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: (info.is_love ? "💗 " : "📖 ") + info.display + " · " + info.entries + " 篇" }),
            UI.h("div", { class: "row-sub",
              text: UI.bytes(info.chars) + " 正文 · 最近更新 " + (info.latest_date || "—") +
                (info.updated_at ? " · " + UI.shortTime(info.updated_at) : "") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", { class: "btn btn-sm", type: "button", text: open ? "收起" : "展开",
              onclick: function () {
                state.collapsed[info.book] = !isOpen(info);   /* 现在开着 → 记 false（收起）；收着 → 记 true（展开） */
                /* 收起/展开改了可见性 → 重画后必须补内容，否则正文凭空消失。
                   **点哪张卡就加载哪本**（6.3 步改的：原来取"当前展开的第一本"，
                   点恋爱日记的展开却去读 normal，不顺）。都收起就不加载。 */
                if (isOpen(info)) { state.book = info.book; renderAndLoad(info.book); }
                /* 都收起就一本都不加载，但**回收站照常显示**——它是全局的，
                   跟哪本展开没关系（走 renderAndLoad(null)，正文区自然空着）。 */
                else { var keep = firstOpen(); renderAndLoad(keep ? keep.book : null); }
              } }),
            UI.h("button", { class: "btn btn-sm", type: "button", text: "看最近 " + state.tail + " 条",
              onclick: function () {
                st.date = ""; st.page = 0;
                state.book = info.book;
                if (!open) state.collapsed[info.book] = true;   /* 收着的那本：先撑开，否则正文加载了卡片还是折着的 */
                renderAndLoad(info.book);
              } }),
          ]),
        ]));

        if (open) {
          card.appendChild(renderCalendar(UI, info, st));
          var tailSel = UI.h("select", { class: "pill-select" },
            [5, 20, 50].map(function (n) {
              return UI.h("option", { value: String(n), text: "最近 " + n + " 条", selected: n === state.tail });
            }));
          tailSel.addEventListener("change", function () {
            state.tail = Number(tailSel.value) || 20;
            var s = slot(info.book);
            s.date = ""; s.page = 0;      /* 改条数 = 放弃按天筛选，回到"最近 N 条" */
            state.book = info.book;
            /* 要重画：否则日历上旧的 is-pick 会留着（日期已经清了，格子还亮着） */
            renderAndLoad(info.book);
          });
          /* ⚠️ 必须挂进 card —— 少了这一句，下面整块（连同"最近 N 条"下拉）都不进 DOM，
             页面看着只是"少了个选择框"，很难第一眼看出是哪句丢了。 */
          card.appendChild(UI.h("div", { class: "row-actions np-tail" }, [
            tailSel,
            /* 正文 / 卡片 两个看法（第 16 步）。卡片看的是**真渲染**出来的那张，
               翻页就是按天翻——省得为了确认一句样式好不好看去 QQ 上戳一次。 */
            UI.h("div", { class: "seg np-view" }, [
              ["text", "正文"], ["card", "日记本"],
            ].map(function (pair) {
              return UI.h("button", {
                class: "seg-btn" + (state.view === pair[0] ? " is-on" : ""),
                type: "button", "data-view": pair[0], text: pair[1],
                onclick: function () {
                  if (state.view === pair[0]) return;
                  state.view = pair[0];
                  renderAndLoad(info.book);
                },
              });
            })),
          ]));
        }
        holder.appendChild(card);
      });

      if (!books.length) holder.appendChild(UI.empty("还没有日记文件"));
    }

    async function refresh() {
      var data = await ctx.apiGet("diaryList", { who: ctx.who() });
      state.books = data.books || [];
      render();
      if (state.view === "card") await loadCard(state.book);
      else await loadContent(state.book);
      await loadTrash();
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () {
        holder = null; trashSlot = null; cardSlot = null;
        cardStage = null; cardFrame = null;
        /* 摘掉 resize 监听：匿名函数挂上去就摘不掉，所以绑的是 onWindowResize。
           不摘的话每切一次 tab 多挂一个，切几十次就几十个回调。 */
        if (resizeBound) {
          resizeBound = false;
          window.removeEventListener("resize", onWindowResize);
        }
        if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = null; }
      },
    };
  };
})(window);
