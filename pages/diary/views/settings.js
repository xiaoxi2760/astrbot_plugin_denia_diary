/* 设置（第 5.3 步 · 前端增强）：全部配置项搬进面板，并让它在一屏里能用。
   零依赖、零构建——不引任何外部库；只改这一个文件 + style.css 末尾追加样式。

   五件事（用户 00:52 点名要的）：
   1. **分组折叠**——每个顶层节点一个 `<details>`，状态记在 `collapsed` 里，
      重新渲染（保存后 / 撤销后）不丢。
   2. **搜索过滤**——一个输入框，按「名称 / 说明 / 路径 / 当前值」筛。
      搜索时**强制展开**有命中的组；清空搜索回到用户自己的折叠状态。
   3. **改动高亮 + 未保存 N 项**——值和已保存基线不一致的行加左侧色条 + 「已改」角标，
      顶部固定条实时显示「未保存 N 项」，每个组标题上也挂一个该组改了几项的徽标
      （**没有这个徽标，组一折叠就看不见里面改了东西**）。
   4. **错误就地显示**——坏值就显示在那一行下面，不用去猜是哪一行。
   5. **problems 黄条**——有填写错误、或后端报配置降级时，顶部黄条列出来，点一条直接跳到那一行。

   三条纪律（沿用第 5.2 步，没破）：
   - **字段表由后端 schema 驱动**（GET settings 的 groups/fields），前端**不抄一份字段清单**——
     `_conf_schema.json` 加一项，这里自动多一项；
   - 改动只进**本地草稿**，底部「保存」才提交；
   - 坏值在**提交前**就地报错，不带着坏值去骚扰后端。

   控件按字段类型自动选：bool→开关、int→数字框（schema 给了 min/max 就带上）、
   含换行的 string→textarea（作息表）、list→一行一项、dict→JSON 文本域。

   ⚠️ **不要整块重画来刷新状态**：输入过程中重画会把焦点弄丢。
   `paint()` 只做外科手术（改 class / 改文字 / 改 hidden），整块 render 只在
   「打开页面 / 保存后 / 撤销 / 恢复默认」这几种时刻发生。 */
