/* 曲线 / 主动消息（三期）：手写 SVG 折线（valence 与 arousal）+
   主动消息记录（她什么时候主动说了什么）。

   曲线数据源是 state_history.jsonl（追加型，键名冻结 ts/layer/valence/arousal/word），
   页面在受限 iframe 里读不到文件系统——**GET /history 是曲线唯一入口**。 */
(function (global) {
  "use strict";

  var LAYER_NOW = "now";
  var LAYER_BASELINE = "baseline";

  global.createProactiveView = function createProactiveView(ctx) {
    var holder = null;
    var days = 30;

    function toPoints(points, field) {
      return (points || [])
        .filter(function (item) { return item.layer === LAYER_NOW; })
        .map(function (item) {
          return {
            y: Number(item[field]),
            label: item.date,
            strong: (item.n || 1) > 1,
            title: item.date + "　情绪 " + item.valence + " / 精神 " + item.arousal +
              (item.n > 1 ? "（当天 " + item.n + " 条）" : ""),
          };
        });
    }

    function toBaseline(points, field) {
      return (points || [])
        .filter(function (item) { return item.layer === LAYER_BASELINE; })
        .map(function (item) {
          return { y: Number(item[field]), label: item.date, strong: true, title: "基调 " + item.date };
        });
    }

    async function refresh() {
      var UI = ctx.UI;
      UI.clear(holder);

      var history = null;
      var proactive = null;
      try {
        history = await ctx.apiGet("history", { days: days });
      } catch (error) {
        history = { error: String((error && error.message) || error) };
      }
      try {
        proactive = await ctx.apiGet("proactive", {});
      } catch (error) {
        proactive = { error: String((error && error.message) || error) };
      }

      /* ---- 曲线 ---- */
      var title = UI.h("div", { class: "panel-title", text: "情绪曲线（近 " + days + " 天）" });
      var daySelect = UI.h("select", { class: "pill-select" }, [7, 30, 90, 180].map(function (n) {
        return UI.h("option", { value: String(n), text: "近 " + n + " 天", selected: n === days });
      }));
      daySelect.addEventListener("change", function () { days = Number(daySelect.value) || 30; refresh(); });
      holder.appendChild(UI.h("div", { class: "stage-tools", style: "justify-content:space-between" }, [title, daySelect]));

      if (history && history.error) {
        holder.appendChild(UI.errorBox("情绪历史：" + history.error));
      } else if (history) {
        var now = toPoints(history.points, "valence");
        var base = toBaseline(history.points, "valence");
        holder.appendChild(UI.lineChart({
          points: base.concat(now), color: "#e69bb0", area: "rgba(230,155,176,.10)",
          yMin: -1, yMax: 1,
        }));
        holder.appendChild(UI.legend([
          { color: "#e69bb0", text: "当下情绪（" + now.length + " 点）" },
          { color: "#6d7787", text: "基调情绪（" + base.length + " 点）" },
        ]));
        holder.appendChild(UI.lineChart({
          points: toPoints(history.points, "arousal"), color: "#7fc8a9", yMin: -1, yMax: 1,
        }));
        holder.appendChild(UI.legend([{ color: "#7fc8a9", text: "当下精神（唤醒度）" }]));
        holder.appendChild(UI.h("div", { class: "sub",
          text: "共 " + now.length + " 个当下点、 " + base.length + " 个基调点" +
            (history.truncated ? "　·　超出窗口的点已折叠（truncated=true）" : "") }));
        if (!now.length) holder.appendChild(UI.empty("这个窗口里还没有情绪记录"));
      }

      /* ---- 主动消息 ---- */
      holder.appendChild(UI.h("div", { class: "panel-title", style: "margin-top:22px", text: "主动消息" }));
      if (proactive && proactive.error) {
        holder.appendChild(UI.errorBox("主动消息：" + proactive.error));
        return;
      }
      if (!proactive) return;

      var sessions = proactive.sessions || [];
      /* 时段槽位是后端给的英文枚举（private/group/night/idle/greeting/platform），
         直接显示就是一片英文。翻成中文，认不出来的键退回原样，别显示成空白。 */
      var SLOT_CN = {
        private: "私聊", group: "群聊", night: "深夜", idle: "空闲",
        greeting: "打招呼", platform: "平台", morning: "早上", evening: "晚上",
      };
      var slotText = function (v) {
        var s = String(v === undefined || v === null || v === "" ? "—" : v);
        return SLOT_CN[s] || s;
      };
      var sBox = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "会话（今日计数）" }),
      ]);
      if (!sessions.length) sBox.appendChild(UI.empty("还没有主动消息记录"));
      sessions.forEach(function (item) {
        sBox.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: item.umo }),
            UI.h("div", { class: "row-sub",
              text: "最近 " + slotText(item.last_slot) + " · " +
                (item.last_sent_at ? UI.shortTime(item.last_sent_at) : "没发过") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("span", { class: "tag", text: "今日 " + (item.today_count || 0) }),
          ]),
        ]));
      });
      holder.appendChild(sBox);

      var log = proactive.log || [];
      var lBox = UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "她主动说了什么（倒序，共 " + (proactive.log_total || 0) + " 条）" }),
      ]);
      if (!log.length) lBox.appendChild(UI.empty("还没有发送记录"));
      log.forEach(function (item) {
        lBox.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: item.fragment || "（空）" }),
            UI.h("div", { class: "row-sub", text: UI.shortTime(item.ts) + " · " + slotText(item.slot) + " · " + item.umo }),
          ]),
        ]));
      });
      holder.appendChild(lBox);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
