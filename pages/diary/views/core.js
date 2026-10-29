/* One authenticated admin page, scoped by the same frozen ctx as the diary. */
(function (global) {
  "use strict";
  global.createCoreView = function (ctx) {
    var UI = ctx.UI, holder = null, dirty = false, busy = false, serial = 0;
    var data = null, selectedPreset = "", offset = 0;
    var drafts = Object.create(null);

    function button(label, action, danger) {
      return UI.h("button", {type: "button", class: "btn btn-sm" + (danger ? " btn-danger" : ""), text: label,
        disabled: busy, onclick: action});
    }
    function field(label, value, multiline, kind) {
      var input = UI.h(multiline ? "textarea" : "input", {
        class: "core-input", type: multiline ? null : (kind || "text"), rows: multiline ? 7 : null,
        "aria-label": label, oninput: function () {
          if (input.value === String(value === null || value === undefined ? "" : value)) delete drafts[label];
          else drafts[label] = input.value;
          dirty = Object.keys(drafts).length > 0;
        },
      });
      input.value = Object.prototype.hasOwnProperty.call(drafts, label) ? drafts[label] : (value === null || value === undefined ? "" : String(value));
      return {label: label, input: input, node: UI.h("label", {class: "core-field"}, [UI.h("span", {text: label}), input])};
    }
    function box(title) {
      return UI.h("section", {class: "card panel core-section"}, [UI.h("h3", {text: title})]);
    }
    function guard() {
      if (dirty) { ctx.toast("请先保存或放弃表单修改。", true); return false; }
      return !busy;
    }
    async function send(body, fields) {
      if (busy || !holder) return;
      busy = true;
      try {
        var result = await ctx.apiPost("coreAction", Object.assign({subject_id: data.subject_id || ctx.who()}, body));
        if (!holder) return;
        if (!result || !result.ok) throw new Error((result && result.error) || "保存失败");
        (fields || []).forEach(function (item) { delete drafts[item.label]; });
        dirty = Object.keys(drafts).length > 0;
        ctx.toast("核心已保存");
        await refresh();
      } catch (error) {
        if (holder) ctx.toast(String(error.message || error), true);
      } finally { busy = false; if (holder && data) render(); }
    }

    function render() {
      UI.clear(holder);
      if (!data || !data.ok) {
        holder.appendChild(UI.empty((data && data.error) || "核心服务不可用"));
        return;
      }
      var toolbar = UI.h("div", {class: "row"});
      var subject = UI.h("select", {class: "set-input", "aria-label": "核心用户"});
      subject.appendChild(UI.h("option", {value: "", text: "选择已有核心用户"}));
      (data.subjects || []).forEach(function (item) {
        subject.appendChild(UI.h("option", {value: item.id, text: (item.nickname || item.id) + "（" + item.id + "）"}));
      });
      subject.value = data.subject_id || "";
      subject.addEventListener("change", function () {
        if (!guard()) { subject.value = data.subject_id || ""; return; }
        offset = 0;
        ctx.setWho(subject.value);
      });
      toolbar.appendChild(subject);
      toolbar.appendChild(button("放弃表单修改", function () { if (busy) return; drafts = Object.create(null); dirty = false; render(); }));
      holder.appendChild(toolbar);
      holder.appendChild(UI.h("p", {class: "sub", text: "核心数据由核心插件保存；日记与生活数据仍按当前实例管理。"}));

      var presets = data.presets || [], persona = box("人设与核心预设");
      if (!presets.some(function (item) { return item.id === selectedPreset; })) {
        selectedPreset = ((data.active || {}).preset_id) || ((presets[0] || {}).id) || "";
      }
      var picker = UI.h("select", {class: "set-input", "aria-label": "查看核心预设"});
      presets.forEach(function (item) { picker.appendChild(UI.h("option", {value: item.id, text: item.name || item.id})); });
      picker.value = selectedPreset;
      picker.addEventListener("change", function () {
        if (!guard()) { picker.value = selectedPreset; return; }
        selectedPreset = picker.value; render();
      });
      persona.appendChild(picker);
      var preset = presets.filter(function (item) { return item.id === selectedPreset; })[0];
      if (preset) {
        var name = field("预设名称", preset.name), desc = field("预设说明", preset.description), prompt = field("人设提示词", preset.system_prompt, true);
        [name, desc, prompt].forEach(function (item) { persona.appendChild(item.node); });
        persona.appendChild(button("保存人设", function () { send({action: "preset_save", preset_id: preset.id,
          revision: preset.revision, name: name.input.value, description: desc.input.value, system_prompt: prompt.input.value}, [name, desc, prompt]); }));
      } else { persona.appendChild(UI.empty("这个实例还没有核心预设。")); }
      holder.appendChild(persona);
      if (!data.active) {
        holder.appendChild(UI.empty("选择已有核心用户后，可管理其记忆、历史和关系数据。"));
        return;
      }
      var active = data.active, state = box("当前对话");
      (data.uncertain_turns || []).forEach(function (item) {
        var warning = box("需要检查的桌宠回合");
        warning.appendChild(UI.h("p", {text: "回合 " + item.request_id + " 的执行状态不明。请检查下面的历史、记忆和关系值；确认后允许开始新回合，原请求不会自动重做。"}));
        warning.appendChild(button("已检查，允许继续新回合", function () {
          if (guard()) send({action: "turn_ack_uncertain", turn_id: item.turn_id, revision: item.revision, reviewed: true});
        }));
        holder.appendChild(warning);
      });
      state.appendChild(UI.h("p", {text: "当前预设：" + (active.preset_id || "默认") + "　记忆分组：" + active.namespace}));
      var style = UI.h("select", {class: "set-input", "aria-label": "切换用户核心预设"});
      (data.styles || []).forEach(function (item) { if (item.preset_id) style.appendChild(UI.h("option", {value: item.key, text: item.key + " · " + item.name})); });
      var activeStyle = (data.styles || []).filter(function (item) { return item.preset_id === active.preset_id && item.namespace === active.namespace; })[0];
      if (activeStyle) style.value = activeStyle.key;
      state.appendChild(style);
      state.appendChild(button("应用到此用户", function () { if (guard()) send({action: "subject_preset", style_key: style.value, revision: active.revision}); }));
      state.appendChild(button("开始新会话并保留旧记录", function () { if (guard()) send({action: "history_restart", revision: active.revision}); }));
      state.appendChild(button("恢复上次保留的会话", function () { if (guard()) send({action: "history_restore", revision: active.revision}); }));
      holder.appendChild(state);

      var relation = box("好感度与观测值");
      relation.appendChild(UI.h("p", {text: "好感度：" + (data.affection ? data.affection.score : "尚无记录") + "　观测值：" + (data.observation ? data.observation.balance : "尚无记录")}));
      var delta = field("好感度调整（-2 至 2）", "0", false, "number");
      relation.appendChild(delta.node);
      relation.appendChild(button("调整好感度", function () { send({action: "affection_adjust", delta: Number(delta.input.value)}, [delta]); }));
      var amount = field("增加观测值（1 至 10000）", "1", false, "number");
      relation.appendChild(amount.node);
      relation.appendChild(button("增加观测值", function () { send({action: "observation_add", amount: Number(amount.input.value)}, [amount]); }));
      holder.appendChild(relation);

      var memory = box("长期记忆");
      (data.memory || []).forEach(function (item) {
        var entry = UI.h("div", {class: "core-memory"}), value = field("记忆内容 · " + item.id, item.value, true), importance = field("重要度 · " + item.id, item.importance, false, "number");
        entry.appendChild(value.node); entry.appendChild(importance.node);
        entry.appendChild(button("保存记忆", function () { send({action: "memory_save", memory_id: item.id, revision: item.revision, value: value.input.value, importance: Number(importance.input.value)}, [value, importance]); }));
        entry.appendChild(button("归档记忆", function () { if (guard()) send({action: "memory_archive", memory_id: item.id, revision: item.revision}); }, true));
        memory.appendChild(entry);
      });
      if (!(data.memory || []).length) memory.appendChild(UI.empty("当前分组没有长期记忆。"));
      memory.appendChild(button("上一页", function () { if (guard()) { offset = Math.max(0, offset - 50); refresh(); } }));
      if (data.has_more) memory.appendChild(button("下一页", function () { if (guard()) { offset += 50; refresh(); } }));
      (data.archived_memory || []).forEach(function (item) {
        memory.appendChild(UI.h("div", {class: "row"}, [UI.h("span", {text: item.value}), button("恢复归档", function () { if (guard()) send({action: "memory_restore", archive_id: item.archive_id}); })]));
      });
      holder.appendChild(memory);
      var history = box("当前会话历史（最近 50 条）");
      (data.history || []).forEach(function (item) {
        history.appendChild(UI.h("div", {class: "core-memory"}, [UI.h("strong", {text: item.role + " · " + UI.shortTime(item.created_at)}), UI.h("pre", {class: "core-history", text: item.text})]));
      });
      if (!(data.history || []).length) history.appendChild(UI.empty("当前会话还没有历史。"));
      holder.appendChild(history);
    }

    async function refresh() {
      var current = ++serial;
      var result = await ctx.apiGet("coreSnapshot", {who: ctx.who(), offset: offset});
      if (!holder || current !== serial) return;
      data = result;
      if (result && result.ok) ctx.mergeOptions((result.subjects || []).map(function (item) { return {id: item.id, name: item.nickname || item.id}; }));
      render();
    }
    return {mount: function (target) { holder = target; }, refresh: refresh,
      hasUnsavedChanges: function () { return dirty || busy; },
      unmount: function () { serial++; holder = null; }};
  };
})(window);
