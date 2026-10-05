/* 总览（达妮娅此刻）：**只管"一眼看完"**——心情 / 作息 / 基调 / 主动计数 / 对谁。
   「此刻的坐标」和「数据文件」两块纯数据搬去了新增的第 7 个 tab「数据」
   （views/data.js）。理由：总览是给人看她现在怎么样的，不是看数值的。
   数值不是不要看，是不该混在这一屏里。 */
(function (global) {
  "use strict";

  function coord(value) {
    var number = Number(value);
    if (isNaN(number)) return "—";
    return (number >= 0 ? "+" : "") + number.toFixed(2);
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

      var grid = UI.h("div", { class: "grid" }, [
        UI.card(mood.word || "（还没情绪）", mood.valence !== undefined ? coord(mood.valence) : "—",
          "此刻心情" + (mood.updated_at ? " · " + UI.shortTime(mood.updated_at) : "")),
        UI.card(rhythm.word || "（这个点没什么特别）", rhythm.late_night ? "深夜档" : "清醒",
          "作息" + (rhythm.late_night ? "（免打扰可能生效）" : "")),
        UI.card("基调", coord(base.valence), "达妮娅最近一直是" + (Number(base.valence) >= 0 ? "偏暖" : "偏冷") + "的"),
        UI.card("今日主动", String(today.total || 0), "共 " + (today.by_session || 0) + " 个会话 · " + (today.date || "")),
        UI.card("累计主动记录", String(data.log_total === undefined ? 0 : data.log_total), "确认发出去的条数"),
        UI.card("对谁", data.who_name || "（未选）", data.who || "面板按人组织"),
      ]);

      holder.appendChild(UI.h("div", { class: "panel-title", text: "一眼看完" }));
      holder.appendChild(grid);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
