/* 总览（达妮娅此刻）：情绪 + 基调 + 作息词 + 今日主动计数 + 版本 + 文件体检。 */
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
      var subs = data.subsystems || {};
      var subsText = Object.keys(subs).map(function (key) { return key + (subs[key] ? "✓" : "✕"); }).join("  ");

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

      var detail = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "此刻的坐标" }),
        UI.kv("valence（暖↔冷）", coord(mood.valence)),
        UI.kv("arousal（激动↔安静）", coord(mood.arousal)),
        UI.kv("基调 valence", coord(base.valence)),
        UI.kv("基调 arousal", coord(base.arousal)),
        UI.kv("子系统", subsText, Object.keys(subs).some(function (k) { return !subs[k]; }) ? "warn" : "ok"),
        UI.kv("插件版本", data.version || "—"),
        UI.kv("服务器时间", UI.shortTime(data.now)),
      ]);

      var files = data.files || {};
      var fileRows = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "数据文件（排障第一问：数据在哪、多大）" }),
      ]);
      Object.keys(files).forEach(function (name) {
        var info = files[name] || {};
        fileRows.appendChild(UI.kv(name,
          info.exists ? UI.bytes(info.bytes) + " · " + UI.shortTime(info.mtime) : "不存在",
          info.exists ? "" : "warn"));
      });

      holder.appendChild(UI.h("div", { class: "panel-title", text: "一眼看完" }));
      holder.appendChild(grid);
      holder.appendChild(detail);
      holder.appendChild(fileRows);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