(function (global) {
  "use strict";

  global.createSettingsView = function createSettingsView(ctx) {
    var holder = null;
    var schema = null;       /* GET settings 的响应 */
    var baseline = null;     /* path → 已保存的值。**只读基线**，永远拿它比对"改过没有" */
    var controls = {};       /* path → 该字段的行/控件/读值器/写值器 */
    var groups = {};         /* 顶层节点 path → {el, badge, paths} */
    var collapsed = {};      /* 顶层节点 path → true（用户手动折叠的） */
    var query = "";          /* 搜索词 */
    var onlyDirty = false;   /* 只看改动 */
    var els = {};            /* 常驻节点引用，省得每次重画都去 getElementById */
    var resetArmed = false;  /* 危险区「再点一次」的确认态 */
    var resetTimer = null;

    function clone(value) {
      return JSON.parse(JSON.stringify(value === undefined ? null : value));
    }

    /* 深比较：bool / int / string 直接比；list 逐项；dict 按 key 排序后比，
       免得 {"a":1,"b":2} 和 {"b":2,"a":1} 被当成改了。 */
    function same(a, b) {
      if (a === b) return true;
      if (a === null || b === null || a === undefined || b === undefined) return false;
      if (typeof a !== typeof b) return false;
      if (typeof a !== "object") return false;
      var aIsList = Array.isArray(a), bIsList = Array.isArray(b);
      if (aIsList !== bIsList) return false;
      if (aIsList) {
        if (a.length !== b.length) return false;
        for (var i = 0; i < a.length; i += 1) if (!same(a[i], b[i])) return false;
        return true;
      }
      var ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
      if (ka.length !== kb.length) return false;
      for (var j = 0; j < ka.length; j += 1) {
        if (ka[j] !== kb[j]) return false;
        if (!same(a[ka[j]], b[ka[j]])) return false;
      }
      return true;
    }

    /* 点分路径 → 基线里的值 */
    function baselineAt(path) {
      var parts = String(path).split(".");
      var node = baseline;
      for (var i = 0; i < parts.length; i += 1) {
        if (!node || typeof node !== "object") return undefined;
        node = node[parts[i]];
      }
      return node;
    }

    function valueAt(path) {
      var parts = String(path).split(".");
      var node = schema && schema.values;
      for (var i = 0; i < parts.length; i += 1) {
        if (!node || typeof node !== "object") return undefined;
        node = node[parts[i]];
      }
      return node;
    }

    /* ---- 状态统计：唯一的"改过没有 / 有没有填错"判定处 ---- */

    function computeState() {
      var dirtyPaths = [];
      var problems = [];
      Object.keys(controls).forEach(function (path) {
        var c = controls[path];
        var out = c.read();
        c.value = out.__value;
        c.error = out.__error || "";
        /* 有错的值不算"改动"——它连合法值都不是，不该混进"已改 N 项"里误导人 */
        c.dirty = !c.error && !same(out.__value, baselineAt(path));
        if (c.dirty) dirtyPaths.push(path);
        if (c.error) problems.push({ path: path, text: c.error });
      });
      return { dirtyPaths: dirtyPaths, problems: problems };
    }

    /* ---- 控件 ---- */

    /* ---- 第 6.2 步：结构化编辑器 ----
       **只按 field.editor 分派**，绝不按字段名硬编码——editor 就是后端为此加的。
       editor 为空串的字段走的还是下面那套原逻辑，一行没动。 */

    /* 一行作息 = HH:MM|状态词。`#` 开头是注释；**全角 ｜ 也当分隔符**（用户输入法很容易打出来）。
       返回 {rows, extras}：rows 是能用的，extras 是**原样保留**的（注释 + 解析不了的行）。
       往返安全是硬要求：用户打开面板不该发现自己敲的东西没了。 */
    function parseRhythm(text) {
      var rows = [], extras = [];
      String(text === undefined || text === null ? "" : text).split("\n").forEach(function (line) {
        var trimmed = line.replace(/\s+$/, "");
        if (trimmed.trim() === "") return;            /* 纯空行直接丢，不是用户写的内容 */
        if (trimmed.trim().charAt(0) === "#") { extras.push(trimmed); return; }
        var cut = trimmed.indexOf("|");
        if (cut < 0) cut = trimmed.indexOf("｜");
        if (cut < 0) { extras.push(trimmed); return; }
        var time = trimmed.slice(0, cut).trim();
        var word = trimmed.slice(cut + 1).trim();
        if (!/^\d{2}:\d{2}$/.test(time) ||
            Number(time.slice(0, 2)) > 23 || Number(time.slice(3)) > 59) {
          extras.push(trimmed);
          return;
        }
        if (word === "") { extras.push(trimmed); return; }   /* 词为空后端会丢，收进 extras 免得静默 */
        rows.push({ time: time, word: word });
      });
      rows.sort(function (a, b) { return a.time < b.time ? -1 : (a.time > b.time ? 1 : 0); });
      return { rows: rows, extras: extras };
    }

    function serializeRhythm(rows, extras) {
      var lines = [];
      (rows || []).forEach(function (r) { if (r.time && r.word) lines.push(r.time + "|" + r.word); });
      (extras || []).forEach(function (x) { if (x) lines.push(x); });
      return lines.join("\n");
    }

    function makeRhythmControl(field, current) {
      var UI = ctx.UI;
      var parsed = parseRhythm(current);
      var rows = parsed.rows;
      var extras = parsed.extras;
      var isWeekend = field.path === "state.rhythm_weekend";
      var empty = rows.length === 0 && extras.length === 0;

      var listEl = UI.h("div", { class: "rhythm-rows" });
      var warnEl = UI.h("div", { class: "set-hint warn", hidden: true });

      function draw() {
        UI.clear(listEl);
        if (!rows.length) {
          listEl.appendChild(UI.h("div", { class: "rhythm-empty" , text: "还没有作息段" }));
        }
        rows.forEach(function (row, i) {
          (function (row) {
            var t = UI.h("input", { type: "time", class: "rhythm-time", value: row.time,
              step: "60", title: "从这一刻起生效" });
            var w = UI.h("input", { type: "text", class: "rhythm-word", value: row.word,
              placeholder: "状态词", maxlength: "40" });
            t.addEventListener("change", function () { row.time = t.value || "00:00"; draw(); });
            w.addEventListener("input", function () { row.word = w.value; });
            listEl.appendChild(UI.h("div", { class: "rhythm-row" }, [
              t, w,
              UI.h("button", { class: "btn btn-sm", type: "button", text: "删",
                title: "删掉这一段",
                onclick: function () { rows.splice(i, 1); draw(); } }),
            ]));
          })(row);
        });
        /* 重复时间只提示、不拦：后写的那段覆盖前面那段，是用户的自由 */
        var seen = {}, dup = [];
        rows.forEach(function (r) { if (seen[r.time]) dup.push(r.time); seen[r.time] = 1; });
        warnEl.hidden = dup.length === 0;
        if (dup.length) {
          warnEl.textContent = "时间重复了：" + dup.join("、") + "（后写的会盖住先写的）";
        }
        empty = rows.length === 0 && extras.length === 0;
        if (emptyEl) emptyEl.hidden = !empty;
      }

      var addBtn = UI.h("button", { class: "btn btn-sm", type: "button", text: "+ 加一段",
        onclick: function () {
          var last = rows.length ? rows[rows.length - 1].time : "08:00";
          rows.push({ time: last, word: "" });
          draw();
          var inputs = listEl.querySelectorAll(".rhythm-word");
          var last2 = inputs[inputs.length - 1];
          if (last2 && last2.focus) last2.focus();
        } });
      var emptyEl = UI.h("div", { class: "set-hint" , hidden: true,
        text: "空表 = 没有作息词，她就不会被告知「现在这个点该干嘛」。" });

      var el = UI.h("div", { class: "rhythm" }, [
        UI.h("div", { class: "rhythm-legend",
          text: "到点的最近一段生效；凌晨（第一段之前）归入最后一段" }),
        emptyEl,
        listEl,
        warnEl,
        addBtn,
      ]);

      /* 注释 / 解析不了的行：收进折叠区，保存时原样写回（接在末尾，保持相对顺序） */
      if (extras.length) {
        var box = UI.h("div", { class: "rhythm-extras", hidden: true });
        extras.forEach(function (line) {
          box.appendChild(UI.h("div", { class: "rhythm-extra", text: line }));
        });
        var toggle = UI.h("button", { class: "btn btn-sm", type: "button",
          text: "原样保留（不参与作息）· " + extras.length + " 行",
          title: "注释和解析不了的行。保存时会原样写回去，不会丢。",
          onclick: function () {
            box.hidden = !box.hidden;
            toggle.textContent = (box.hidden ? "原样保留（不参与作息）· " : "收起原样保留 · ") + extras.length + " 行";
          } });
        el.appendChild(UI.h("div", { class: "rhythm-keep" }, [toggle, box]));
      }

      /* 周末表：空态给「照平时那张复制过来」（只填表单，仍要点保存才落盘） */
      if (isWeekend) {
        el.insertBefore(UI.h("div", { class: "set-hint",
          text: "留空 = 周六周日沿用工作日那张表（默认单表）。填了才单独生效。" }), el.firstChild);
        var copyBtn = UI.h("button", { class: "btn btn-sm", type: "button", text: "照平时那张复制过来",
          title: "把工作日作息表的内容复制进这里（只是填进表单，仍要点保存才落盘）",
          onclick: function () {
            var src = parseRhythm(baselineAt("state.rhythm"));
            rows = src.rows.map(function (r) { return { time: r.time, word: r.word }; });
            extras = src.extras.slice();
            draw();
          } });
        el.appendChild(UI.h("div", { class: "rhythm-copy" }, [copyBtn]));
      }

      draw();

      return {
        el: el,
        read: function () {
          /* 状态词为空**直接拦在保存前**：后端会把这行当无效丢掉，
             用户会莫名其妙少一行还以为是自己删错了。 */
          for (var i = 0; i < rows.length; i += 1) {
            if (!String(rows[i].word).trim()) {
              return { __value: null, __error: "第 " + (i + 1) + " 段还没填状态词" };
            }
          }
          return { __value: serializeRhythm(rows, extras), __error: "" };
        },
        set: function (v) {
          var p = parseRhythm(v);
          rows = p.rows;
          extras = p.extras;
          draw();
        },
      };
    }

    /* late_night：HH:MM-HH:MM，可跨午夜；空 = 永不深夜 */
    function makeTimeRangeControl(field, current) {
      var UI = ctx.UI;
      var raw = String(current === undefined || current === null ? "" : current);
      var m = /^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/.exec(raw.trim());
      var startVal = m ? pad2(m[1]) : "23:30";
      var endVal = m ? pad2(m[2]) : "06:30";
      var never = !m;

      var start = UI.h("input", { type: "time", class: "set-input", value: startVal, step: "60", disabled: never });
      var end = UI.h("input", { type: "time", class: "set-input", value: endVal, step: "60", disabled: never });
      var off = UI.h("input", { type: "checkbox", class: "set-switch" });
      off.checked = never;
      function sync() { start.disabled = never; end.disabled = never; }
      off.addEventListener("change", function () { never = off.checked; sync(); });
      sync();

      var el = UI.h("div", { class: "time-range" }, [
        UI.h("div", { class: "time-range-row" }, [start, UI.h("span", { class: "muted", text: "到" }), end]),
        UI.h("label", { class: "set-only" }, [off, " 永不深夜（留空）"]),
        UI.h("div", { class: "set-hint", text: "可以跨午夜，比如 23:30 到次日 06:30 是合法的。" }),
      ]);

      return {
        el: el,
        read: function () {
          if (never) return { __value: "", __error: "" };
          if (!start.value || !end.value) return { __value: null, __error: "起止时间都要填，或勾「永不深夜」" };
          return { __value: start.value + "-" + end.value, __error: "" };
        },
        set: function (v) {
          var mm = /^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/.exec(String(v || "").trim());
          never = !mm;
          if (mm) { start.value = pad2(mm[1]); end.value = pad2(mm[2]); }
          off.checked = never;
          sync();
        },
      };
    }

    function pad2(hhmm) {
      var p = String(hhmm).split(":");
      if (p.length !== 2) return String(hhmm);
      return (p[0].length < 2 ? "0" + p[0] : p[0]) + ":" + (p[1].length < 2 ? "0" + p[1] : p[1]);
    }

    /* 启用范围（第 7 步）：**和顶栏同一个控件**（UI.scopeControl），
       区别只在这里走草稿——改完点保存才落盘，顶栏是点一下就存。
       这里**不认识** "scope.mode" 这个路径，只认 editor === "scope"。 */
    function makeScopeControl(field, current) {
      var ctl = ctx.UI.scopeControl(current);
      return {
        el: ctl.el,
        read: function () { return { __value: ctl.get(), __error: "" }; },
        set: function (v) { ctl.set(v); },
      };
    }

    function makeControl(field, current) {
      var path = field.path;

      /* 第 6.2 步 / 第 7 步：只认 editor，不认字段名 */
      if (field.editor === "rhythm") return makeRhythmControl(field, current);
      if (field.editor === "time_range") return makeTimeRangeControl(field, current);
      if (field.editor === "scope") return makeScopeControl(field, current);


      function text(value) {
        return value === undefined || value === null ? "" : String(value);
      }

      if (field.type === "bool") {
        var box = ctx.UI.h("input", { type: "checkbox", class: "set-switch" });
        box.checked = !!current;
        return {
          el: box,
          read: function () { return { __value: box.checked, __error: "" }; },
          set: function (v) { box.checked = !!v; },
        };
      }

      if (field.type === "int") {
        var num = ctx.UI.h("input", { type: "number", class: "set-input" });
        num.value = text(current);
        if (field.min !== undefined) num.min = String(field.min);
        if (field.max !== undefined) num.max = String(field.max);
        return {
          el: num,
          read: function () {
            var raw = String(num.value).trim();
            if (raw === "") return { __value: field.default, __error: "" };
            var parsed = Number(raw);
            if (!isFinite(parsed) || Math.floor(parsed) !== parsed) {
              return { __value: null, __error: "要填整数" };
            }
            if (field.min !== undefined && parsed < field.min) {
              return { __value: null, __error: "不能小于 " + field.min };
            }
            if (field.max !== undefined && parsed > field.max) {
              return { __value: null, __error: "不能大于 " + field.max };
            }
            return { __value: parsed, __error: "" };
          },
          set: function (v) { num.value = text(v); },
        };
      }

      /* list：一行一项 */
      if (field.type === "list") {
        var listBox = ctx.UI.h("textarea", { class: "set-area", rows: 3 });
        listBox.value = (current || []).join("\n");
        return {
          el: listBox,
          read: function () {
            var items = listBox.value.split("\n")
              .map(function (line) { return line.trim(); })
              .filter(function (line) { return line !== ""; });
            return { __value: items, __error: "" };
          },
          set: function (v) { listBox.value = (v || []).join("\n"); },
        };
      }

      /* dict：默认给「一行一格」的表格（左边 id、右边称呼、后面一个删），
         **别逼人手写 JSON**。但只在当前值**确实是扁平的 字符串→字符串 映射**时才用表格；
         解析不了、或里面有嵌套结构 / 非字符串值，就老实退回下面的 JSON 文本域并就地报错——
         绝不能因为"好看"就把用户已有的数据悄悄改了形。 */
      if (field.type === "dict") {
        var isFlatMap = function (v) {
          if (!v || typeof v !== "object" || Array.isArray(v)) return false;
          return Object.keys(v).every(function (k) { return typeof v[k] === "string"; });
        };

        /* A. 扁平字符串映射 → 表格编辑器 */
        if (isFlatMap(current)) {
          var rowsBox = ctx.UI.h("div", { class: "set-kv-rows" });
          var rows = [];
          function addRow(key, val) {
            var keyIn = ctx.UI.h("input", { class: "set-input set-kv-key", type: "text", value: key === undefined || key === null ? "" : String(key) });
            var valIn = ctx.UI.h("input", { class: "set-input set-kv-val", type: "text", value: val === undefined || val === null ? "" : String(val) });
            var row = { key: keyIn, val: valIn, el: null };
            row.el = ctx.UI.h("div", { class: "set-kv-row" }, [
              keyIn,
              ctx.UI.h("span", { class: "muted set-kv-arrow", text: "→" }),
              valIn,
              ctx.UI.h("button", {
                class: "btn btn-sm", type: "button", text: "删", title: "删掉这一条",
                onclick: function () {
                  rows = rows.filter(function (r) { return r !== row; });
                  rowsBox.removeChild(row.el);
                },
              }),
            ]);
            rows.push(row);
            rowsBox.appendChild(row.el);
          }
          var seed = Object.keys(current);
          if (seed.length) seed.forEach(function (k) { addRow(k, current[k]); });
          else addRow("", "");

          return {
            el: ctx.UI.h("div", { class: "set-kv" }, [
              rowsBox,
              ctx.UI.h("button", {
                class: "btn btn-sm set-kv-add", type: "button", text: "＋ 加一条",
                onclick: function () { addRow("", ""); },
              }),
            ]),
            read: function () {
              var out = {};
              for (var i = 0; i < rows.length; i += 1) {
                var k = String(rows[i].key.value || "").trim();
                var v = String(rows[i].val.value || "").trim();
                if (k === "" && v === "") continue;        /* 全空的行直接忽略 */
                if (k === "") return { __value: null, __error: "有一行左边没填是谁" };
                if (Object.prototype.hasOwnProperty.call(out, k)) return { __value: null, __error: "「" + k + "」重复了" };
                out[k] = v;
              }
              return { __value: out, __error: "" };
            },
            set: function (v) {
              if (!isFlatMap(v)) return;                  /* 不是扁平结构就别画表格，保持现状 */
              rows = [];
              ctx.UI.clear(rowsBox);
              var keys = Object.keys(v);
              if (keys.length) keys.forEach(function (k) { addRow(k, v[k]); });
              else addRow("", "");
            },
          };
        }

        /* B. 兜底：JSON 文本域，解析失败就地报错、绝不提交 */
        var jsonBox = ctx.UI.h("textarea", { class: "set-area", rows: 3 });
        jsonBox.value = JSON.stringify(current || {}, null, 2);
        return {
          el: jsonBox,
          read: function () {
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
          },
          set: function (v) { jsonBox.value = JSON.stringify(v || {}, null, 2); },
        };
      }

      /* string：默认带换行的（作息表）用 textarea，其余单行 */
      var multiline = !!field.multiline;
      var input = ctx.UI.h(multiline ? "textarea" : "input", {
        class: multiline ? "set-area" : "set-input",
      });
      if (multiline) input.rows = 5;
      input.value = text(current);
      return {
        el: input,
        read: function () { return { __value: input.value, __error: "" }; },
        set: function (v) { input.value = text(v); },
      };
    }

    /* ---- 渲染 ---- */

    function renderField(field, current, groupPath, box, bare) {
      var path = field.path;
      var control = makeControl(field, current);

      /* 搜索用的料：名称 / 说明 / 路径 / 类型 / 当前值，合成一条小写字符串 */
      var haystack = [
        field.description || "", field.hint || "", path, field.type,
        control.el.value || "",
      ].join(" ").toLowerCase();

      /* `bare`：这个字段自己就是一个顶层节点，组标题和组说明已经把它说完了，
         再渲染一遍标签和说明就成了同一句话连着说两遍（第 5.3 步第一版就这毛病）。
         只留控件、范围提示和错误。 */
      var cells = [];
      if (!bare) {
        cells.push(ctx.UI.h("div", { class: "set-head" }, [
          ctx.UI.h("span", { class: "set-label", text: field.description || path }),
          ctx.UI.h("span", { class: "set-chip", hidden: true, text: "已改" }),
          ctx.UI.h("button", {
            class: "btn btn-sm set-revert", type: "button", text: "改回", hidden: true,
            title: "把这一项恢复成已保存的值",
            onclick: function () { control.set(baselineAt(path)); paint(); },
          }),
        ]));
      } else {
        cells.push(ctx.UI.h("div", { class: "set-head set-head--bare" }, [
          ctx.UI.h("span", { class: "set-chip", hidden: true, text: "已改" }),
          ctx.UI.h("button", {
            class: "btn btn-sm set-revert", type: "button", text: "改回", hidden: true,
            title: "把这一项恢复成已保存的值",
            onclick: function () { control.set(baselineAt(path)); paint(); },
          }),
        ]));
      }

      var ctl = [control.el];
      if (field.min !== undefined && field.max !== undefined) {
        ctl.push(ctx.UI.h("span", {
          class: "muted set-range", text: String(field.min) + "-" + String(field.max),
        }));
      }
      cells.push(ctx.UI.h("div", { class: "set-ctl" }, ctl));
      if (field.hint && !bare) cells.push(ctx.UI.h("div", { class: "set-hint", text: field.hint }));
      if (field.editable === false) {
        cells.push(ctx.UI.h("div", { class: "set-hint warn", text: field.note || "只读" }));
        control.el.setAttribute("disabled", "");
      }
      var errEl = ctx.UI.h("div", { class: "set-err", "data-err": path, hidden: true });
      cells.push(errEl);

      var row = ctx.UI.h("div", { class: "row set-row" }, [ctx.UI.h("div", { class: "row-main" }, cells)]);
      box.appendChild(row);

      /* 输入即刷新状态。**不重画整块**——重画会把焦点从输入框里踢出去。 */
      control.el.addEventListener("input", paint);
      control.el.addEventListener("change", paint);

      controls[path] = {
        field: field,
        group: groupPath,
        el: control.el,
        read: control.read,
        set: control.set,
        row: row,
        errEl: errEl,
        chip: row.querySelector(".set-chip"),
        revert: row.querySelector(".set-revert"),
        haystack: haystack,
        dirty: false,
        error: "",
        value: undefined,
      };
    }

    function renderGroup(node) {
      var isObject = node.type === "object";
      var box = ctx.UI.h("div", { class: "set-body" });

      var summary = ctx.UI.h("summary", { class: "set-sum" }, [
        ctx.UI.h("span", { class: "set-caret", text: "▾" }),
        ctx.UI.h("span", { class: "set-sum-title", text: node.description || node.path }),
        ctx.UI.h("span", { class: "set-badge", hidden: true }),
      ]);

      var details = ctx.UI.h("details", { class: "card panel set-group", open: collapsed[node.path] ? null : true }, [
        summary,
        box,
      ]);
      if (collapsed[node.path]) details.removeAttribute("open");
      /* 用户手动折叠/展开 → 记住，别让保存后的重画把它弹回去 */
      details.addEventListener("toggle", function () {
        if (searching()) return;      /* 搜索期间是程序在控制 open，不算用户意愿 */
        if (details.open) delete collapsed[node.path];
        else collapsed[node.path] = true;
      });

      if (isObject) {
        var groupValues = (schema.values && schema.values[node.key]) || {};
        (node.children || []).forEach(function (child) {
          renderField(child, groupValues[child.key], node.path, box, false);
        });
      } else {
        renderField(node, valueAt(node.path), node.path, box, true);
      }

      if (node.hint) details.insertBefore(
        ctx.UI.h("div", { class: "set-hint set-group-hint", text: node.hint }), summary.nextSibling);

      groups[node.path] = {
        el: details,
        body: box,
        badge: summary.querySelector(".set-badge"),
        paths: Object.keys(controls).filter(function (p) { return controls[p].group === node.path; }),
      };
      return details;
    }

    function render() {
      var UI = ctx.UI;
      disarmReset();          /* 重画前先解除确认态：旧按钮马上被丢弃，别让 armed 状态漏到新按钮上 */
      UI.clear(holder);
      controls = {};
      groups = {};

      /* ---- 顶部固定区：未保存计数 + 按钮 + problems 黄条 ---- */
      els.dirty = UI.h("span", { class: "set-dirty", id: "set-dirty" });
      els.problems = UI.h("div", { class: "set-problems", id: "set-problems", hidden: true });

      holder.appendChild(UI.h("div", { class: "set-sticky" }, [
        UI.h("div", { class: "set-bar" }, [
          els.dirty,
          UI.h("span", { class: "set-actions" }, [
            UI.h("button", { class: "btn btn-sm", type: "button", text: "恢复默认值", onclick: fillDefaults }),
            UI.h("button", { class: "btn btn-sm", type: "button", text: "撤销改动", onclick: revertAll }),
            UI.h("button", { class: "btn btn-sm btn-primary", type: "button", text: "保存", onclick: save }),
          ]),
        ]),
        els.problems,
      ]));

      els.result = UI.h("div", { class: "sub set-result", id: "set-result" });
      holder.appendChild(els.result);

      /* ---- 工具条：搜索 + 只看改动 ---- */
      els.search = UI.h("input", {
        type: "search", class: "set-search", placeholder: "搜配置项：名称 / 说明 / 路径 / 当前值",
        value: query, "aria-label": "搜索配置项",
      });
      els.search.addEventListener("input", function () {
        query = String(els.search.value || "").trim();
        paint();
      });

      els.onlyDirty = UI.h("input", { type: "checkbox", class: "set-switch" });
      els.onlyDirty.checked = onlyDirty;
      els.onlyDirty.addEventListener("change", function () {
        onlyDirty = !!els.onlyDirty.checked;
        paint();
      });

      els.count = UI.h("span", { class: "set-count muted" });

      holder.appendChild(UI.h("div", { class: "set-tools" }, [
        els.search,
        UI.h("label", { class: "set-only" }, [els.onlyDirty, " 只看改动"]),
        els.count,
      ]));

      /* ---- 分组 ---- */
      (schema.groups || []).forEach(function (node) { holder.appendChild(renderGroup(node)); });

      if (schema.notices && schema.notices.length) {
        var noteBox = UI.h("div", { class: "set-notice sub" });
        schema.notices.forEach(function (item) {
          noteBox.appendChild(UI.h("div", { text: String(item) }));
        });
        holder.appendChild(noteBox);
      }

      /* ---- 危险区：一键恢复全部默认（立即生效）----
         **跟上面那个「恢复默认值」按钮不是一回事**，别混淆：
           上面那个 = fillDefaults()，只把表单填成默认值，**不落盘**，还能反悔；
           这里这个 = POST settings/reset，**直接落盘 + 热生效 + 重建巡检 job**，撤不回来。 */
      els.reset = UI.h("button", {
        class: "btn btn-sm set-reset-btn", type: "button",
        text: "一键恢复全部默认（立即生效）", onclick: resetAll,
      });
      holder.appendChild(UI.h("div", { class: "danger-zone" }, [
        UI.h("div", { class: "danger-title", text: "危险操作" }),
        UI.h("div", { class: "danger-text sub" }, [
          "把插件的全部配置项恢复成出厂默认值，",
          UI.h("strong", { text: "立即生效" }),
          "：会落盘、热重载配置并重建巡检任务。想先看看默认值长什么样，"
            + "请用上面的「恢复默认值」——那个只填表单，不写盘。",
        ]),
        els.reset,
      ]));

      paint();
    }

    /* ---- 状态上色：只改 class / 文字 / hidden，不重建节点 ---- */

    function searching() { return query !== ""; }

    function rowVisible(c) {
      if (searching() && c.haystack.indexOf(query.toLowerCase()) < 0) return false;
      if (onlyDirty && !c.dirty && !c.error) return false;
      return true;
    }

    function paint() {
      var st = computeState();
      var visible = 0;

      Object.keys(controls).forEach(function (path) {
        var c = controls[path];
        var shown = rowVisible(c);
        c.row.hidden = !shown;
        if (shown) visible += 1;
        c.row.className = "row set-row"
          + (c.dirty ? " set-row--dirty" : "")
          + (c.error ? " set-row--bad" : "");
        c.chip.hidden = !c.dirty;
        c.revert.hidden = !c.dirty;
        c.errEl.textContent = c.error;
        c.errEl.hidden = !c.error;
      });

      Object.keys(groups).forEach(function (key) {
        var g = groups[key];
        var n = 0, any = false;
        g.paths.forEach(function (p) {
          var c = controls[p];
          if (!c) return;
          if (c.dirty) n += 1;
          if (!c.row.hidden) any = true;
        });
        g.badge.textContent = n ? String(n) : "";
        g.badge.hidden = !n;
        g.el.hidden = !any;
        /* 搜索时把有命中的组撑开；**不搜索时 `collapsed` 是唯一真相**，
           无条件按它摆一次 open——不然搜索期间被程序关掉的组，
           清空搜索后会停在折叠态（用户明明没手动折叠过它）。 */
        g.el.open = searching() ? any : !collapsed[key];
      });

      var total = Object.keys(controls).length;
      els.dirty.textContent = st.dirtyPaths.length
        ? ("未保存 " + st.dirtyPaths.length + " 项")
        : "没有未保存的改动";
      els.dirty.className = "set-dirty" + (st.dirtyPaths.length ? " is-dirty" : "");
      els.count.textContent = (visible === total)
        ? ("共 " + total + " 项")
        : ("显示 " + visible + " / " + total + " 项");

      paintProblems(st);
    }

    /* ---- problems 黄条：填写错误 + 后端降级 ---- */

    function jumpTo(path) {
      var c = controls[path];
      if (!c) return;
      if (c.group && groups[c.group]) {
        delete collapsed[c.group];
        groups[c.group].el.open = true;
        groups[c.group].el.hidden = false;
      }
      c.row.hidden = false;
      c.row.scrollIntoView({ block: "center" });
      try { c.el.focus(); } catch (error) { /* 某些控件不可聚焦，忽略 */ }
    }

    function paintProblems(st) {
      var UI = ctx.UI;
      var warnings = (schema && schema.warnings) || [];
      UI.clear(els.problems);

      if (st.problems.length) {
        els.problems.appendChild(UI.h("div", { class: "set-problems-line" }, [
          UI.h("b", { text: st.problems.length + " 项填写有问题，保存不了：" }),
          UI.h("span", {
            class: "set-jump-list",
          }, st.problems.map(function (item) {
            return UI.h("button", {
              class: "set-jump", type: "button",
              text: (controls[item.path] ? controls[item.path].field.description || item.path : item.path)
                + "：" + item.text,
              onclick: function () { jumpTo(item.path); },
            });
          })),
        ]));
      }

      if (warnings.length) {
        els.problems.appendChild(UI.h("div", { class: "set-problems-line" }, [
          UI.h("b", { text: warnings.length + " 项配置被降级（面板按实际生效值显示）：" }),
          UI.h("span", { class: "set-warn-list" }, warnings.map(function (item) {
            return UI.h("div", { class: "set-warn-item", text: String(item) });
          })),
        ]));
      }

      els.problems.hidden = !(st.problems.length || warnings.length);
    }

    function say(text, isError) {
      if (!els.result) return;
      els.result.className = "sub set-result" + (isError ? " err" : "");
      els.result.textContent = String(text || "");
    }

    /* ---- 动作 ---- */

    function revertOne(path) {
      var c = controls[path];
      if (!c) return;
      c.set(baselineAt(path));
      paint();
    }

    function revertAll() {
      var st = computeState();
      if (!st.dirtyPaths.length) { say("没有可撤销的改动。", false); return; }
      st.dirtyPaths.forEach(revertOne);
      say("已撤销 " + st.dirtyPaths.length + " 项改动。", false);
    }

    function fillDefaults() {
      /* 目标值取后端给的 defaults，**按点分路径直接深取**。
         不用「摊平成点分路径」那套：name_preference 的默认值是 {}，一摊平就没了，
         恢复默认会漏掉它。
         **不能动 baseline**——基线是"已保存的值"，改了就再也算不出差异
         （第 5.2 步的 fillDefaults 把 values 一起覆盖，正是这个坑）。 */
      var filled = 0;
      Object.keys(controls).forEach(function (path) {
        var c = controls[path];
        var value = defaultAt(path);
        if (value === undefined && c.field && c.field.default !== undefined) value = c.field.default;
        if (value === undefined) return;      /* schema 没给默认值就别乱填 */
        c.set(value);
        filled += 1;
      });
      var st = computeState();
      say("已把 " + filled + " 项填成默认值，检查后点「保存」才会落盘"
        + "（与已保存值不同的有 " + st.dirtyPaths.length + " 项）。", false);
    }

    /* ---- 危险区：一键恢复全部默认（立即生效）---- */

    /* 解除两步确认：把按钮文案和颜色还原，并把 4 秒自动还原的定时器清掉。
       凡是「发完请求」「重画」「卸载」都必须走它，否则按钮会停在红色等着下一次误点。 */
    function disarmReset() {
      resetArmed = false;
      if (resetTimer) { global.clearTimeout(resetTimer); resetTimer = null; }
      if (els.reset) {
        els.reset.textContent = "一键恢复全部默认（立即生效）";
        els.reset.classList.remove("is-danger");
      }
    }

    async function resetAll() {
      /* 第一步只武装，不发请求。**绝对不能用 window.confirm**——
         Dashboard 插件页是受限 iframe，sandbox 没有 allow-modals，会被直接拦掉。 */
      if (!resetArmed) {
        resetArmed = true;
        els.reset.textContent = "确定恢复？再点一次";
        els.reset.classList.add("is-danger");
        resetTimer = global.setTimeout(disarmReset, 4000);
        say("这一步会立刻落盘并热生效，不是一般的「填表单」。再点一次确认。", false);
        return;
      }
      disarmReset();

      say("正在恢复默认值…", false);
      var result = null;
      try {
        result = await ctx.apiPost("settingsReset", {});
      } catch (error) {
        say("恢复失败：" + String((error && error.message) || error), true);
        return;
      }
      if (!result || result.ok === false) {
        say("没恢复：" + String((result && (result.error || result.message)) || "未知原因"), true);
        return;
      }

      var changed = (result.changed || []).length;
      var parts = [changed ? ("已恢复 " + changed + " 项为默认值")
        : "所有配置本来就是默认值，什么都没改"];
      if (result.backup) parts.push("旧配置已备份为 " + result.backup);
      if (result.reloaded) parts.push("配置已热生效");
      (result.notices || []).forEach(function (item) { parts.push(String(item)); });
      (result.warnings || []).forEach(function (item) { parts.push("注意：" + item); });

      ctx.UI.toast(changed ? "已恢复默认值" : "没有可恢复的项");
      /* 重拉：后端 apply_settings 换的是**已保存**的值，表单还停在旧值上，
         不重拉的话用户看到的和真生效的对不上。 */
      await refresh();
      say(parts.join("　·　"), false);
    }

    /* 点分路径 → defaults 里的值（{"diary":{"max_chars":1}} 取 "diary.max_chars"） */
    function defaultAt(path) {
      var parts = String(path).split(".");
      var node = schema && schema.defaults;
      for (var i = 0; i < parts.length; i += 1) {
        if (!node || typeof node !== "object") return undefined;
        node = node[parts[i]];
      }
      return node;
    }

    async function save() {
      var st = computeState();
      if (st.problems.length) {
        say("有 " + st.problems.length + " 项没填对，先改掉再保存。", true);
        jumpTo(st.problems[0].path);
        return;
      }
      if (!st.dirtyPaths.length) { say("没有改动，不用保存。", false); return; }

      /* **只提交改过的**——第 5.2 步是把全部字段原样发一遍，
         那样后端 applied 永远是字段总数，用户根本看不出自己改了几项。 */
      var changes = {};
      st.dirtyPaths.forEach(function (path) { changes[path] = controls[path].value; });

      say("保存中…（" + st.dirtyPaths.length + " 项）", false);
      var result = null;
      try {
        result = await ctx.apiPost("settings", { changes: changes });
      } catch (error) {
        say("保存失败：" + String((error && error.message) || error), true);
        return;
      }
      if (!result || result.ok === false) {
        say("没保存：" + String((result && result.error) || "未知原因"), true);
        return;
      }
      var parts = ["已保存 " + ((result.applied || []).length || st.dirtyPaths.length) + " 项"];
      if (result.backup) parts.push("旧配置已备份为 " + result.backup);
      if (result.reloaded) parts.push("巡检任务已重建");
      (result.notices || []).forEach(function (item) { parts.push(String(item)); });
      (result.warnings || []).forEach(function (item) { parts.push("注意：" + item); });
      /* 保存成功后把新档位推给顶栏，两个入口永远显示同一个值。
         认的是 field.editor === "scope"（field 本来就存在 controls[path] 里），
         不按路径名判断——换个字段路径也不影响。 */
      st.dirtyPaths.forEach(function (path) {
        var c = controls[path];
        if (c && c.field && c.field.editor === "scope" && ctx.setScope) ctx.setScope(c.value);
      });
      ctx.UI.toast("设置已生效");
      await refresh();
      /* 面板标题（panel.brand / panel.brand_sub）改完要**当场**换掉左上角，
         不然用户得刷新页面才看得见自己刚改的字。整组一起推，省得逐字段盯。 */
      if (ctx.setPanel) ctx.setPanel(schema.values && schema.values.panel);
      say(parts.join("　·　"), false);
    }

    async function refresh() {
      schema = await ctx.apiGet("settings", {});
      baseline = clone(schema.values);
      render();
    }

    return {
      mount: function (target) { holder = target; },
      refresh: refresh,
      unmount: function () { disarmReset(); holder = null; },
    };
  };
})(window);
