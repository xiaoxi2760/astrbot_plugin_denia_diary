/* 日记（只读）：两本概览 + 选一本/一天看正文。
   恋爱日记默认收起（决策 #18）：列表只显示"恋爱日记 · N 篇 · 最近更新"。 */
(function (global) {
  "use strict";

  global.createDiaryView = function createDiaryView(ctx) {
    var holder = null;
    var state = { book: "normal", date: "", tail: 20, collapsed: {} };

    async function loadContent(book) {
      var UI = ctx.UI;
      var box = UI.h("div", { class: "panel" }, [UI.h("div", { class: "muted", text: "读取中…" })]);
      holder.appendChild(box);
      try {
        var data = await ctx.apiGet("diaryContent", { book: book, date: state.date, tail: state.tail });
        UI.clear(box);
        box.appendChild(UI.h("div", { class: "panel-title",
          text: (book === "love" ? "恋爱日记" : "日记") + " · 本次 " + data.count + " 条 / 全书 " + data.total + " 条" }));
        if (!data.count) { box.appendChild(UI.empty("这一段还没有内容")); return; }
        box.appendChild(UI.h("pre", { class: "body", text: data.text || "（空）" }));
      } catch (error) {
        UI.clear(box);
        box.appendChild(UI.errorBox(String(error && error.message ? error.message : error)));
      }
    }

    async function refresh() {
      var UI = ctx.UI;
      UI.clear(holder);
      var data = await ctx.apiGet("diaryList", {});
      var books = data.books || [];

      books.forEach(function (info) {
        var card = UI.h("div", { class: "card panel" });
        var collapsed = info.is_love ? state.collapsed[info.book] !== false : false;
        var open = !collapsed;
        card.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: info.display + " · " + info.entries + " 篇" }),
            UI.h("div", { class: "row-sub",
              text: UI.bytes(info.chars) + " 正文 · 最近更新 " + (info.latest_date || "—") +
                (info.updated_at ? " · " + UI.shortTime(info.updated_at) : "") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: open ? "收起" : "展开",
              onclick: function () {
                state.collapsed[info.book] = !open;
                refresh();
              },
            }),
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: "读最近 " + state.tail + " 条",
              onclick: function () {
                state.book = info.book; state.date = "";
                loadContent(info.book);
              },
            }),
          ]),
        ]));
        if (open) {
          var dayInput = UI.h("input", {
            class: "pill-select", type: "text", placeholder: "YYYY-MM-DD（留空=最近）",
            title: "按日期读：填 YYYY-MM-DD，留空读最近若干条",
            value: state.date, style: "margin-top:8px;min-width:200px",
          });
          var tailSelect = UI.h("select", { class: "pill-select", style: "margin-top:8px" },
            [5, 20, 50].map(function (n) {
              return UI.h("option", { value: String(n), text: "最近 " + n + " 条", selected: n === state.tail });
            }));
          var actions = UI.h("div", { class: "row-actions", style: "margin-top:8px" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: "读取",
              onclick: function () {
                state.book = info.book;
                state.date = dayInput.value.trim();
                state.tail = Number(tailSelect.value) || 20;
                loadContent(info.book);
              },
            }),
          ]);
          card.appendChild(dayInput);
          card.appendChild(tailSelect);
          card.appendChild(actions);
        }
        holder.appendChild(card);
      });

      if (!books.length) holder.appendChild(UI.empty("还没有日记文件"));
      await loadContent(state.book);
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
