/* 设置（第 5.2 步）：**全部配置项**搬进面板。

   三条纪律：
   - **字段表由后端 schema 驱动**（GET settings 的 groups/fields），前端**不抄一份
     字段清单**——`_conf_schema.json` 加一项，这里自动多一项；
   - 改动只进**本地草稿**，底部「保存」一次性提交 changes（点分路径 → 新值）；
   - 坏值在**提交前**就地报错（JSON 解析失败、数字越界提示），不带着坏值去骚扰后端。

   控件按字段类型自动选：bool→开关、int→数字框（schema 给了 min/max 就带上）、
   含换行的 string→textarea（作息表）、list→一行一项、dict→JSON 文本域。 */
(function (global) {
  "use strict";

  global.createSettingsView = function createSettingsView(ctx) {
    var holder = null;
    var schema = null;      /* GET settings 的响应 */
    var values = null;      /* 当前生效值（按顶层 key） */
    var draft = null;       /* 草稿：和 values 同形状 */
    var controls = {};      /* path → 取值函数 / 标脏函数 */
    var errors = {};        /* path → 就地错误 */
    var dirty = false;

    function clone(value) {
      return JSON.parse(JSON.stringify(value === undefined ? null : value));
    }

    function setDirty(flag) {
      dirty = flag;
      var mark = document.getElementById("set-dirty");
      if (mark) mark.textContent = flag ? "有未保存的改动" : "";
    }

    function say(text, isError) {
      var box = document.getElementById("set-result");
      if (!box) return;
      box.className = "sub" + (isError ? " warn" : "");
      box.textContent = String(text || "");
    }

    /* ---- 取值 / 标脏 ---- */

    function markDirty(path, value, error) {
      var parts = String(path).split(".");
      var node = draft;
      for (var i = 0; i < parts.length - 1; i += 1) {
        if (!node[parts[i]] || typeof node[parts[i]] !== "object") node[parts[i]] = {};
        node = node[parts[i]];
      }
      node[parts[parts.length - 1]] = value;
      if (error) errors[path] = String(error); else delete errors[path];
      setDirty(true);
      var hint = document.querySelector('[data-err="' + path + '"]');
      if (hint) {
        hint.textContent = String(error || "");
        hint.style.display = error ? "" : "none";
      }
    }

    /* ---- 控件 ---- */

    function makeControl(field, current) {
      var path = field.path;

      function bind(read) {
        controls[path] = read;
      }

      function finish(read) {
        bind(read);
        return function () {
          var out = read();
          if (out && out.__error) { markDirty(path, out.__value, out.__error); return; }
          markDirty(path, out.__value, "");
        };
      }

      if (field.type === "bool") {
        var box = ctx.UI.h("input", { type: "checkbox", class: "set-switch" });
        box.checked = !!current;
        bind(function () {
          return { __value: box.checked, __error: "" };
        });
        box.addEventListener("change", finish(controls[path]));
        return box;
      }

      if (field.type === "int") {
        var num = ctx.UI.h("input", { type: "number", class: "set-input" });
        num.value = String(current === undefined || current === null ? "" : current);
        if (field.min !== undefined) num.min = String(field.min);
        if (field.max !== undefined) num.max = String(field.max);
        bind(function () {
          var raw = String(num.value).trim();
          if (raw === "") return { __value: field.default, __error: "" };
          var parsed = Number(raw);
          if (!isFinite(parsed) || Math.floor(parsed) !== parsed) {
            return { __value: null, __error: "要填整数" };
          }
          return { __value: parsed, __error: "" };
        });
        num.addEventListener("input", finish(controls[path]));
        return num;
      }

      /* list：一行一项 */
      if (field.type === "list") {
        var listBox = ctx.UI.h("textarea", { class: "set-area", rows: 3 });
        listBox.value = (current || []).join("\n");
        bind(function () {
          var items = listBox.value.split("\n")
            .map(function (line) { return line.trim(); })
            .filter(function (line) { return line !== ""; });
          return { __value: items, __error: "" };
        });
        listBox.addEventListener("input", finish(controls[path]));
        return listBox;
      }

      /* dict：JSON 文本域，解析失败就地报错、绝不提交 */
      if (field.type === "dict") {
        var jsonBox = ctx.UI.h("textarea", { class: "set-area", rows: 3 });
        jsonBox.value = JSON.stringify(current || {}, null, 2);
        bind(function () {
          var raw = jsonBox.value.trim();
          if (raw === "") return { __value: {}, __error: "" };
          try {
            var parsed = JSON.parse(raw);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              return { __value: null, __error: "要写成 {键: 值} 的 JSON 对象" };
            }
            return { __value: parsed, __error: "" };
          } catch (error) {
            return { __value: null, __error: "JSON 解析失败：" + error.message };
          }
        });
        jsonBox.addEventListener("input", finish(controls[path]));
        return jsonBox;
      }

      /* string：默认带换行的（作息表）用 textarea，其余单行 */
      var multiline = !!field.multiline;
      var input = ctx.UI.h(multiline ? "textarea" : "input", {
        class: multiline ? "set-area" : "set-input",
      });
      if (multiline) input.rows = 5;
      input.value = current === undefined || current === null ? "" : String(current);
      bind(function () {
        return { __value: input.value, __error: "" };
      });
      input.addEventListener("input", finish(controls[path]));
      return input;
    }

    /* ---- 渲染 ---- */

    function renderField(field, current, container) {
      var path = field.path;
      var control = makeControl(field, current);

      var line = [control];
      if (field.min !== undefined && field.max !== undefined) {
        line = [control, ctx.UI.h("span", { class: "muted", text: String(field.min) + "-" + String(field.max) })];
      }

      var cells = [ctx.UI.h("div", { class: "set-label", text: field.description || path })];
      cells.push(ctx.UI.h("div", { class: "set-ctl" }, line));
      if (field.hint) {
        cells.push(ctx.UI.h("div", { class: "set-hint", text: field.hint }));
      }
      if (!field.editable) {
        cells.push(ctx.UI.h("div", { class: "set-hint warn", text: field.note || "只读" }));
        control.setAttribute("disabled", "");
      }
      cells.push(ctx.UI.h("div", { class: "set-err", "data-err": path, style: "display:none" }));
      container.appendChild(ctx.UI.h("div", { class: "row set-row" }, [
        ctx.UI.h("div", { class: "row-main" }, cells),
      ]));
      /* 打开设置页就把值灌进草稿（草稿的初值 = 当前生效值） */
      var out = controls[path]();
      if (out && !out.__error) markDirty(path, out.__value, "");
    }

    function renderGroup(node) {
      var box = ctx.UI.h("div", { class: "card panel" });
      box.appendChild(ctx.UI.h("h3", { text: node.description || node.path }));
      if (node.hint) box.appendChild(ctx.UI.h("div", { class: "sub", text: node.hint }));

      if (node.type === "object") {
        var group = values[node.key] || {};
        (node.children || []).forEach(function (child) {
          renderField(child, group[child.key], box);
        });
      } else {
        renderField(node, values[node.key], box);
      }
      return box;
    }

    function render() {
      var UI = ctx.UI;
      UI.clear(holder);
      controls = {};
      errors = {};
      dirty = false;
      setDirty(false);

      var bar = ctx.UI.h("div", { class: "set-bar" }, [
        ctx.UI.h("span", { class: "muted", id: "set-dirty" }),
        ctx.UI.h("span", { class: "set-actions" }, [
          ctx.UI.h("button", { class: "btn btn-sm", type: "button", text: "恢复默认值",
            onclick: fillDefaults }),
          ctx.UI.h("button", { class: "btn btn-sm", type: "button", text: "撤销改动",
            onclick: function () { refresh(); } }),
          ctx.UI.h("button", { class: "btn btn-sm", type: "button", text: "保存", onclick: save }),
        ]),
      ]);
      holder.appendChild(bar);
      holder.appendChild(ctx.UI.h("div", { class: "sub", id: "set-result" }));

      if (schema && schema.warnings && schema.warnings.length) {
        var warnBox = ctx.UI.h("div", { class: "card panel" }, [
          ctx.UI.h("h3", { text: "配置有降级项（面板按实际生效值显示）" }),
        ]);
        schema.warnings.forEach(function (item) {
          warnBox.appendChild(ctx.UI.h("div", { class: "sub warn", text: String(item) }));
        });
        holder.appendChild(warnBox);
      }

      (schema.groups || []).forEach(function (node) {
        holder.appendChild(renderGroup(node));
      });

      if (schema.notices && schema.notices.length) {
        var noteBox = ctx.UI.h("div", { class: "sub" });
        schema.notices.forEach(function (item) {
          noteBox.appendChild(ctx.UI.h("div", { text: String(item) }));
        });
        holder.appendChild(noteBox);
      }
      setDirty(false);
    }

    /* ---- 动作 ---- */

    function collect() {
      var changes = {};
      var bad = [];
      Object.keys(controls).forEach(function (path) {
        var out = controls[path]();
        if (out && out.__error) { bad.push(path + "：" + out.__error); return; }
        changes[path] = out ? out.__value : null;
      });
      return { changes: changes, bad: bad };
    }

    function fillDefaults() {
      draft = clone(schema.defaults);
      values = draft;
      render();
      say("已把表单填成默认值，检查后点「保存」才会落盘。", false);
    }

    async function save() {
      var out = collect();
      if (out.bad.length) {
        say("有 " + out.bad.length + " 项没填对，先改掉再保存：" + out.bad[0], true);
        return;
      }
      say("保存中…", false);
      var result = null;
      try {
        result = await ctx.apiPost("settings", { changes: out.changes });
      } catch (error) {
        say("保存失败：" + String((error && error.message) || error), true);
        return;
      }
      if (!result || result.ok === false) {
        say("没保存：" + String((result && result.error) || "未知原因"), true);
        return;
      }
      var parts = ["已保存 " + ((result.applied || []).length) + " 项"];
      if (result.backup) parts.push("旧配置已备份为 " + result.backup);
      if (result.reloaded) parts.push("巡检 job 已重建");
      (result.notices || []).forEach(function (item) { parts.push(String(item)); });
      (result.warnings || []).forEach(function (item) { parts.push("注意：" + item); });
      say(parts.join("　·　"), false);
      ctx.UI.toast("设置已生效");
      await refresh();
    }

    async function refresh() {
      schema = await ctx.apiGet("settings", {});
      values = clone(schema.values);
      draft = clone(schema.values);
      render();
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { holder = null; },
    };
  };
})(window);
