/* 日记（第 6.2 步）：单本日记本 + 日历 + 改 / 删 / 留言 / 回收站。

   这版按"主人用着顺手"重做了三件事，其余功能（卡片预览、导出为图片、回收站、
   留言、编辑删除、翻页）全部保留：

   1) **单本常驻切换**（第 19 步）：原来两本各占一张大卡、恋爱日记默认收起，
      想看恋爱日记得先"展开"再点一次。本版改成顶上一排常驻的两段切换，
      面板里**永远只有一本**在展开，日历和正文都跟着当前本走。
      主人首次优先直接落在**可访问的恋爱日记**上，之后记住本人的选择。
   2) **状态按人存在 ctx.state 里**（`ctx.state.diaryUI[who]`）：日期 / 月份 /
      本子 / 正文·日记本显示方式都按人分开记。切 tab 回来不丢，切人也不串。
      草稿（编辑中的 seg_id、留言草稿）同样按人分开，绝不跨人串。
   3) **尽量原位更新**：编辑 / 取消 / 删除确认 / 留言都只换掉那一段节点，
      不再整屏重画——整屏重画会带走滚动位置和输入焦点（用户反馈 2026-10-18）。
      外壳（日历 + 控制条 + 正文 + 回收站）拆成四块各自更新，翻页只动正文。

   **异步纪律**：每一次读取都带 generation 守卫，**旧响应绝不覆盖新选择**——
   切本 / 换日期时先发的请求后到，必须被丢弃（否则会看到上一本的日记）。

   **向后兼容**：后端没给 `entries` 时退回 `text`，没给 `seg_id` / `editable` /
   `edit_window` 时一律不给改删按钮。改删靠 seg_id 定位，写操作后重拉正文，
   绝不拿本地旧内容接着改。 */
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
    var UI = ctx.UI;
    /* 三条读取各自一个 generation：正文 / 回收站 / 卡片互不干扰，
       但都遵守"后发的请求才准画"。 */
    var contentGeneration = 0;
    var trashGeneration = 0;
    var cardGeneration = 0;
    /* 视图级：连着两次 refresh（顶栏刷新 / 换人）时，旧的那次不许覆盖新的。 */
    var refreshCounter = 0;

    var books = [];          /* /diary/list 回的两本概览 */
    var currentBook = "";    /* 当前正在看哪本 */
    var editWindow = null;   /* 本次拉到的窗口口径 */

    /* 外壳四块：书头 / 日历 / 控制条 / 正文 / 回收站。分开是为了**只换要换的那块**，
       其余保持原样——滚动位置和光标就不会被整屏重画带走。 */
    var headSlot = null, calSlot = null, ctrlSlot = null, contentSlot = null, trashSlot = null;

    /* ---- 跨 tab 存活的、按人分开的状态（ctx.state.diaryUI[who]）----
       ctx.state 是 app.js 里那个跨视图的 state；视图每次重建，这里跟着它活下来。 */
    function rootState() {
      if (!ctx.state) ctx.state = {};
      return ctx.state;
    }
    function personBox() {
      var root = rootState();
      if (!root.diaryUI || typeof root.diaryUI !== "object") root.diaryUI = {};
      var key = String((typeof ctx.who === "function" ? ctx.who() : ctx.who) || "");
      /* ⚠️ 跨 scope 隔离（三端整合 P0）：键里带 scope 前缀，
         两个实例里的**同名**用户各存各的草稿，A 的草稿绝不会出现在 B。
         老 ctx 没有 scopeKey（测试里的旧夹具）→ 前缀为空，键不变。 */
      var scopePrefix = (typeof ctx.scopeKey === "function") ? String(ctx.scopeKey() || "") : "";
      key = scopePrefix + key;
      var box = root.diaryUI[key];
      if (!box || typeof box !== "object") {
        box = root.diaryUI[key] = {
          book: "",          /* 记住本人上次看的是哪本 */
          tail: 20,
          view: "text",      /* 正文 / 日记本（卡片） */
          cardDate: "",
          cardStyle: "",
          editing: "",       /* 正在编辑的 seg_id */
          confirming: "",
          trashOpen: false,
          per: {},           /* 每本各自：{date, month, page} */
          /* **实际草稿**：key = `book|seg_id` → {text}。
             只存 seg_id 不算保存草稿——那只是"哪一段在编辑"，
             用户没保存就切走的话字就丢了。这里存真正敲出来的字。 */
          edits: {},
          /* 留言草稿：同 key → 文字。换人回来能续上，绝不跨人。 */
          comments: {},
        };
      }
      box.tail = Number(box.tail) || 20;
      if (box.view !== "card") box.view = "text";
      if (!box.per || typeof box.per !== "object") box.per = {};
      if (!box.edits || typeof box.edits !== "object") box.edits = {};
      if (!box.comments || typeof box.comments !== "object") box.comments = {};
      return box;
    }
    /* 草稿键：**按人 + 本 + 段**三层分开。不同人同一天同一段也不会互相看见。
       seg_id 变了就是另一个键，旧草稿自然套不上去（"这段已经变了"的兜底）。 */
    function draftKey(book, segId) { return book + "|" + String(segId || ""); }
    function getDraft(book, segId) {
      var v = personBox().edits[draftKey(book, segId)];
      return typeof v === "string" ? v : null;
    }
    function setDraft(book, segId, text) {
      personBox().edits[draftKey(book, segId)] = String(text);
    }
    function clearDraft(book, segId) {
      delete personBox().edits[draftKey(book, segId)];
    }
    function getCommentDraft(book, segId) {
      var v = personBox().comments[draftKey(book, segId)];
      return typeof v === "string" ? v : null;
    }
    function setCommentDraft(book, segId, text) {
      personBox().comments[draftKey(book, segId)] = String(text);
    }
    function clearCommentDraft(book, segId) {
      delete personBox().comments[draftKey(book, segId)];
    }
    function per(book) {
      var box = personBox();
      var p = box.per[book];
      if (!p || typeof p !== "object") p = box.per[book] = { date: "", month: "", page: 0 };
      if (typeof p.date !== "string") p.date = "";
      if (typeof p.month !== "string") p.month = "";
      p.page = Number(p.page) || 0;
      return p;
    }

    /* 主人是谁：只认 app.js 合并好的 state.options 里的 is_owner，不猜。 */
    function isOwner() {
      var id = String((typeof ctx.who === "function" ? ctx.who() : ctx.who) || "");
      var opts = (ctx.state && ctx.state.options) || [];
      var hit = opts.filter(function (one) { return one && String(one.id) === id; })[0];
      return !!(hit && hit.is_owner);
    }
    function bookInfo(book) {
      return books.filter(function (one) { return one && one.book === book; })[0] || null;
    }

    /* 选哪本：**记住的优先**。没记过就按"主人首次优先恋爱日记"落位——
       她日常看的就是恋爱本，默认藏着等于每次进来都多点一次（用户反馈 2026-10-18）。
       恋爱本在这一档下取不到内容时才退回普通日记，不是无脑无视用户选择。 */
    function pickBook() {
      var box = personBox();
      var known = books.filter(function (one) { return one && one.book; });
      if (!known.length) { currentBook = box.book || "normal"; return currentBook; }
      var names = known.map(function (one) { return one.book; });
      if (box.book && names.indexOf(box.book) >= 0) { currentBook = box.book; return currentBook; }
      var love = bookInfo("love");
      var normal = bookInfo("normal");
      var next;
      if (isOwner() && love && Number(love.entries) > 0) next = "love";
      else if (normal && Number(normal.entries) > 0) next = "normal";
      else if (love && Number(love.entries) > 0) next = "love";
      else next = love ? "love" : "normal";
      box.book = next;              /* 记住本人这次的落位 */
      currentBook = next;
      return currentBook;
    }

    /* ---- 昵称映射：who 是原字符串，映射不到就原样显示（绝不显示空） ----
       优先显示中文名字，正文里不该出现「小满(1411638634)」这种下拉格式。 */
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

    /* ---- 卡片预览（第 16 步；第 18 步补"按内容自适应高度 + PC 端缩小"）----
       后端把模板渲成 HTML 再给我们，所以这里拿到的就是真卡片。
       用 iframe + srcdoc 装：卡片自带 body 背景与整页尺寸，塞进当前页会串味。 */
    var cardStage = null, cardFrame = null, resizeBound = false, resizeTimer = null;

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
    function refitCard() {
      if (cardStage && cardFrame) fitCardHeight(cardStage, cardFrame);
    }
    function onWindowResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(refitCard, 150);
    }

    /* cardSlot = 这次卡片读取要往哪个槽里画。记下来，回包时校验还是同一个槽。 */
    var cardSlot = null;

    async function loadCard(book) {
      if (!holder) return;
      var generation = ++cardGeneration;
      var box = personBox();
      var slot = contentSlot;
      if (!slot) return;
      cardSlot = slot;
      /* 换卡时不先清空：留旧的撑着，等新的 html 到了再换（少一次白屏）。 */
      var stage = UI.h("div", { class: "np-cardstage" });
      var scale = UI.h("div", { class: "np-cardscale" });
      var frame = UI.h("iframe", { class: "np-cardframe", title: "日记本预览" });
      frame.setAttribute("sandbox", "allow-same-origin");
      frame.addEventListener("load", function () { fitCardHeight(stage, frame); });
      /* 卡片同样**不清整区**：旧内容留着，只挂一条小状态。 */
      markBusy(true, "渲染日记本…");
      try {
        var data = await ctx.apiGet("diaryCard", {
          book: book, date: per(book).date, style: box.cardStyle, who: ctx.who(),
        });
        /* 五重守卫：视图还在 / 这次读取仍是最新 / 期间没人换本 / 槽没换 /
           **而且现在仍然是"日记本"视图**——切回正文后迟到的卡片响应一律丢弃。 */
        if (!holder || generation !== cardGeneration || book !== currentBook) return;
        if (slot !== cardSlot || contentSlot !== slot) return;
        if (personBox().view !== "card") return;
        UI.clear(slot);
        markIdle();
        if (data.empty) {
          slot.appendChild(UI.empty("这本还没有日记，先陪她聊两句吧 ✎"));
          return;
        }
        var selected = per(book);
        selected.date = data.date || "";
        if (selected.date) selected.month = selected.date.slice(0, 7);
        selected.page = 0;
        renderCalendar();
        box.cardStyle = data.style || "";
        frame.srcdoc = data.html || "";
        scale.appendChild(frame);
        stage.appendChild(scale);
        slot.appendChild(UI.h("div", { class: "np-notebook" }, [stage, cardBar(data)]));
        cardStage = stage;
        cardFrame = frame;
        if (!resizeBound) { resizeBound = true; window.addEventListener("resize", onWindowResize); }
      } catch (error) {
        if (!holder || generation !== cardGeneration || slot !== cardSlot) return;
        if (personBox().view !== "card") return;
        UI.clear(slot);
        markIdle();
        slot.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    /* ---- 日记本页：左卡片 + 右侧竖排按钮栏 --------------------------------------
       左边是卡片，右边一整条竖排按钮栏：上一页 / 下一页 → 导出为图片 →
       风格标题 → 六款版式 → 保存配置。保存配置单独落在最底下。 */
    function cardBar(data) {
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
            onclick: function () { if (!prevDay) return; per(currentBook).date = prevDay; loadCard(currentBook); },
          }),
          UI.h("button", {
            class: "btn", type: "button", text: "下一页 ›",
            disabled: at >= total - 1, title: nextDay ? "翻到 " + nextDay : "已经是最早的一页",
            onclick: function () { if (!nextDay) return; per(currentBook).date = nextDay; loadCard(currentBook); },
          }),
        ]),
        exportBtn,
        UI.h("div", { class: "np-rail-label", text: "风格" }),
        cardStyleRail(data),
        saveBtnFor(data),
      ]);
    }

    /* 挑款式只改预览；满意了按「保存配置」才落盘。 */
    function cardStyleRail(data) {
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
          onclick: function () { personBox().cardStyle = style; loadCard(currentBook); },
        }));
      });
      return rail;
    }

    function saveBtnFor(data) {
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

    /* 导出为图片：后端出图，和聊天里 /看日记 出的是同一张。 */
    async function exportCard(btn) {
      if (btn.disabled) return;
      var oldText = btn.textContent;
      var box = personBox();
      btn.disabled = true;
      btn.textContent = "导出中…";
      try {
        var data = await ctx.apiGet("diaryCardImage", {
          book: currentBook, date: per(currentBook).date, style: box.cardStyle, who: ctx.who(),
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

    /* 保存配置：走 settings 的 POST，面板和私聊 /看日记 看到同一份。
       只提交这一项，不把整份配置原样发一遍。 */
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

    /* ---- 书头 + 常驻本子切换 ----
       两段控件常驻在面板顶部，**不用展开/收起**：面板里永远只有 currentBook 一本，
       下面日历和正文都跟着它走。点一下就切，不用滚到下面去找。 */
    function renderHead() {
      if (!headSlot) return;
      UI.clear(headSlot);
      if (!books.length) return;
      var box = personBox();
      var seg = UI.h("div", { class: "seg np-bookseg", role: "group", "aria-label": "选择日记本" },
        books.map(function (info) {
          return UI.h("button", {
            class: "seg-btn" + (info.book === currentBook ? " is-on" : ""),
            type: "button",
            "data-book": info.book,
            title: (info.is_love ? "恋爱日记" : "日记") + " · " + info.entries + " 篇",
            text: (info.is_love ? "💗 " : "📖 ") + info.display + " · " + info.entries + " 篇",
            /* 返回 promise：调用方要能 await 到"这一本真的画出来了" */
            onclick: function () { return switchBook(info.book); },
          });
        })
      );
      var info = bookInfo(currentBook);
      headSlot.appendChild(UI.h("div", { class: "row np-bookrow" }, [
        UI.h("div", { class: "row-main" }, [
          UI.h("div", { class: "row-sub", text: info
            ? UI.bytes(info.chars) + " 正文 · 最近更新 " + (info.latest_date || "—") +
              (info.updated_at ? " · " + UI.shortTime(info.updated_at) : "")
            : "" }),
        ]),
        UI.h("div", { class: "row-actions" }, [seg]),
      ]));
      /* 当前本没内容时给一句话，不让空面板像坏了 */
      if (info && !Number(info.entries)) {
        headSlot.appendChild(UI.empty(info.is_love
          ? "这本恋爱日记还是空的，先陪她聊两句吧 ✎"
          : "这本日记还是空的，先陪她聊两句吧 ✎"));
      }
    }

    function switchBook(book) {
      if (!book || book === currentBook) return;
      currentBook = book;
      personBox().book = book;      /* 记住本人的选择，下次进来就在这本 */
      /* 换本 = 换内容槽：作废所有在途响应，旧的晚到也不准画 */
      contentGeneration += 1;
      cardGeneration += 1;
      renderHead();
      renderCalendar();
      renderControls();
      /* 返回 promise：调用方（以及测试）要能 await 到"这一本真的画出来了" */
      return paintContent();
    }

    /* ---- 控制条：最近 N 条 + 正文/日记本 ---- */
    function renderControls() {
      if (!ctrlSlot) return;
      UI.clear(ctrlSlot);
      if (!books.length) return;
      var box = personBox();
      var st = per(currentBook);
      var tailSel = UI.h("select", { class: "pill-select", "aria-label": "显示最近多少条" },
        [5, 20, 50].map(function (n) {
          return UI.h("option", { value: String(n), text: "最近 " + n + " 条", selected: n === box.tail });
        }));
      tailSel.addEventListener("change", function () {
        box.tail = Number(tailSel.value) || 20;
        st.date = ""; st.page = 0;      /* 改条数 = 放弃按天筛选 */
        renderCalendar();
        renderControls();
        paintContent();
      });
      var view = UI.h("div", { class: "seg np-view" },
        [["text", "正文"], ["card", "日记本"]].map(function (pair) {
          return UI.h("button", {
            class: "seg-btn" + (box.view === pair[0] ? " is-on" : ""),
            type: "button", "data-view": pair[0], text: pair[1],
            onclick: function () {
              if (box.view === pair[0]) return;
              if (pair[0] === "card" && !st.date && st.shownDate) st.date = st.shownDate;
              box.view = pair[0];
              renderControls();
              paintContent();
            },
          });
        }));
      ctrlSlot.appendChild(UI.h("div", { class: "row-actions np-tail" }, [tailSel, view]));
    }

    /* ---- 日历 ---- */
    function dayIndex(days) {
      var out = {};
      (days || []).forEach(function (d) { if (d && d.date) out[d.date] = d; });
      return out;
    }

    function renderCalendar() {
      if (!calSlot) return;
      UI.clear(calSlot);
      var info = bookInfo(currentBook);
      if (!info) return;
      var days = info.days || [];
      var index = dayIndex(days);
      var st = per(currentBook);
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
            el.appendChild(UI.h("span", { class: "cal-dot", text: n >= 3 ? "3+" : String(n) }));
            el.setAttribute("title", shortDate(key) + " · " + n + " 篇 · " + (Number(rec.chars) || 0) + " 字");
          } else {
            el.setAttribute("title", shortDate(key) + " · 没有日记");
          }
          var choose = function () {
            /* 再点同一天 = 取消筛选，回"最近若干条" */
            st.date = (st.date === key) ? "" : key;
            st.page = 0;
            /* 只重画日历高亮 + 正文，日历本体不闪、不丢滚动 */
            renderCalendar();
            paintContent();
          };
          el.addEventListener("click", choose);
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
          onclick: function () { st.month = shiftMonth(st.month, -1); renderCalendar(); } }),
        UI.h("span", { class: "cal-title", text: year + " 年 " + mon + " 月" }),
        UI.h("button", { class: "btn btn-sm", type: "button", text: "›", title: "下个月",
          onclick: function () { st.month = shiftMonth(st.month, 1); renderCalendar(); } }),
        UI.h("button", { class: "btn btn-sm", type: "button", text: "回到本月", title: "回到本月并选中今天",
          onclick: function () {
            st.month = ym(new Date()); st.date = todayYmd(); st.page = 0;
            renderCalendar(); paintContent();
          } }),
      ]);
      if (info.first_date) {
        head.appendChild(UI.h("button", { class: "btn btn-sm", type: "button",
          text: "跳到最早一天", title: "跳到有日记的最早一天：" + info.first_date,
          onclick: function () {
            st.month = String(info.first_date).slice(0, 7);
            st.date = String(info.first_date);
            st.page = 0;
            renderCalendar(); paintContent();
          } }));
      }

      var wrap = UI.h("div", { class: "cal-wrap" }, [head, grid]);
      if (info.days_truncated) {
        wrap.appendChild(UI.h("div", { class: "cal-note", text: "更早的不在日历里（每天只保留最近一段）" }));
      }
      if (!days.length) {
        wrap.appendChild(UI.h("div", { class: "cal-note", text: "这本还没有日记，先陪她聊两句吧 ✎" }));
      }
      calSlot.appendChild(wrap);
    }

    /* ---- 改 / 删的写操作：POST → toast → **只重拉正文** ----
       不再整屏重画：整屏重画会带走滚动位置，正在编辑的另一段也会被冲掉。 */
    var mutating = false;
    async function mutate(key, body, okMsg) {
      if (mutating || !holder) return false;
      mutating = true;
      var box = personBox();
      var generation = contentGeneration;
      var person = ctx.who();
      function active() { return !!holder && box === personBox() && generation === contentGeneration; }
      try {
        var result = await ctx.apiPost(key, body);
        if (result && result.ok) {
          /* 写成功了：这一段的本地草稿作废。seg_id 之后可能变，
             留着只会把旧字套到新内容上。 */
          if (body && body.seg_id) delete box.edits[draftKey(body.book || currentBook, body.seg_id)];
          box.editing = "";
          box.confirming = "";
          if (!active()) return true;
          ctx.toast(okMsg);
          /* 篇数和日历跟着写入更新；只更新当前视图，旧写入不能重绘后来选的人。 */
          try {
            var list = await ctx.apiGet("diaryList", { who: person });
            if (!active()) return true;
            books = list.books || [];
            renderHead(); renderCalendar();
          } catch (error) { /* 正文仍继续刷新，已成功的写入不报成失败。 */ }
          if (!active()) return true;
          await paintContent();
          return true;
        }
        if (!active()) return false;
        ctx.toast(String((result && result.error) || "操作失败"), true);
        var unsaved = body && body.seg_id ? box.edits[draftKey(body.book, body.seg_id)] : null;
        var reloadGeneration = contentGeneration + 1;
        await paintContent();
        if (holder && box === personBox() && contentGeneration === reloadGeneration && typeof unsaved === "string" && !entryNodes[body.seg_id]) {
          var recovery = UI.h("textarea", { class: "np-editor-text", rows: "4", readonly: true, "aria-label": "保留的未保存正文" });
          recovery.value = unsaved;
          contentSlot.appendChild(UI.h("div", { class: "err-box" }, [
            UI.h("p", { text: "日记已刷新。你未保存的文字保留在下面，核对后再编辑。" }), recovery,
          ]));
        }
        return false;
      } catch (error) {
        if (active()) ctx.toast(String((error && error.message) || error), true);
        return false;
      } finally {
        mutating = false;
      }
    }

    /* ---- 行内编辑器（显示中→编辑中是原位换节点，不动整页）----
       **草稿是正文本身**：每次输入都按 `book|seg_id` 存进 personBox，
       切 tab 回来 / 换个人再回来都还在。键里带 seg_id，所以"这段已经变了"
       之后新段是另一个键，旧草稿套不上去。 */
    function editorFor(book, entry, node) {
      var area = UI.h("textarea", {
        class: "np-editor-text", rows: "6", spellcheck: "false",
        placeholder: "正文（空行分段；开头那行日期与心情不动）",
      });
      /* 有草稿就用草稿：用户敲到一半切走了，回来看到的是自己写的那段 */
      var saved = getDraft(book, entry.seg_id);
      area.value = saved !== null ? saved : String(entry.text || "");
      area.addEventListener("input", function () { setDraft(book, entry.seg_id, area.value); });
      var saving = false;
      function save() {
        if (saving) return;              /* 连点两下别发两次 */
        var text = area.value;
        if (!text.trim()) { ctx.toast("正文不能为空（想删掉请用删除）", true); return; }
        saving = true;
        area.readOnly = true;
        saveBtn.disabled = true; saveBtn.textContent = "保存中…";
        cancelBtn.disabled = true;
        /* 返回 promise：调用方要能 await 到"真的写完了"，草稿也才是清干净的。 */
        return mutate("diaryRewrite", { book: book, seg_id: entry.seg_id, text: text }, "已改好（原文已备份）")
          .finally(function () {
            saving = false; area.readOnly = false;
            saveBtn.disabled = false; saveBtn.textContent = "保存"; cancelBtn.disabled = false;
          });
      }
      var saveBtn = UI.h("button", { class: "btn btn-sm", type: "button", text: "保存", onclick: save });
      var cancelBtn = UI.h("button", {
        class: "btn btn-sm", type: "button", text: "取消",
        onclick: function () {
          if (saving) return;
          personBox().editing = "";
          if (node && node.parentNode) node.parentNode.replaceChild(renderEntry(book, entry, editWindow), node);
        },
      });
      var box = UI.h("div", { class: "np-editor" }, [
        area,
        UI.h("div", { class: "row-actions np-actions" }, [
          saveBtn,
          /* 取消 = 原位换回正文，不重拉、不闪 */
          cancelBtn,
          UI.h("span", { class: "np-locked", text: "只改正文；开头那行是历史，不动。" }),
        ]),
      ]);
      area.addEventListener("keydown", function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
          if (event.preventDefault) event.preventDefault();
          save();
        }
      });
      return box;
    }

    /* 不可改的原因：给 title 用，也是"为什么没有按钮"的解释。 */
    function lockedReason(window_) {
      if (!window_ || !window_.can_edit) return "这版后端还没有改删接口";
      if (window_.unlimited) return "";
      return "只能改最近 " + (window_.within_days || 0) + " 天的；要在面板里改任意一段，"
        + "去「设置」打开「面板不受可改天数限制」";
    }

    /* ---- 一条日记 ----
       node 由 renderEntry 记进 entryNodes，编辑 / 取消 / 删除确认都**原位换它**，
       不重画整页（滚动位置、光标、其他段的草稿都不受影响）。 */
    var entryNodes = {};

    function renderEntry(book, entry, window_) {
      var head = [];
      if (entry.time) head.push(UI.h("span", { class: "np-time", text: entry.time }));
      if (entry.mood) head.push(UI.h("span", { class: "np-mood", text: entry.mood }));
      var who = whoName(entry.who);
      if (who) head.push(UI.h("span", { class: "np-who", text: "和 " + who }));

      var editable = !!(window_ && window_.can_edit) && !!entry.seg_id && entry.editable !== false;
      var node = UI.h("article", { class: "np-entry" });
      if (entry.seg_id) entryNodes[entry.seg_id] = node;
      if (head.length) node.appendChild(UI.h("div", { class: "np-meta" }, head));

      var box = personBox();
      if (editable && box.editing === entry.seg_id) {
        node.appendChild(editorFor(book, entry, node));
        return node;
      }

      var body = UI.h("div", { class: "np-body" });
      String(entry.text || "").split(/\n\s*\n/).forEach(function (para) {
        var t = para.replace(/\s+$/, "");
        if (t.trim() === "") return;
        body.appendChild(UI.h("p", { class: "np-p", text: t }));
      });
      if (!body.children.length) body.appendChild(UI.h("p", { class: "np-p muted", text: "（这条是空的）" }));
      node.appendChild(body);
      node.appendChild(entryActions(book, entry, editable, window_, node));
      if (entry.seg_id) node.appendChild(commentsFor(book, entry));
      return node;
    }

    function entryActions(book, entry, editable, window_, node) {
      var row = UI.h("div", { class: "row-actions np-actions" });
      if (!editable) {
        var why = lockedReason(window_);
        if (why) row.appendChild(UI.h("span", { class: "np-locked", text: "🔒 " + why }));
        return row;
      }
      var box = personBox();
      var confirming = box.confirming === entry.seg_id;
      row.appendChild(UI.h("button", {
        class: "btn btn-sm", type: "button", text: "编辑",
        title: "改这一段的正文（开头那行日期与心情不动）",
        /* 编辑 = 原位把这篇文章换成编辑器，光标已经在框里，不用来回找 */
        onclick: function () {
          box.editing = entry.seg_id; box.confirming = "";
          if (node && node.parentNode) {
            var fresh = renderEntry(book, entry, editWindow);
            node.parentNode.replaceChild(fresh, node);
            var area = fresh.querySelector("textarea");
            if (area && area.focus) area.focus();
          }
        },
      }));
      if (confirming) {
        row.appendChild(UI.h("span", { class: "np-confirm", text: "删掉这一段？" }));
        row.appendChild(UI.h("button", {
          class: "btn btn-sm btn-danger", type: "button", text: "确认删除",
          onclick: function () { doDelete(book, entry); },
        }));
        row.appendChild(UI.h("button", {
          class: "btn btn-sm", type: "button", text: "算了",
          onclick: function () {
            box.confirming = "";
            if (node && node.parentNode) node.parentNode.replaceChild(renderEntry(book, entry, editWindow), node);
          },
        }));
      } else {
        row.appendChild(UI.h("button", {
          class: "btn btn-sm btn-danger", type: "button", text: "删除",
          title: "删掉这一段（先进回收站，可还原）",
          /* 删除确认也是原位换：只是把按钮换成"确认删除/算了"，不重新拉正文 */
          onclick: function () {
            box.confirming = entry.seg_id; box.editing = "";
            if (node && node.parentNode) node.parentNode.replaceChild(renderEntry(book, entry, editWindow), node);
          },
        }));
      }
      return row;
    }

    /* 真删：确认态那颗按钮走这里。endpoint 写 ENDPOINTS 的键，不写路径。 */
    function doDelete(book, entry) {
      return mutate("diaryDelete", { book: book, seg_id: entry.seg_id }, "已删除（进了回收站，可还原）");
    }

    /* ---- 留言（第 20 步重做）----
       两条纪律：
       - **输入框不重建**：`paint()` 只换列表那半边（留言条 + 状态），下半边的
         textarea / 计数器 / 按钮是常驻节点。旧版每次写操作都重建 textarea，
         保存一次焦点就没了，长留言要重新点回去。
       - 草稿**按人 + 本 + 段**存（`setCommentDraft`）：切 tab 回来、换个人再回来
         都还在；换人绝不会看见上一个人没发出去的字。写成功后清掉。
         只存一个局部变量是不够的——视图一重建就没了，等于没存。 */
    function commentsFor(book, entry) {
      var generation = contentGeneration;
      var ownerBox = personBox();
      var box = UI.h("details", { class: "np-comments" });
      box.appendChild(UI.h("summary", { text: "留言" }));
      var listBox = UI.h("div", { class: "np-comment-list" });
      var composerBox = UI.h("div", { class: "np-comment-composer" });
      box.appendChild(listBox);
      box.appendChild(composerBox);
      var items = [], loaded = false, busy = false, message = "", failed = false;
      /* 草稿从**按人 + 本 + 段**的存储里取：视图重建（切 tab 回来）、换个人再回来，
         之前敲的半句话都还在。 */
      var draftText = getCommentDraft(book, entry.seg_id);
      /* composer 是常驻的，只建一次；重建就会丢焦点和光标位置。 */
      var area = null, counter = null, saveBtn = null;
      function alive() {
        return !!holder && generation === contentGeneration && holder.contains(box);
      }
      function errorText(error) { return String((error && error.message) || error || "未知错误"); }
      function check(result) {
        if (!result || result.ok === false) throw new Error(String(result && result.error || "留言操作失败"));
        return result;
      }
      function ensureComposer() {
        if (area) return;
        area = UI.h("textarea", {
          /* ⚠️ maxlength 数的是 **UTF-16 码元**，emoji 占 2 个。写 300 会把
             300 个 emoji 拦在 150 个（用户反馈 2026-10-18）。属性只做宽松兜底
             （600 码元 ≈ 最坏 300 个代理对），真口径是下面的 Array.from 码点数。 */
          class: "np-editor-text np-comment-input", rows: "3", maxlength: "600", spellcheck: "false",
          "aria-label": "给这篇日记留言", placeholder: "给这篇日记留一句回应（最多 300 字）",
        });
        counter = UI.h("span", { class: "np-comment-note", text: "0/300 字" });
        saveBtn = UI.h("button", {
          class: "btn btn-sm", type: "button", text: "保存草稿",
          onclick: function () {
            if (busy || !alive()) return;
            draftText = area.value;
            var text = draftText.trim();
            if (!text || Array.from(text).length > 300) {
              failed = true; message = !text ? "留言不能为空。" : "每条留言最多 300 字。"; paint(); return;
            }
            return act("diaryCommentSave", { book: book, seg_id: entry.seg_id, text: text }, "草稿已保存；点“让她知道”才会交给她。", true);
          },
        });
        area.addEventListener("input", function () {
          area.value = Array.from(area.value).slice(0, 300).join("");
          draftText = area.value;
          /* 落盘到按人 + 本 + 段的草稿：视图重建 / 换人回来都还在。 */
          setCommentDraft(book, entry.seg_id, draftText);
          counter.textContent = Array.from(area.value).length + "/300 字";
        });
        composerBox.appendChild(area);
        composerBox.appendChild(UI.h("div", { class: "row-actions np-comment-actions" }, [saveBtn, counter]));
        composerBox.appendChild(UI.h("div", { class: "np-comment-note", text: "草稿只留在这里；发布后会在对应人的下一次私聊中交给她。" }));
      }
      function paint() {
        if (!alive()) return;
        UI.clear(listBox);
        if (message) listBox.appendChild(UI.h("div", { class: failed ? "err-box" : "np-comment-note", role: "status", text: message }));
        if (!loaded) {
          /* 没读到内容就不给输入框（也收掉半成品）：读失败了先重试，别在半瞎的状态下写留言。 */
          UI.clear(composerBox);
          area = null; counter = null; saveBtn = null;
          listBox.appendChild(UI.h("button", {
            class: "btn btn-sm", type: "button", text: busy ? "读取留言中…" : "重试读取留言", disabled: busy,
            onclick: function () { return load(); },
          }));
          return;
        }
        ensureComposer();
        if (!items.length) {
          listBox.appendChild(UI.h("div", { class: "muted", text: "还没有留言，写一句回应吧。" }));
        } else {
          items.filter(function (item) { return item.status !== "deleted"; }).forEach(function (item) {
            var labels = { draft: "草稿", pending: "待交给她", retired: "已交给她", withdrawn: "已撤回" };
            var label = item.status === "retired" && item.retired_reason === "source_changed" ? "来源失效" : labels[item.status] || "未知状态";
            var row = UI.h("div", { class: "np-comment-row" }, [
              UI.h("span", { class: "tag", text: label }),
              UI.h("p", { class: "np-comment-text", text: String(item.text || "") }),
            ]);
            var actions = UI.h("div", { class: "row-actions np-comment-actions" });
            function button(text, handler) {
              actions.appendChild(UI.h("button", { class: "btn btn-sm", type: "button", text: text, disabled: busy, onclick: handler }));
            }
            if (item.status === "draft" && item.source_valid === true && item.publishable === true) {
              button("让她知道", function () { return act("diaryCommentPublish", { id: item.id }, "已发布，等你们下次私聊时交给她。"); });
            }
            if (item.status === "pending") {
              button("撤回", function () { return act("diaryCommentWithdraw", { id: item.id }, "已撤回，尚未交付的留言不会再交给她。"); });
            }
            if (item.status !== "pending") {
              button(item.status === "draft" ? "编辑草稿" : "编辑为新草稿", function () {
                if (busy || !alive()) return;
                draftText = String(item.text || "");
                if (area) { area.value = draftText; counter.textContent = Array.from(draftText).length + "/300 字"; area.focus(); }
                paint();
              });
            }
            button("删除留言", function () { return act("diaryCommentDelete", { id: item.id }, "留言已删除；批注容量满时会清理已删除记录。"); });
            if (item.source_valid !== true) row.appendChild(UI.h("div", { class: "np-comment-note", text: "原日记已改写、删除或归属变化，不能发布此留言。" }));
            else if (item.status === "draft" && item.publishable !== true) row.appendChild(UI.h("div", { class: "np-comment-note", text: item.publish_reason || "这条日记没有明确的私聊归属，可保存草稿，暂不能交给她。" }));
            row.appendChild(actions);
            listBox.appendChild(row);
          });
        }
        /* 已有输入就**不动**它：写操作不该把用户正在打的字换掉 */
        if (area && draftText !== null && area.value !== draftText && !busy) {
          area.value = draftText;
          counter.textContent = Array.from(draftText).length + "/300 字";
        }
        area.disabled = busy;
        saveBtn.disabled = busy;
      }
      async function fetchItems() {
        var result = check(await ctx.apiGet("diaryComments", { book: book, seg_id: entry.seg_id }));
        if (!alive()) return;
        items = Array.isArray(result.items) ? result.items : [];
        loaded = true;
      }
      async function load() {
        if (busy || !alive()) return;
        busy = true; failed = false; message = ""; paint();
        try { await fetchItems(); }
        catch (error) { if (alive()) { failed = true; message = "读取留言失败：" + errorText(error); } }
        finally { if (alive()) { busy = false; paint(); } }
      }
      async function act(key, payload, success, resetDraft) {
        if (busy || !alive()) return;
        busy = true; failed = false; message = "正在保存…"; paint();
        var posted = false;
        var publishing = items.filter(function (item) { return item.id === payload.id; })[0];
        var clearInput = resetDraft || (key === "diaryCommentPublish" && publishing && area && area.value.trim() === String(publishing.text || "").trim());
        try {
          check(await ctx.apiPost(key, payload));
          posted = true;
          if (clearInput) delete ownerBox.comments[draftKey(book, entry.seg_id)];
          if (!alive()) return;
          if (clearInput) {
            draftText = "";
            if (area) area.value = "";
            if (counter) counter.textContent = "0/300 字";
          }
          message = success;
          /* 写接口不含实时来源权限，必须重读再决定是否给发布按钮。 */
          await fetchItems();
        } catch (error) {
          if (alive()) {
            failed = true;
            if (posted) loaded = false;
            message = (posted ? "操作已保存，但刷新留言失败，请重试读取：" : "留言操作失败：") + errorText(error);
          }
        } finally { if (alive()) { busy = false; paint(); } }
      }
      box.addEventListener("toggle", function () { if (box.open && !loaded) return load(); });
      return box;
    }

    /* ---- 单页日记本 ---- */
    function renderNotebook(data, st) {
      var wrap = UI.h("div", { class: "notebook" });
      var entries = Array.isArray(data.entries) ? data.entries : null;

      /* 老后端没有 entries：退回 text，一条都不能少 */
      if (!entries) {
        wrap.appendChild(UI.h("div", { class: "np-pagehead" }, [
          UI.h("span", { class: "np-datestamp", text: (currentBook === "love" ? "恋爱日记" : "日记") }),
        ]));
        wrap.appendChild(UI.h("pre", { class: "body", text: data.text || "（空）" }));
        return wrap;
      }
      if (!entries.length) {
        wrap.appendChild(UI.empty("这一段还没有内容 ✎"));
        return wrap;
      }
      /* 接口按时间正序返回；“最近若干条”从最新一条看起。按天阅读仍保持当天顺序。 */
      if (!st.date) entries = entries.slice().sort(function (a, b) {
        var left = String(a.date || "") + " " + String(a.time || "");
        var right = String(b.date || "") + " " + String(b.time || "");
        return left < right ? 1 : left > right ? -1 : 0;
      });

      /* 选中某天 = 那天全部顺次排在一页上；"最近若干条" = 一页放 1 条。 */
      var pageEntries, pageIndex = 0, totalPages = 1;
      if (st.date) {
        pageEntries = entries;
      } else {
        pageEntries = [entries[Math.min(st.page, entries.length - 1)]];
        totalPages = entries.length;
        pageIndex = Math.min(st.page, entries.length - 1);
      }

      var stamp = pageEntries[0] && pageEntries[0].date ? pageEntries[0].date : (st.date || "");
      st.shownDate = stamp;
      wrap.appendChild(UI.h("div", { class: "np-pagehead" }, [
        UI.h("span", { class: "np-datestamp", text: "— " + stamp + " " + weekdayCn(stamp) + " —" }),
        UI.h("span", { class: "np-count", text: "本次 " + (data.count || entries.length) + " 条 / 全书 " + (data.total || 0) + " 条" }),
      ]));
      entryNodes = {};
      pageEntries.forEach(function (entry) { wrap.appendChild(renderEntry(currentBook, entry, editWindow)); });
      wrap.appendChild(UI.h("div", { class: "np-foot", text: "· · ·" }));

      if (totalPages > 1) {
        /* 翻页只重画正文，日历和控制条不动 */
        wrap.appendChild(UI.h("div", { class: "np-pager" }, [
          UI.h("button", { class: "btn btn-sm", type: "button", text: "上一页", disabled: pageIndex <= 0,
            onclick: function () { st.page = Math.max(0, st.page - 1); paintContent(); } }),
          UI.h("span", { class: "np-pageno", text: (pageIndex + 1) + " / " + totalPages }),
          UI.h("button", { class: "btn btn-sm", type: "button", text: "下一页", disabled: pageIndex >= totalPages - 1,
            onclick: function () { st.page = Math.min(totalPages - 1, st.page + 1); paintContent(); } }),
        ]));
      }
      return wrap;
    }

    /* ---- 正文区 ----
       **常驻槽，顺序固定**：正文槽在 buildShell 里就建好并放在回收站**之前**，
       之后 paintContent 只往里换内容，从不把它摘下来重挂——重挂会让整块高度塌到 0
       再弹回来（闪一下），还容易把回收站顶到正文前面。

       等待期间**保留旧内容**：只在槽顶挂一条小状态（"读取中…"），
       旧日记留在屏幕上不塌陷。旧内容此时**不可操作**（np-busy 挡住点击），
       否则用户会点到一个已经过期的段上。 */
    var busyNode = null;

    function markBusy(on, text) {
      if (!contentSlot) return;
      contentSlot.className = "panel np-panel" + (on ? " np-busy" : "");
      contentSlot.inert = !!on;
      contentSlot.setAttribute("aria-busy", on ? "true" : "false");
      if (!on) { if (busyNode && busyNode.parentNode) busyNode.remove(); busyNode = null; return; }
      if (!busyNode) { busyNode = UI.h("div", { class: "np-busybar", role: "status", "aria-live": "polite" }); contentSlot.appendChild(busyNode); }
      busyNode.textContent = text || "读取中…";
    }
    function markIdle() {
      if (busyNode && busyNode.parentNode) busyNode.remove();
      busyNode = null;
      if (contentSlot) {
        contentSlot.className = "panel np-panel";
        contentSlot.inert = false;
        contentSlot.setAttribute("aria-busy", "false");
      }
    }

    async function paintContent() {
      if (!holder || !contentSlot) return;
      var generation = ++contentGeneration;
      /* 切模式 / 换本 / 换日期时，卡片那边的在途响应一并作废：
         否则"卡片"的晚响应会盖掉刚切回来的正文。 */
      cardGeneration += 1;
      cardSlot = null;
      var book = currentBook;
      var box = personBox();
      var st = per(book);
      markBusy(true, box.view === "card" ? "渲染日记本…" : "读取中…");

      if (box.view === "card") {
        await loadCard(book);
        return;
      }
      /* 等待期间**保留旧内容**：这里刻意不清槽。回包后下面才 UI.clear(contentSlot)
         一次性换掉，所以屏幕不会先塌到 0 高度再弹回来。 */
      try {
        var data = await ctx.apiGet("diaryContent", {
          book: book, date: st.date, tail: st.date ? 0 : box.tail, who: ctx.who(),
        });
        /* 旧响应绝不覆盖新选择：视图没了 / 读取过期了 / 期间换本了 */
        if (!holder || generation !== contentGeneration || book !== currentBook || !contentSlot) return;
        editWindow = data.edit_window || null;
        UI.clear(contentSlot);
        markIdle();
        contentSlot.appendChild(renderNotebook(data, st));
        if (st.date) {
          contentSlot.appendChild(UI.h("div", { class: "row-actions np-tools" }, [
            UI.h("button", { class: "btn btn-sm", type: "button", text: "看最近 " + box.tail + " 条",
              onclick: function () { st.date = ""; st.page = 0; renderCalendar(); paintContent(); } }),
          ]));
        }
      } catch (error) {
        if (!holder || generation !== contentGeneration || !contentSlot) return;
        UI.clear(contentSlot);
        markIdle();
        contentSlot.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    /* ---- 回收站（第 15 步）--------------------------------------------------
       **按需加载**：不再每次翻页/换日期/切本都去拉一遍。没展开过就只画一个
       安静的入口；只有第一次展开（或删完/还原完）才发 diary/trash。 */
    var trashLoaded = false;

    async function loadTrash() {
      if (!holder) return;
      var generation = ++trashGeneration;
      var box = personBox();
      box.trashOpen = true;        /* 展开态记下来：切 tab 回来仍是展开的 */
      var parent = trashSlot && trashSlot.parentNode ? trashSlot.parentNode : holder;
      var old = trashSlot;
      var slot = UI.h("div", { class: "panel np-panel" }, [UI.h("div", { class: "muted", text: "读取回收站…" })]);
      parent.appendChild(slot);
      if (old) old.remove();
      trashSlot = slot;
      try {
        var data = await ctx.apiGet("diaryTrash", {});
        if (!holder || generation !== trashGeneration || trashSlot !== slot) return;
        UI.clear(slot);
        var items = data.items || [];
        var head = UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: "🗑 回收站 · " + (data.count || 0) + " 条" }),
            UI.h("div", { class: "row-sub",
              text: "面板删掉的日记先落在这里（上限 " + (data.cap || 0) + " 条，超了丢最旧的）；还原会插回它原本的时间位置" }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: "收起",
              onclick: function () {
                /* 收起只改显示，**不再发请求**：数据已经在这儿了。 */
                box.trashOpen = false;
                paintTrashHead(false);
              },
            }),
          ]),
        ]);
        slot.appendChild(head);
        if (!items.length) slot.appendChild(UI.empty("回收站是空的"));
        else items.forEach(function (item) { slot.appendChild(trashRow(item)); });
        trashLoaded = true;
      } catch (error) {
        if (!holder || generation !== trashGeneration || trashSlot !== slot) return;
        UI.clear(slot);
        slot.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    /* 回收站收起态：只画标题 + 展开按钮，**不发请求**。 */
    function paintTrashHead(open) {
      if (!trashSlot) return;
      var box = personBox();
      box.trashOpen = !!open;
      UI.clear(trashSlot);
      trashSlot.appendChild(UI.h("div", { class: "row" }, [
        UI.h("div", { class: "row-main" }, [
          UI.h("div", { class: "row-title", text: "🗑 回收站" + (trashLoaded ? "" : "（点开才读）") }),
          UI.h("div", { class: "row-sub", text: "面板删掉的日记先进这里，可还原。点开才去读，不占翻页的等待。" }),
        ]),
        UI.h("div", { class: "row-actions" }, [
          UI.h("button", {
            class: "btn btn-sm", type: "button", text: "展开",
            /* 返回 promise：调用方（测试）要能 await 到"读完了"那一刻 */
            onclick: function () { return loadTrash(); },
          }),
        ]),
      ]));
    }

    function trashRow(item) {
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
              /* 还原完回收站内容变了：重拉一次（本来就已经展开着） */
              mutate("diaryRestore", { id: item.id }, "已还原到 " + item.date).then(loadTrash);
            },
          }),
        ]),
      ]);
    }

    /* ---- 组装外壳 ---- */
    function buildShell() {
      UI.clear(holder);
      headSlot = UI.h("div", { class: "np-headslot" });
      calSlot = UI.h("div", { class: "np-calwrap" });
      ctrlSlot = UI.h("div", { class: "np-ctrlslot" });
      /* 正文槽**常驻**：建一次就在回收站之前定好位置，之后只往里换内容。 */
      contentSlot = UI.h("div", { class: "panel np-panel" }, [UI.h("div", { class: "muted", text: "读取中…" })]);
      trashSlot = UI.h("div", { class: "panel np-panel np-trash" });
      holder.appendChild(headSlot);
      holder.appendChild(calSlot);
      holder.appendChild(ctrlSlot);
      holder.appendChild(contentSlot);   /* 正文在前 */
      holder.appendChild(trashSlot);      /* 回收站恒在最后 */
      busyNode = null;
      entryNodes = {};
    }

    async function refresh() {
      if (!holder) return;
      /* 同一个视图可能被连着 refresh 两次（顶栏刷新 / 换人）。每次 refresh 自己一份
         generation：第二次的 diaryList 先回来就得作废第一次那次，否则会用旧书列表
         覆盖新选择。 */
      var refreshGeneration = ++refreshCounter;
      var box = personBox();
      /* 图表指定的日期（overview 点某天跳过来）优先，并在读完就用掉。 */
      var jumpDate = ctx.state && ctx.state.diaryDate;
      var data = await ctx.apiGet("diaryList", { who: ctx.who() });
      if (!holder || refreshGeneration !== refreshCounter) return;
      books = data.books || [];
      buildShell();
      pickBook();
      if (jumpDate && parseYmd(jumpDate)) {
        var target = per(currentBook);
        target.date = jumpDate;
        target.month = jumpDate.slice(0, 7);
        box.cardDate = jumpDate;
        box.view = "text";
        delete ctx.state.diaryDate;
      }
      renderHead();
      renderCalendar();
      renderControls();
      await paintContent();
      /* 回收站默认收起，且**不发请求**；展开过 / 这次开着才读。 */
      trashLoaded = false;
      if (box.trashOpen) await loadTrash();
      else paintTrashHead(false);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () {
        /* 三条 generation 全部作废：在途响应回来时一律不画。 */
        contentGeneration += 1;
        trashGeneration += 1;
        cardGeneration += 1;
        refreshCounter += 1;
        holder = null;
        trashSlot = null; contentSlot = null; headSlot = null; calSlot = null; ctrlSlot = null;
        cardSlot = null;
        cardStage = null; cardFrame = null;
        entryNodes = {};
        /* 摘掉 resize 监听：不摘的话每切一次 tab 多挂一个。 */
        if (resizeBound) {
          resizeBound = false;
          window.removeEventListener("resize", onWindowResize);
        }
        if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = null; }
      },
    };
  };
})(window);
