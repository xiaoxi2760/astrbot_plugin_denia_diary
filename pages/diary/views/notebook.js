/* 小本本：事实与约定（读）+ 完成 / 删除（写，二期）。
   写走 store 的原子写，幂等；归属判定**不套聊天里的"只有私聊过的人才能改"**——
   面板是主人视角（裁定 §1.3#2），否则主人自己反而改不了。 */
(function (global) {
  "use strict";

  global.createNotebookView = function createNotebookView(ctx) {
    var holder = null;

    async function mutate(key, id) {
      try {
        var result = await ctx.apiPost(key, { id: id });
        if (result && result.ok) {
          ctx.toast("已" + (key === "notebookComplete" ? "标记完成" : "删除（进了回收站）"));
        } else {
          ctx.toast(String((result && result.error) || "操作失败"), true);
        }
      } catch (error) {
        ctx.toast(String((error && error.message) || error), true);
      }
      await refresh();
    }

    async function refresh() {
      var UI = ctx.UI;
      UI.clear(holder);
      var data = await ctx.apiGet("notebook", { who: ctx.who() });
      ctx.mergeOptions(data.who_options);
      var limits = data.limits || {};
      UI.clear(holder);

      holder.appendChild(UI.h("div", { class: "panel-title",
        text: "对谁：" + (data.who_name || data.who || "（全部）") + "　·　约定上限 " +
          (limits.promise_limit === undefined ? "—" : limits.promise_limit) +
          "　事实上限 " + (limits.fact_limit === undefined ? "—" : limits.fact_limit) }));

      var promises = data.promises || [];
      var pBox = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "约定" })]);
      if (!promises.length) pBox.appendChild(UI.empty("还没有约定"));
      promises.slice().reverse().forEach(function (item) {
        var done = !!item.done_at;
        pBox.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title" + (done ? " tag-done" : ""), text: item.text || "（空）" }),
            UI.h("div", { class: "row-sub",
              text: "id " + (item.id || "?") + " · " + (item.about || "?") +
                (done ? " · 已完成 " + UI.shortTime(item.done_at) : "") +
                (item.created_at ? " · 记于 " + UI.shortTime(item.created_at) : "") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            done ? null : UI.h("button", {
              class: "btn btn-sm", type: "button", text: "完成",
              onclick: function () { mutate("notebookComplete", item.id); },
            }),
            UI.h("button", {
              class: "btn btn-sm btn-danger", type: "button", text: "删除",
              onclick: function () { mutate("notebookDelete", item.id); },
            }),
          ]),
        ]));
      });
      holder.appendChild(pBox);

      var facts = data.facts || [];
      var fBox = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "事实" })]);
      if (!facts.length) fBox.appendChild(UI.empty("还没有事实"));
      facts.forEach(function (item) {
        fBox.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: item.text || "（空）" }),
            UI.h("div", { class: "row-sub",
              text: "id " + (item.id || "?") + (item.created_at ? " · " + UI.shortTime(item.created_at) : "") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm btn-danger", type: "button", text: "删除",
              onclick: function () { mutate("notebookDelete", item.id); },
            }),
          ]),
        ]));
      });
      holder.appendChild(fBox);
      holder.appendChild(UI.h("div", { class: "sub",
        text: "删除不是真消失：条目会进 notebook.json 的 trash（最多 50 条）。" }));
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
