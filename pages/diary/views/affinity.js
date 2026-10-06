/* 熟悉度：榜 + 分数 + 档位词 + last_ts + 当前 love_peers（**只显示**）。
   love_peers 是插件配置项，AstrBot 原生面板本来就能改，WebUI 不做第二个写入者。 */
(function (global) {
  "use strict";

  global.createAffinityView = function createAffinityView(ctx) {
    var holder = null;

    /* 熟悉度上限是 20，仪表条照实显示 n/20——不套 /100，那是在撒谎。 */
    function bar(score) {
      return ctx.UI.meter("分数", Number(score) || 0, "ok", null, 20);
    }

    async function refresh() {
      var UI = ctx.UI;
      UI.clear(holder);
      var data = await ctx.apiGet("affinity", {});
      var people = data.people || [];

      holder.appendChild(UI.h("div", { class: "grid" }, [
        UI.card(String(data.total_known || 0), "", "认识的人（落过盘）"),
        UI.card(String(people.length), "", "榜上有分的人（衰减后 > 0）"),
        UI.card(String((data.love_peers || []).length), "", "当前最亲密名单（只显示）"),
      ]));

      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "榜（分数降序，坐标已按半衰期衰减）" })]);
      if (!people.length) box.appendChild(UI.empty("还没有互动记录"));
      people.forEach(function (item) {
        box.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title",
              text: (item.name || item.id) + "　" + Number(item.score || 0).toFixed(2) }),
            UI.h("div", { class: "row-sub",
              text: "编号 " + item.id + " · " + (item.last_ts ? "最后互动 " + UI.shortTime(item.last_ts) : "无记录") }),
            bar(item.score),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("span", { class: "tag", text: item.band || "—" }),
          ]),
        ]));
      });
      holder.appendChild(box);

      var peers = data.love_peers || [];
      holder.appendChild(UI.h("div", { class: "card panel" }, [
        UI.h("h3", { text: "当前最亲密名单" }),
        UI.h("div", { class: "sub",
          text: peers.length ? peers.join("、") : "（空）" }),
        UI.h("div", { class: "sub",
          text: "这是插件配置项，请回 AstrBot 插件配置里改（这里只显示，不写）。" }),
      ]));
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
