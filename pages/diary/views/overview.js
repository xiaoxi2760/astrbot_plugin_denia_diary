/* 总览（此刻状态）：**只管"一眼看完"**——心情 / 作息 / 基调 / 主动计数 / 对谁。
   「此刻的坐标」和「数据文件」两块纯数据搬去了新增的第 7 个 tab「数据」
   （views/data.js）。理由：总览是给人看她现在怎么样的，不是看数值的。
   数值不是不要看，是不该混在这一屏里。

   顶上那块立绘**不在这个文件里**：它是 index.html 的静态节点（见那里的注释——
   必须让 AstrBot 在服务端重写相对路径并补上 asset_token，JS 动态建图拿不到）。
   这个视图只管下面那 6 张卡片。 */
(function (global) {
  "use strict";

  function coord(value) {
    var number = Number(value);
    if (isNaN(number)) return "—";
    return (number >= 0 ? "+" : "") + number.toFixed(2);
  }

  /* -2~+2 → 0~100，只给仪表条用。**这是映射不是百分比**，
     所以调用处必须在 axis 上标出 -2 / 0 / +2。 */
  function scaleCoord(value) {
    var n = Number(value);
    if (isNaN(n)) return 0;
    return Math.max(0, Math.min(100, (n + 2) / 4 * 100));
  }
  function toneCoord(value) {
    var n = Number(value);
    if (isNaN(n) || n === 0) return "zero";
    return n > 0 ? "ok" : "warn";
  }

  /* ---- 立绘：一次只展示一张，右边一个切换按钮 + 上传 / 删这张 ----

     图源**只有一种**：用户自己传的那张。第 10 步把打包的默认立绘删掉了——公开发布
     不夹带角色立绘，也不替用户决定放谁的照片，所以没有图时露 #portrait-empty 那块
     「这里可以放图」。

     ⚠️ 接口形状（第 11 步独立审查订正）：`GET portrait` 回的是
        `{current: {id, name, mime, bytes, created_at, data_url} | null, items: [元数据…]}`
     ——**current 是一个对象**（不是 id 字符串），**items 里没有 data_url**
     （test_webui_portrait.py:126 钉的就是这个：items 只有元数据）。
     所以画哪张就按 id 单独 `GET portrait?id=…` 取那张的 data_url，
     取过一次记进 `urls` 缓存（模块级，data_url 几百 KB，切 tab 不重拉）。
     上传 / 删除后作废缓存重取。 */

  var portrait = { items: [], index: 0, fromUpload: false, ready: false, urls: {} };
  var portraitWired = false;
  var deleteArmed = null;

  function portraitRefs() {
    return {
      strip: document.getElementById("portraits"),
      frame: document.getElementById("portrait-frame"),
      upload: document.getElementById("portrait-img"),
      empty: document.getElementById("portrait-empty"),
      count: document.getElementById("portrait-count"),
      switchBtn: document.getElementById("portrait-switch"),
      delBtn: document.getElementById("portrait-delete"),
      file: document.getElementById("portrait-file"),
    };
  }

  async function loadPortrait(ctx) {
    if (portrait.ready) return portrait;
    var items = [];
    try {
      var data = await ctx.apiGet("portrait");
      items = (data && data.items) || [];
      /* current 是**对象**（带 data_url），不是 id 字符串——拿它的 .id 去定位。
         万一老后端回的是字符串 id 也能认，两种都不炸。 */
      var cur = data && data.current;
      var currentId = (cur && typeof cur === "object") ? cur.id : cur;
      var at = -1;
      items.forEach(function (item, i) { if (item && item.id === currentId) at = i; });
      portrait.index = at >= 0 ? at : 0;
      portrait.fromUpload = items.length > 0;
      /* current 自带 data_url，直接进缓存，省一次单图请求 */
      if (cur && typeof cur === "object" && cur.id && cur.data_url) {
        portrait.urls[cur.id] = cur.data_url;
      }
    } catch (error) {
      /* 接口没接上（后端未实现 / 离线预览）：当作"一张都没有"，
         也就是「这里可以放图」那块占位——**不再退回打包的默认图**。 */
      items = [];
      portrait.index = 0;
      portrait.fromUpload = false;
    }
    portrait.items = items;
    portrait.ready = true;
    return portrait;
  }

  /* 当前这张的 data_url：三条来源依次取——
       1. 这一项自己就带 data_url（离线预览桩 / 老后端就是这形状）；
       2. 缓存里有（之前按 id 取过）；
       3. 没有就按 id 单取一次（真后端 items 只有元数据，只能这么拿）。
     取不到返回 ""，paintPortrait 会退到占位块，不会开天窗。 */
  async function urlOf(ctx, item) {
    if (!item) return "";
    if (item.data_url) return item.data_url;
    if (!item.id) return "";
    if (portrait.urls[item.id]) return portrait.urls[item.id];
    try {
      var one = await ctx.apiGet("portrait", { id: item.id });
      var url = one && one.item && one.item.data_url;
      if (url) {
        portrait.urls[item.id] = url;
        return url;
      }
    } catch (error) {
      /* 这张取不到（可能刚被别人删了）：下面按空处理 */
    }
    return "";
  }

  async function paintPortrait(ctx) {
    var refs = portraitRefs();
    if (!refs.strip) return;
    var total = portrait.items.length;
    if (total) portrait.index = ((portrait.index % total) + total) % total;
    var item = total ? portrait.items[portrait.index] : null;
    var url = portrait.fromUpload && item ? await urlOf(ctx, item) : "";

    /* 拿到 url 才亮图；拿不到就当这张没有——有图没占位、没图只有占位，两槽永远互斥 */
    if (url) {
      refs.upload.src = url;
      refs.upload.alt = item.name || "上传的立绘";
      refs.upload.hidden = false;
    } else {
      refs.upload.hidden = true;
      refs.upload.removeAttribute("src");
    }
    if (refs.empty) refs.empty.hidden = !!url;

    if (refs.count) refs.count.textContent = total > 1 ? (portrait.index + 1) + " / " + total : "";
    if (refs.switchBtn) refs.switchBtn.hidden = total <= 1;
    if (refs.delBtn) refs.delBtn.hidden = !portrait.fromUpload || !total;
    if (refs.frame) refs.frame.classList.toggle("is-empty", !url);
  }

  function wirePortrait(ctx) {
    if (portraitWired) return;
    var refs = portraitRefs();
    if (!refs.strip || !refs.switchBtn) return;
    portraitWired = true;

    refs.switchBtn.addEventListener("click", function () {
      if (portrait.items.length < 2) return;
      portrait.index = (portrait.index + 1) % portrait.items.length;
      paintPortrait(ctx);
      /* 把选择记回后端，刷新后还停在同一张。存的是哪张与界面无关，失败也别打断切换。 */
      var item = portrait.items[portrait.index];
      if (portrait.fromUpload && item && item.id) {
        ctx.apiPost("portraitSelect", { id: item.id }).catch(function () { });
      }
    });

    refs.file.addEventListener("change", async function (event) {
      var file = event.target.files && event.target.files[0];
      event.target.value = ""; /* 同一张图再选一次也得能触发 change */
      if (!file) return;
      try {
        ctx.toast("正在上传…");
        await ctx.apiUpload("portraitUpload", file);
        portrait.ready = false;
        portrait.urls = {};          /* 缓存里有作废的 id，必须一起清 */
        await loadPortrait(ctx);
        paintPortrait(ctx);
        ctx.toast("传好了");
      } catch (error) {
        ctx.toast(String((error && error.message) || error || "上传失败"), true);
      }
    });

    /* 受限 iframe 的 sandbox 是 allow-scripts allow-forms allow-downloads，
       **没有 allow-modals**，window.confirm 会被直接拦掉。
       所以删除走两步：先点「删这张」把它变成「真删？」，再点一次才动手。 */
    refs.delBtn.addEventListener("click", async function () {
      if (deleteArmed !== portrait.index) {
        deleteArmed = portrait.index;
        refs.delBtn.textContent = "真删？再点一次";
        refs.delBtn.classList.add("is-danger");
        global.setTimeout(function () {
          deleteArmed = null;
          refs.delBtn.textContent = "删这张";
          refs.delBtn.classList.remove("is-danger");
        }, 4000);
        return;
      }
      var item = portrait.items[portrait.index];
      if (!item || !item.id) return;
      deleteArmed = null;
      try {
        await ctx.apiPost("portraitDelete", { id: item.id });
        portrait.ready = false;
        portrait.urls = {};          /* 同上：删掉的 id 别留在缓存里 */
        await loadPortrait(ctx);
        paintPortrait(ctx);
        ctx.toast("删了");
      } catch (error) {
        ctx.toast(String((error && error.message) || error || "删除失败"), true);
      }
    });
  }

  global.createOverviewView = function createOverviewView(ctx) {
    var holder = null;

    async function refresh() {
      var data = await ctx.apiGet("status", { who: ctx.who() });
      ctx.mergeOptions(data.who_options);
      var UI = ctx.UI;
      UI.clear(holder);

      var mood = data.mood || {};
      var base = mood.baseline || {};
      var rhythm = data.rhythm || {};
      var today = data.today || {};

      /* bento：心情/作息两卡放宽（span 2），计数与小卡用常规宽度。
         不新增任何数据请求，只用 status 已经返回的东西。 */
      /* 卡片主次（第 9 步）：**大字给中文**，数字降级到副行。
         数值一个都不许丢——只是从"大字"挪到"小字"，我们对外说过"数值可见但不进提示词"。 */
      var signCoord = function (v) {
        if (v === undefined || v === null || v === "") return "—";
        var n = Number(v);
        return (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(2);
      };
      /* 「对谁」卡片读**前端选中态**（ctx.who + 已合并的候选），不读回包的
         who_name 回显：首屏取数发生在 applyWhoDefault 落定之前（who 还是空串
         就发请求了），回显会让默认主人被"（未选）"顶掉——真机反馈 2026-10-07。
         选中态才是这张卡的真相；回包 who_name 与它恒等（同一个 ctx.who 发的请求）。
         大字只给**名字**，号码归副行——大字"希(508416913)"副行再"508416913"
         是同一个号码说两遍（真机反馈 2026-10-07）。没名字时大字用 id，
         副行就让回"面板按人组织"，别再重复一遍。 */
      var whoId = ctx.who();
      var whoHit = whoId ? ctx.state.options.filter(function (one) { return one.id === whoId; })[0] : null;
      var whoName = (whoHit && whoHit.name) || "";
      var whoTitle = whoId ? (whoName || whoId) : "（未选）";
      var whoSub = whoId && whoName ? whoId : "面板按人组织";
      var wide = function (el) { el.className += " bento-wide"; return el; };
      var grid = UI.h("div", { class: "grid bento" }, [
        wide(UI.card("此刻心情", mood.word || "（还没情绪）",
          signCoord(mood.valence) + (mood.updated_at ? " · " + UI.shortTime(mood.updated_at) : ""),
          "card-mood")),
        wide(UI.card(rhythm.word || "（这个点没什么特别）", rhythm.late_night ? "深夜档" : "清醒",
          "作息" + (rhythm.late_night ? "（免打扰可能生效）" : ""))),
        UI.card("基调", "最近一直是" + (Number(base.valence) >= 0 ? "偏暖" : "偏冷") + "的", signCoord(base.valence)),
        UI.card("今日主动", String(today.total || 0), "共 " + (today.by_session || 0) + " 个会话 · " + (today.date || "")),
        UI.card("累计主动记录", String(data.log_total === undefined ? 0 : data.log_total), "确认发出去的条数"),
        UI.card("对谁", whoTitle, whoSub),
      ]);

      /* 仪表条是**加**不是**替**：上面的原文字一个字没删。
         valence/arousal 是 -2~+2，映射到 0~100 属于前端展示，
         所以轴上标出 -2 / 0 / +2，不许装成百分比。 */
      holder.appendChild(UI.h("div", { class: "panel-title", text: "一眼看完" }));
      holder.appendChild(UI.h("div", { class: "ov-meters" }, [
        UI.meter("此刻情绪", scaleCoord(mood.valence), toneCoord(mood.valence), ["-2", "0", "+2"]),
        UI.meter("此刻精神", scaleCoord(mood.arousal), toneCoord(mood.arousal), ["-2", "0", "+2"]),
      ]));
      holder.appendChild(grid);

      /* 立绘区是 stage-body **外面**的静态节点（UI.clear 清不到它），单独画。
         放最后面：拉立绘要传 data_url，别让上面这 6 张卡片等着它。 */
      wirePortrait(ctx);
      await loadPortrait(ctx);
      await paintPortrait(ctx);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
