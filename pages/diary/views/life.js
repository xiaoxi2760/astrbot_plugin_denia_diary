/* 今天：她自己的公开日常 —— 一天的计划 + 推进记录。
   生活计划是**全局公开**的，不按 who 分裂：面板里看到的都是「她」的一天。
   只做面板（前端）这一层：读用 GET life，写用 lifeGenerate / lifeReplace / lifeRecord。

   几条硬规矩（别改）：
   - GET 是只读的：refresh() 只读，绝不 POST，也绝不自动生成计划。
   - 写操作保留旧内容直到新请求成功；失败不白屏、不覆盖用户正在填的东西。
   - 时段结束 ≠ 完成现实操作：elapsed 只说明钟表走过了，完成要靠「确认完成 / 跳过」。
   - 未来计划是公开日常，别把用户的私事写进去。
*/
(function (global) {
  "use strict";

  var LABEL = {
    planned: "待开始",
    current: "当前",
    elapsed: "时段已过",
    done: "已确认",
    skipped: "已跳过",
  };
  var NOTE = {
    planned: "还没开始",
    current: "正在进行",
    elapsed: "时段过了 ≠ 做完了",
    done: "已确认完成",
    skipped: "已跳过",
  };
  var GEN_LABEL = {
    idle: "还没生成",
    pending: "排队中",
    running: "生成中",
    ok: "已生成",
    success: "已生成",
    ready: "已安排",
    generating: "正在安排",
    disabled: "已关闭",
    failed: "生成失败",
    error: "生成失败",
  };

  /* —— 模块级草稿 ——
     生活计划是全局公开的，不按 who 分裂；所以草稿也放模块级：
     切页、换人回来，未保存的编辑还在原地。保存成功才清。 */
  var planDraft = null;   /* { date, timezone, rows: [{start,end,title,location}] } */
  var outcomeDraft = {};  /* activity_id -> 文本（≤160 字） */
  var outcomeRevisions = {};
  var editing = false;    /* 未来计划编辑器是否展开 */
  var recordOpen = null;  /* 正在写结果的那一项 id */

  global.createLifeView = function createLifeView(ctx) {
    var holder = null;
    var gen = 0;            /* 生命周期代次：卸载后旧响应一律丢弃 */
    var mutationSeq = 0;    /* 写操作计数：GET 回来晚于写操作时不许覆盖 */
    var readSeq = 0;
    var busy = false;
    var snapshot = null;
    var loadError = "";
    var notice = null;      /* { kind:"error"|"warn"|"info", text } */
    var busyButtons = [];
    var targetDate = "";
    var targetTz = "";
    var refreshTimer = null;

    function alive(token) { return token === gen && !!holder; }

    function notify(kind, text) {
      notice = text ? { kind: kind, text: String(text) } : null;
    }

    function toast(text, isErr) {
      var fn = ctx.toast || (ctx.UI && ctx.UI.toast);
      if (typeof fn === "function") fn(text, isErr);
    }

    /* ---------------- 数据读取（只读） ---------------- */

    function revision() {
      if (!snapshot || snapshot.revision === undefined || snapshot.revision === null) return null;
      return snapshot.revision;
    }
    function planDate() { return (snapshot && snapshot.date) || targetDate || ""; }
    function planTz() { return (snapshot && snapshot.timezone) || targetTz || ""; }
    function activities() { return (snapshot && snapshot.activities) || []; }

    function sorted() {
      return activities()
        .map(function (a, i) { return { a: a, i: i }; })
        .sort(function (x, y) {
          var sx = String((x.a && x.a.start) || ""), sy = String((y.a && y.a.start) || "");
          if (sx === sy) return x.i - y.i;
          return sx < sy ? -1 : 1;
        })
        .map(function (x) { return x.a; });
    }

    /* 还没开始的才算「未来」，可以编辑；过去的不许改，也不许被删。 */
    function isFuture(a) { return String((a && a.status) || "planned") === "planned"; }
    /* 当前和过去的都能写结果 */
    function canRecord(a) {
      var st = String((a && a.status) || "");
      return st === "current" || st === "elapsed";
    }

    function fmtTime(value) {
      var text = String(value === undefined || value === null ? "" : value).trim();
      if (!text) return "";
      var m = text.match(/(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
      if (m) return m[2];
      m = text.match(/(\d{2}:\d{2})/);
      if (m) return m[1];
      return text;
    }
    function timeRange(a) {
      var s = fmtTime(a && a.start), e = fmtTime(a && a.end);
      if (s && e) return s + "–" + e;
      if (s) return s + " 起";
      if (e) return "到 " + e;
      return "时间待定";
    }

    function outcomeText(id) {
      if (Object.prototype.hasOwnProperty.call(outcomeDraft, id)) return outcomeDraft[id];
      return "";
    }

    async function refresh() {
      var token = gen;
      var requestId = ++readSeq;
      var mark = mutationSeq;
      var data = null;
      var errorText = "";
      try {
        data = await ctx.apiGet("life");
      } catch (error) {
        errorText = String((error && error.message) || error || "读取失败");
      }
      if (!alive(token)) return;                 /* 已卸载：旧返回直接丢 */
      if (requestId !== readSeq) return;
      if (mutationSeq !== mark) return;          /* 期间有写操作落地，别用旧 GET 盖掉 */
      loadError = errorText;
      if (data && data.ok !== false) {
        snapshot = data;
        if (!targetDate) targetDate = String(data.date || "");
        if (!targetTz) targetTz = String(data.timezone || "");
      } else if (!data) {
        notify("error", "读取失败：" + loadError);
      } else {
        if (!snapshot) snapshot = data;
        notify("error", String(data.error || (data.generation && data.generation.error) || "读取失败"));
      }
      render();
    }

    /* ---------------- 写操作 ---------------- */

    function applyOk(res, opts) {
      opts = opts || {};
      mutationSeq++;
      if (res.snapshot) snapshot = res.snapshot;
      if (opts.clearOutcome) { delete outcomeDraft[opts.clearOutcome]; delete outcomeRevisions[opts.clearOutcome]; }
      if (opts.clearDraft) { planDraft = null; editing = false; recordOpen = null; }
      notify("info", opts.text || "已保存");
      render();
    }

    function applyFail(res, fallback) {
      var msg = (res && res.error) ? String(res.error) : String(fallback || "操作失败");
      /* 失败也可能带回服务器更新的快照——它比在途的旧 GET 新，所以要盖住它。 */
      if (res && res.snapshot) { snapshot = res.snapshot; mutationSeq++; }
      if (res && res.conflict) {
        notify("warn", "安排已变化，草稿仍保留。检查最新时间线，再选择是否基于新版继续编辑。");
      } else {
        notify("error", msg);
      }
      render();
    }

    async function doGenerate() {
      if (busy) return;
      if (planDraft) { notify("warn", "先保存或取消草稿，再重排未来。"); render(); return; }
      busy = true; setBusy();
      var token = gen;
      var res = null, err = "";
      try {
        res = await ctx.apiPost("lifeGenerate", {
          date: planDate(),
          timezone: planTz(),
          expected_revision: revision(),
        });
      } catch (error) {
        err = String((error && error.message) || error || "生成失败");
      }
      if (!alive(token)) return;
      busy = false;
      if (err) { notify("error", err); render(); return; }
      if (res && res.ok) {
        applyOk(res, { text: "今天的未来安排已更新" });
        if (snapshot && snapshot.date) targetDate = String(snapshot.date);
        if (snapshot && snapshot.timezone) targetTz = String(snapshot.timezone);
        return;
      }
      applyFail(res, "生成失败");
    }

    async function doReplace() {
      if (busy) return;
      var d = ensureDraft();
      var rows = d.rows.map(function (r) {
        return { start: r.start || "", end: r.end || "", title: r.title || "", location: r.location || "" };
      });
      for (var i = 0; i < rows.length; i++) {
        if (!String(rows[i].title || "").trim()) {
          notify("error", "第 " + (i + 1) + " 行还没写标题，补上再保存。");
          render();
          return;
        }
      }
      busy = true; setBusy();
      var token = gen;
      var res = null, err = "";
      try {
        res = await ctx.apiPost("lifeReplace", {
          date: d.date || planDate(),
          timezone: d.timezone || planTz(),
          expected_revision: d.revision,
          activities: rows,
        });
      } catch (error) {
        err = String((error && error.message) || error || "保存失败");
      }
      if (!alive(token)) return;
      busy = false;
      if (err) { notify("error", err); render(); return; }
      if (res && res.ok) { applyOk(res, { clearDraft: true, text: "未来安排已保存" }); return; }
      applyFail(res, "保存失败");
    }

    async function doRecord(act, status) {
      if (busy) return;
      busy = true; setBusy();
      var token = gen;
      var res = null, err = "";
      try {
        res = await ctx.apiPost("lifeRecord", {
          date: planDate(),
          timezone: planTz(),
          expected_revision: outcomeRevisions[act.id],
          activity_id: act.id,
          status: status,
          outcome: outcomeText(act.id).slice(0, 160),
        });
      } catch (error) {
        err = String((error && error.message) || error || "记录失败");
      }
      if (!alive(token)) return;
      busy = false;
      if (err) { notify("error", err); render(); return; }
      if (res && res.ok) {
        recordOpen = null;
        applyOk(res, {
          clearOutcome: act.id,
          text: status === "done" ? "结果已记录" : "已跳过",
        });
        return;
      }
      applyFail(res, "记录失败");
    }

    function ensureDraft() {
      if (!planDraft) {
        planDraft = {
          date: planDate(),
          timezone: planTz(),
          revision: revision(),
          rows: sorted().filter(isFuture).map(function (a) {
            return {
              start: a.start || "", end: a.end || "",
              title: a.title || "", location: a.location || "",
            };
          }),
        };
      }
      return planDraft;
    }

    function cancelEdit() {
      planDraft = null;
      editing = false;
      recordOpen = null;
      render();
      toast("已取消编辑（草稿已还原）");
    }

    /* ---------------- 渲染 ---------------- */

    function actButton(text, act, attrs, onClick) {
      var a = attrs || {};
      var merged = {
        class: "btn btn-sm " + (a.cls || ""),
        type: "button",
        "data-act": act,
        onclick: onClick,
      };
      if (a.title) merged.title = a.title;
      if (a.aria) merged["aria-label"] = a.aria;
      var unavailable = a.disabled || ((!snapshot || snapshot.enabled === false || snapshot.ok === false) && act !== "refresh" && act !== "cancel-edit");
      if (unavailable) merged.disabled = true;
      var el = ctx.UI.h("button", merged, text);
      if (!unavailable) busyButtons.push(el);
      return el;
    }

    function setBusy() {
      busyButtons.forEach(function (el) {
        try { el.disabled = busy; } catch (error) { /* 元素已移除，忽略 */ }
      });
    }

    function banner() {
      var UI = ctx.UI;
      var enabled = snapshot ? snapshot.enabled !== false : true;
      var kids = [
        UI.h("div", { class: "sub", text: "她自己的公开日常，也会成为聊天素材。时段结束只表示时间已过，记录结果后才算已确认。" }),
      ];
      if (!enabled) {
        kids.push(UI.h("div", { class: "sub", style: "color:var(--err);", text: "生活规划当前是禁用状态：下面的计划仍是旧的，不能重新排。" }));
      }
      return UI.h("div", {
        class: "life-note",
        role: "note",
        "aria-label": "生活计划说明",
      }, kids);
    }

    function noticeBox() {
      var UI = ctx.UI;
      if (!notice) return null;
      var style = notice.kind === "error"
        ? "border-color:var(--err);color:var(--err);"
        : notice.kind === "warn"
          ? "border-color:var(--accent-line);"
          : "border-color:var(--accent-line);color:var(--fg-dim);";
      return UI.h("div", {
        class: "err-box",
        style: style,
        role: "status",
        "data-kind": notice.kind,
        text: notice.text,
      });
    }

    function statusCard() {
      var UI = ctx.UI;
      var gen2 = (snapshot && snapshot.generation) || {};
      var box = UI.h("div", { class: "life-header" });
      if (!snapshot) {
        box.appendChild(UI.empty("正在读今天的安排…"));
        return box;
      }
      box.appendChild(UI.h("h2", { text: snapshot.date || "今天" }));
      var stateText = snapshot.enabled === false ? "已关闭" : (GEN_LABEL[String(gen2.status || "")] || "等待安排");
      box.appendChild(UI.h("div", { class: "sub", text: stateText + " · " + (snapshot.timezone || "") +
        " · 今日规划 " + (gen2.attempts || 0) + "/" + (gen2.max_attempts || "—") + " 次" }));
      if (gen2.next_retry_at) {
        box.appendChild(UI.kv("下次可重试", UI.shortTime(gen2.next_retry_at)));
      }
      if (gen2.error) {
        box.appendChild(UI.h("div", {
          class: "err-box", role: "status", "data-act": "generation-error",
          text: String(gen2.error),
        }));
      }
      return box;
    }

    function actionBar() {
      var UI = ctx.UI;
      var gen2 = (snapshot && snapshot.generation) || {};
      var running = /^(running|generating)$/.test(String(gen2.status || ""));
      var disabled = snapshot ? snapshot.enabled === false : false;
      var hasPlan = activities().length > 0;
      var box = UI.h("div", { class: "life-actions" });

      var buttons = [
        actButton(
          hasPlan ? "重排未来" : "模型规划",
          "generate",
          { cls: "btn-primary", title: planDraft ? "先保存或取消草稿" : "让她重新安排未来活动", aria: hasPlan ? "重排未来" : "模型规划", disabled: disabled || running || !!planDraft || !snapshot || gen2.attempts >= gen2.max_attempts },
          function () { doGenerate(); }
        ),
      ];
      if (!editing) {
        buttons.push(actButton("编辑未来", "open-editor", { title: "手动排未来", aria: "编辑未来" }, function () {
          ensureDraft(); editing = true; render();
        }));
      }
      box.appendChild(UI.h("div", { class: "row-actions" }, buttons));
      if (disabled) box.appendChild(UI.h("div", { class: "sub", text: "功能被禁用：按钮不可用，等设置里打开再来。" }));
      if (running) box.appendChild(UI.h("div", { class: "sub", text: "模型正在生成，等它自己跑完。" }));
      return box;
    }

    function nowCards() {
      var UI = ctx.UI;
      var cur = snapshot && snapshot.current;
      var next = snapshot && snapshot.next;
      var row = UI.h("div", { class: "life-now" }, [
        UI.h("div", { class: "row-main" }, [
          UI.card("当前", cur ? (cur.title || "（未命名）") : "—",
            cur ? (timeRange(cur) + (cur.location ? " · " + cur.location : "")) : "此刻没有正在进行的事"),
        ]),
        UI.h("div", { class: "row-main" }, [
          UI.card("下一项", next ? (next.title || "（未命名）") : "—",
            next ? (timeRange(next) + (next.location ? " · " + next.location : "")) : "后面没有安排了"),
        ]),
      ]);
      return row;
    }

    function statusTag(a) {
      var st = String((a && a.status) || "planned");
      var style = st === "current"
        ? "color:var(--accent);border-color:var(--accent-line);"
        : st === "elapsed" ? "color:var(--fg-dim);" : "";
      return ctx.UI.h("span", { class: "tag", style: style, "data-status": st, text: LABEL[st] || st });
    }

    function activityRow(a) {
      var UI = ctx.UI;
      var st = String((a && a.status) || "planned");
      var title = a.title || "（未命名）";
      var actions = [];

      if (canRecord(a)) {
        actions.push(actButton(
          recordOpen === a.id ? "收起" : "记录结果",
          "record-open", { title: "写一句结果并确认完成或跳过", aria: "记录结果：" + title },
          function () {
            recordOpen = recordOpen === a.id ? null : a.id;
            if (recordOpen && outcomeRevisions[a.id] === undefined) outcomeRevisions[a.id] = revision();
            render();
          }
        ));
      }
      if (isFuture(a) && !editing) {
        actions.push(actButton("编辑", "edit-future", { title: "编辑未来的这一项", aria: "编辑：" + title }, function () {
          ensureDraft(); editing = true; render();
        }));
      }

      var main = [
        UI.h("div", { class: "row-title" + (st === "done" ? " tag-done" : ""), text: title }),
        UI.h("div", { class: "row-sub", text: timeRange(a) + (a.location ? " · " + a.location : "") }),
        UI.h("div", { class: "row-sub" }, [statusTag(a)]),
      ];
      if (a.outcome) {
        main.push(UI.h("div", { class: "row-sub", text: "结果：" + a.outcome }));
      }

      var row = UI.h("div", { class: "row", "data-activity-id": String(a.id || "") }, [
        UI.h("div", { class: "row-main" }, main),
        UI.h("div", { class: "row-actions" }, actions),
      ]);
      if (recordOpen === a.id) row.appendChild(recordEditor(a));
      return row;
    }

    function recordEditor(a) {
      var UI = ctx.UI;
      var id = "life-outcome-" + String(a.id || "");
      var box = UI.h("div", { class: "row", style: "border-top:1px dashed var(--grid);", "data-act": "record-editor" });
      var ta = UI.h("textarea", {
        id: id, rows: 2, maxlength: 160,
        class: "pill-select",
        style: "width:100%;min-height:56px;resize:vertical;",
        placeholder: "写一句结果：她这边到底做没做、做到哪一步（最多 160 字）",
        "aria-label": "结果说明（最多 160 字）",
        oninput: function (e) { outcomeDraft[a.id] = e.target.value; },
      }, outcomeText(a.id));

      box.appendChild(UI.h("label", { class: "row-sub", for: id, text: "结果（最多 160 字）" }));
      box.appendChild(ta);
      box.appendChild(UI.h("div", { class: "row-actions" }, [
        actButton("确认结果", "record-done", { cls: "btn-primary", title: "记录这段角色日常的结果", aria: "确认结果：" + (a.title || "") }, function () {
          doRecord(a, "done");
        }),
        actButton("跳过", "record-skip", { title: "跳过这一项", aria: "跳过：" + (a.title || "") }, function () {
          doRecord(a, "skipped");
        }),
      ]));
      if (outcomeRevisions[a.id] !== revision()) {
        box.appendChild(actButton("基于新版继续记录", "rebase-record", { aria: "基于新版继续记录" }, function () {
          outcomeRevisions[a.id] = revision(); render();
        }));
      }
      return box;
    }

    function timeline() {
      var UI = ctx.UI;
      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "整日时间线" })]);
      var list = sorted();
      if (!list.length) {
        box.appendChild(UI.empty("还没有安排。让模型排一天，或者自己加几行。"));
        return box;
      }
      list.forEach(function (a) { box.appendChild(activityRow(a)); });
      return box;
    }

    function editorRow(r, idx, d) {
      var UI = ctx.UI;
      function field(key, label, placeholder) {
        var fid = "life-plan-" + key + "-" + idx;
        var input = UI.h("input", {
          id: fid, type: "text", class: "pill-select",
          value: r[key] || "",
          maxlength: key === "title" || key === "location" ? 60 : 5,
          placeholder: placeholder,
          "aria-label": label + "（第 " + (idx + 1) + " 行）",
          oninput: function (e) { r[key] = e.target.value; },
        });
        return UI.h("div", {}, [
          UI.h("label", { class: "row-sub", for: fid, text: label }),
          input,
        ]);
      }
      return UI.h("div", {
        class: "row",
        style: "display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-items:end;",
        "data-act": "plan-row",
      }, [
        field("start", "开始", "09:00"),
        field("end", "结束", "10:30"),
        field("title", "标题", "做什么"),
        field("location", "地点", "在哪（可留空）"),
        UI.h("div", { class: "row-actions" }, [
          actButton("删除这一行", "del-row", { cls: "btn-danger", title: "删掉这一行", aria: "删除第 " + (idx + 1) + " 行" }, function () {
            d.rows.splice(idx, 1);
            render();
          }),
        ]),
      ]);
    }

    function editor() {
      var UI = ctx.UI;
      var d = ensureDraft();
      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "编辑未来安排" })]);
      box.appendChild(UI.h("div", {
        class: "sub",
        text: "只改还没开始的时段；已经过去或已确认的记录不受影响。这里写的是她自己的公开日常，别把用户的私事塞进来。",
      }));
      if (d.revision !== revision() || d.date !== planDate() || d.timezone !== planTz()) {
        box.appendChild(UI.h("div", { class: "err-box", text: "草稿基于旧安排。请检查最新时间线；继续后保存会替换全部未来活动。" }));
        box.appendChild(actButton("保留草稿，基于新版继续编辑", "rebase-plan", { aria: "保留草稿，基于新版继续编辑" }, function () {
          d.revision = revision(); d.date = planDate(); d.timezone = planTz(); render();
        }));
      }
      d.rows.forEach(function (r, idx) { box.appendChild(editorRow(r, idx, d)); });
      box.appendChild(UI.h("div", { class: "row-actions" }, [
        actButton("＋ 添加一行", "add-row", { title: "加一条安排", aria: "添加一行" }, function () {
          var dd = ensureDraft();
          dd.rows.push({ start: "", end: "", title: "", location: "" });
          render();
        }),
        actButton("保存未来安排", "save-plan", { cls: "btn-primary", title: "保存（只影响未来）", aria: "保存未来安排" }, function () {
          doReplace();
        }),
        actButton("取消", "cancel-edit", { title: "丢弃本次编辑", aria: "取消编辑" }, function () {
          cancelEdit();
        }),
      ]));
      return box;
    }

    function recentCard() {
      var UI = ctx.UI;
      var recent = (snapshot && snapshot.recent) || [];
      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "最近" })]);
      if (!recent.length) { box.appendChild(UI.empty("还没有最近的记录。")); return box; }
      recent.slice(0, 10).forEach(function (r) {
        r = r || {};
        var st = String(r.status || "");
        var bits = [fmtTime(r.start), r.title || r.text || "（未命名）"];
        if (st && LABEL[st]) bits.push(LABEL[st]);
        if (r.outcome) bits.push(r.outcome);
        box.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title" + (st === "done" ? " tag-done" : ""), text: bits.filter(Boolean).join(" · ") }),
          ]),
        ]));
      });
      return box;
    }

    function render() {
      if (!holder) return;
      var UI = ctx.UI;
      var top = holder.scrollTop, left = holder.scrollLeft;
      busyButtons = [];
      var frag = UI.h("div", { class: "life-view" });
      var nb = noticeBox();
      if (nb) frag.appendChild(nb);
      if (loadError && !snapshot) frag.appendChild(UI.errorBox(loadError));
      frag.appendChild(statusCard());
      frag.appendChild(nowCards());
      frag.appendChild(banner());
      frag.appendChild(actionBar());
      frag.appendChild(timeline());
      if (editing) frag.appendChild(editor());
      frag.appendChild(recentCard());
      UI.clear(holder);
      holder.appendChild(frag);
      holder.scrollTop = top; holder.scrollLeft = left;
      setBusy();
    }

    return {
      mount: function (target) {
        holder = target;
        gen++;
        render();
        if (typeof global.setInterval === "function") refreshTimer = global.setInterval(refresh, 60000);
      },
      refresh: refresh,
      unmount: function () {
        if (refreshTimer !== null) global.clearInterval(refreshTimer);
        refreshTimer = null;
        holder = null;
        gen++;          /* 在途请求回来时 token 不匹配，直接丢弃 */
      },
    };
  };
})(window);
