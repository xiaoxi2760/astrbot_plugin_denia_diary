/* 数据：从「总览」里搬出来的两块纯数据（新增的第 7 个 tab）。

   为什么搬：总览是「她此刻」——一眼看完就够。valence / arousal / 子系统状态 /
   文件大小这些是**排障用的数字**，堆在那儿把"看一眼她的心情"这件小事变得费劲。
   两块都搬到这里，总览只留一眼看完。

   数据源与总览同一个 ``status`` 端点（**没有新增后端路由**）：
   - 此刻的坐标：valence / arousal / 基调两轴 / 子系统 / 版本 / 服务器时间
   - 数据文件：排障第一问——数据在哪、多大、什么时候写的

   这一块允许把 valence / 基调 valence 和总览卡片重复：
   在「她此刻」里那是冗余，在「数据」里那正是要看的东西。 */
(function (global) {
  "use strict";

  function coord(value) {
    var number = Number(value);
    if (isNaN(number)) return "—";
    return (number >= 0 ? "+" : "") + number.toFixed(2);
  }

  global.createDataView = function createDataView(ctx) {
    var holder = null;

    async function refresh() {
      var data = await ctx.apiGet("status", { who: ctx.who() });
      /* 顺手并一下候选集：直接先进「数据」tab 时，"对谁"下拉不该是空的 */
      ctx.mergeOptions(data.who_options);
      var UI = ctx.UI;
      UI.clear(holder);

      var mood = data.mood || {};
      var base = mood.baseline || {};
      var subs = data.subsystems || {};
      var subsText = Object.keys(subs).map(function (key) {
        return key + (subs[key] ? "✓" : "✕");
      }).join("  ");
      var subsBroken = Object.keys(subs).some(function (key) { return !subs[key]; });

      var coordCard = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "此刻的坐标" }),
        UI.kv("valence（暖↔冷）", coord(mood.valence)),
        UI.kv("arousal（激动↔安静）", coord(mood.arousal)),
        UI.kv("基调 valence", coord(base.valence)),
        UI.kv("基调 arousal", coord(base.arousal)),
        UI.kv("情绪自报于", UI.shortTime(mood.updated_at)),
        UI.kv("子系统", subsText, subsBroken ? "warn" : "ok"),
        UI.kv("插件版本", data.version || "—"),
        UI.kv("服务器时间", UI.shortTime(data.now)),
      ]);

      var fileCard = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "数据文件（排障第一问：数据在哪、多大）" }),
      ]);
      var files = data.files || {};
      var names = Object.keys(files);
      if (!names.length) {
        fileCard.appendChild(UI.empty("没有文件信息"));
      }
      names.forEach(function (name) {
        var info = files[name] || {};
        fileCard.appendChild(UI.kv(name,
          info.exists ? UI.bytes(info.bytes) + " · " + UI.shortTime(info.mtime) : "不存在",
          info.exists ? "" : "warn"));
      });

      holder.appendChild(UI.h("div", { class: "panel-title", text: "数值" }));
      holder.appendChild(UI.h("div", { class: "data-grid" }, [coordCard, fileCard]));
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
