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

    function toPoints(points, field, layer) {
      return (points || [])
        .filter(function (item) { return item.layer === layer; })
        .map(function (item) {
          var value = item[field];
          return {
            x: item.date,
            y: typeof value === "number" && isFinite(value) ? value : null,
            label: item.date,
            strong: (item.n || 1) > 1,
            title: (layer === LAYER_BASELINE ? "基调 " : "当下 ") + item.date +
              "　情绪 " + item.valence + " / 精神 " + item.arousal +
              (item.n > 1 ? "（当天 " + item.n + " 条，显示首条）" : "") +
              (typeof ctx.openDiaryDate === "function" ? " · 点击查看当天日记" : ""),
          };
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
        var now = toPoints(history.points, "valence", LAYER_NOW);
        var base = toPoints(history.points, "valence", LAYER_BASELINE);
        function openPoint(point) { ctx.openDiaryDate(point.label); }
        var onPointClick = typeof ctx.openDiaryDate === "function" ? openPoint : null;
        holder.appendChild(UI.lineChart({
          series: [
            { name: "当下情绪", points: now, color: "#e69bb0" },
            { name: "基调情绪", points: base, color: "#6d7787" },
          ],
          yMin: -2, yMax: 2, label: "当下情绪与基调情绪，范围负2至正2", onPointClick: onPointClick,
        }));
        holder.appendChild(UI.legend([
          { color: "#e69bb0", text: "当下情绪（" + now.length + " 点）" },
          { color: "#6d7787", text: "基调情绪（" + base.length + " 点）" },
        ]));
        holder.appendChild(UI.lineChart({
          series: [
            { name: "当下精神", points: toPoints(history.points, "arousal", LAYER_NOW), color: "#7fc8a9" },
            { name: "基调精神", points: toPoints(history.points, "arousal", LAYER_BASELINE), color: "#6d7787" },
          ],
          yMin: -2, yMax: 2, label: "当下精神与基调精神，范围负2至正2", onPointClick: onPointClick,
        }));
        holder.appendChild(UI.legend([
          { color: "#7fc8a9", text: "当下精神（唤醒度）" },
          { color: "#6d7787", text: "基调精神（唤醒度）" },
        ]));
        holder.appendChild(UI.h("div", { class: "sub",
          text: "共 " + now.length + " 个当下点、 " + base.length + " 个基调点 · 每日显示首条记录，空缺日期不补零" +
            (onPointClick ? " · 点击日期或数据点查看当天日记" : "") +
            (history.truncated ? "　·　超出窗口的点已折叠（truncated=true）" : "") }));
        if (!now.length && !base.length) holder.appendChild(UI.empty("这个窗口里还没有情绪记录"));
      }

      /* ---- 主动消息 ---- */
      holder.appendChild(UI.h("div", { class: "panel-title", style: "margin-top:22px", text: "主动消息" }));
      if (proactive && proactive.error) {
        holder.appendChild(UI.errorBox("主动消息：" + proactive.error));
        return;
      }
      if (!proactive) return;

      var diagnostic = proactive.diagnostic;
      var checkBox = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "最近一次巡检 / 发送结果" })]);
      if (!diagnostic) {
        checkBox.appendChild(UI.empty("还没有巡检结果；下一次巡检后显示"));
      } else {
        checkBox.appendChild(UI.h("div", { class: "row-title", text: diagnostic.reason || "没有说明" }));
        checkBox.appendChild(UI.h("div", { class: "row-sub", text: UI.shortTime(diagnostic.ts) + (diagnostic.umo ? " · " + diagnostic.umo : "") }));
        (diagnostic.sessions || []).forEach(function (item) {
          checkBox.appendChild(UI.kv(item.umo, item.reason));
        });
      }
      holder.appendChild(checkBox);

      var sessions = proactive.sessions || [];
      /* 时段槽位是后端给的英文枚举（private/group/night/idle/greeting/platform），
         直接显示就是一片英文。翻成中文，认不出来的键退回原样，别显示成空白。 */
      var SLOT_CN = {
        signal: "心情暗号", promise: "约定跟进", anniversary: "纪念日", care: "关怀",
        quiet: "好久没聊", goodnight: "晚安", diary: "日记",
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
        UI.h("h3", { text: "已发送的主动消息素材（倒序，共 " + (proactive.log_total || 0) + " 条）" }),
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
