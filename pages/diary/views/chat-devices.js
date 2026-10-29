/* Unified chat grants are separate from the original read-only state devices. */
(function (global) {
  "use strict";
  global.createChatDevicesView = function (ctx) {
    var UI = ctx.UI, holder = null, serial = 0, busy = false;
    var data = null, name = "", subject = "", issued = null, revokeId = "", error = "";
    var credentialArea = null;

    function clearIssued() {
      issued = null;
      if (credentialArea) credentialArea.value = "";
      credentialArea = null;
    }
    function button(label, action, disabled) {
      return UI.h("button", {type: "button", class: "btn btn-sm", text: label,
        disabled: !!disabled || busy, onclick: action});
    }
    function section(title) {
      return UI.h("section", {class: "card panel pet-devices"}, [UI.h("h3", {text: title})]);
    }
    function render() {
      if (!holder) return;
      if (credentialArea) credentialArea.value = "";
      credentialArea = null;
      UI.clear(holder);
      var card = section("统一聊天设备");
      card.appendChild(UI.h("p", {class: "sub", text: "设备连接当前实例中已有的核心用户，共用该用户的预设、历史、记忆与关系值。更换核心预设后需要重新配对。"}));
      if (error) card.appendChild(UI.errorBox(error));
      holder.appendChild(card);
      if (!data || !data.ok) {
        card.appendChild(UI.empty((data && data.error) || "正在读取设备与核心用户…"));
        return;
      }
      var listener = data.listener || {};
      card.appendChild(UI.h("p", {class: "sub", text: "实例：" + data.scope_id + "　角色：" + data.agent_id
        + "　连接服务：" + (listener.running ? "运行中" : "未运行") + "（" + (listener.host || "—") + ":" + (listener.port || "—") + "）"}));
      if (listener.error) card.appendChild(UI.h("p", {class: "set-hint warn", text: listener.error}));
      if (!data.available) card.appendChild(UI.h("p", {class: "set-hint warn", text: "核心统一聊天服务尚未就绪。请先安装兼容版本，并开启两个插件的实例隔离。"}));
      card.appendChild(UI.h("p", {class: "sub", text: "当前支持文本聊天与原回合查回；语音、状态与主动事件将按后续阶段接入。旧版只读状态凭据继续单独管理。"}));

      var form = section("配对新设备");
      var select = UI.h("select", {class: "set-input", "aria-label": "绑定核心用户", disabled: busy || !!issued});
      select.appendChild(UI.h("option", {value: "", text: "选择已有核心用户"}));
      (data.subjects || []).forEach(function (item) {
        select.appendChild(UI.h("option", {value: item.id, text: (item.nickname || item.id) + "（" + item.id + "）"}));
      });
      select.value = subject;
      select.addEventListener("change", function () { subject = select.value; updateIssue(); });
      var input = UI.h("input", {class: "set-input", type: "text", maxlength: "60", "aria-label": "统一聊天设备名称",
        placeholder: "例如：我的 Windows 桌宠", disabled: busy || !!issued});
      input.value = name;
      input.addEventListener("input", function () { name = input.value; updateIssue(); });
      var issueButton = button("签发统一聊天凭据", issue, true);
      function updateIssue() { issueButton.disabled = busy || !!issued || !data.available || !subject || !name.trim(); }
      updateIssue();
      form.appendChild(UI.h("label", {class: "pet-device-form-field"}, [UI.h("span", {text: "设备名称"}), input]));
      form.appendChild(UI.h("label", {class: "pet-device-form-field"}, [UI.h("span", {text: "绑定核心用户"}), select]));
      form.appendChild(issueButton);
      if (!issued) form.appendChild(button("清空表单", function () { name = ""; subject = ""; render(); }));
      holder.appendChild(form);

      if (issued) {
        var paired = section("请保存这一次配对资料");
        paired.appendChild(UI.h("p", {class: "sub", text: "复制到 PC 桌宠的统一聊天配对页，再填写手机服务地址。凭据只在此处显示一次；不会保存到浏览器。"}));
        credentialArea = UI.h("textarea", {class: "core-input", rows: 11, readonly: true, "aria-label": "一次性统一聊天配对资料"});
        credentialArea.value = JSON.stringify({expectedAgentId: issued.agent_id, deviceId: issued.device_id,
          scopeId: issued.scope_id, platformSubjectId: issued.platform_subject_id, presetId: issued.preset_id,
          namespace: issued.namespace, credential: issued.credential}, null, 2);
        paired.appendChild(credentialArea);
        paired.appendChild(button("复制配对资料", copy));
        paired.appendChild(button("已保存，隐藏配对资料", function () { clearIssued(); name = ""; render(); }));
        holder.appendChild(paired);
      }

      var list = section("当前实例的已授权设备");
      (data.devices || []).forEach(function (item) {
        var row = UI.h("div", {class: "pet-device-section"}, [
          UI.h("strong", {text: item.name || item.device_id}),
          UI.h("div", {class: "sub", text: "用户：" + item.platform_subject_id + "　预设：" + item.preset_id + "　记忆分组：" + item.namespace}),
          UI.h("div", {class: "sub", text: "权限：" + (item.permissions || []).join(" · ")}),
        ]);
        if (revokeId === item.device_id) {
          row.appendChild(UI.h("p", {text: "撤销后，这台设备需要重新配对才能继续聊天。"}));
          row.appendChild(button("确认撤销此设备", function () { revoke(item.device_id); }));
          row.appendChild(button("取消撤销", function () { revokeId = ""; render(); }));
        } else row.appendChild(button("撤销设备", function () { revokeId = item.device_id; render(); }));
        list.appendChild(row);
      });
      if (!(data.devices || []).length) list.appendChild(UI.empty("当前实例还没有统一聊天设备。"));
      list.appendChild(button("刷新设备列表", refresh));
      holder.appendChild(list);
    }
    async function load() {
      var token = ++serial;
      try {
        var result = await ctx.apiGet("petChatDevices");
        if (!holder || token !== serial) return;
        data = result; error = "";
        if (!(result.subjects || []).some(function (item) { return item.id === subject; })) subject = "";
      } catch (_) {
        if (!holder || token !== serial) return;
        error = "设备列表暂时不可用，请稍后刷新。";
      }
      if (holder && token === serial) render();
    }
    async function refresh() { if (!busy) return load(); }
    async function issue() {
      if (busy || issued || !holder || !data || !data.available || !name.trim() || !subject) return;
      busy = true; serial += 1; error = ""; render();
      var token = serial;
      try {
        var result = await ctx.apiPost("petChatDeviceIssue", {name: name.trim(), subject_id: subject});
        if (!holder || token !== serial) return;
        if (!result || !result.ok || typeof result.credential !== "string") throw new Error((result && result.error) || "签发没有成功");
        var keys = ["agent_id", "device_id", "scope_id", "platform_subject_id", "preset_id", "namespace"];
        if (keys.some(function (key) { return typeof result[key] !== "string" || !result[key]; }) || result.scope_id !== data.scope_id) throw new Error("返回的设备绑定不匹配");
        issued = {credential: result.credential};
        keys.forEach(function (key) { issued[key] = result[key]; });
        await load();
      } catch (failure) {
        if (holder && token === serial) error = String(failure.message || "签发没有成功");
      } finally { busy = false; if (holder) render(); }
    }
    async function revoke(id) {
      if (busy || !holder || revokeId !== id) return;
      busy = true; serial += 1; error = ""; render();
      var token = serial;
      try {
        var result = await ctx.apiPost("petChatDeviceRevoke", {device_id: id});
        if (!holder || token !== serial) return;
        if (!result || !result.ok) throw new Error((result && result.error) || "撤销没有成功");
        if (issued && issued.device_id === id) clearIssued();
        revokeId = "";
        await load();
      } catch (failure) {
        if (holder && token === serial) error = String(failure.message || "撤销没有成功");
      } finally { busy = false; if (holder) render(); }
    }
    async function copy() {
      if (!credentialArea || !issued) return;
      var area = credentialArea, token = serial;
      try {
        if (!global.navigator || !global.navigator.clipboard) throw new Error();
        await global.navigator.clipboard.writeText(area.value);
        if (holder && token === serial) ctx.toast("配对资料已复制");
      } catch (_) {
        if (holder && token === serial && credentialArea === area) {
          if (area.focus) area.focus();
          if (area.select) area.select();
          ctx.toast("请手动复制已选中的配对资料");
        }
      }
    }
    return {
      mount: function (target) { holder = target; render(); }, refresh: refresh,
      hasUnsavedChanges: function () { return busy || !!issued || !!name; },
      unmount: function () { serial += 1; clearIssued(); if (holder) UI.clear(holder); holder = null; data = null; name = ""; subject = ""; },
    };
  };
})(window);
