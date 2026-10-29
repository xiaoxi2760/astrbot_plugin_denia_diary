/* 桌宠设备管理。设备凭据仅保存在本组件的页面内存中，只显示一次。 */
(function (global) {
  "use strict";

  global.DiaryViews = global.DiaryViews || {};
  global.DiaryViews.PetDevices = function createPetDevicesView(ctx) {
    var holder = null;
    var active = false;
    var generation = 0;
    var readGeneration = 0;
    var writeGeneration = 0;
    var mutationGeneration = 0;
    var loaded = false;
    var loading = false;
    var writeBusy = false;
    var data = null;
    var devices = [];
    var owners = [];
    var selectedPrincipal = "";
    var deviceName = "";
    var issued = null;
    var listError = "";
    var actionError = "";
    var notice = "";
    var confirmRevoke = "";
    var credentialArea = null;
    var copyNote = null;

    function supported() {
      return typeof ctx.isLegacyScope !== "function" || ctx.isLegacyScope();
    }

    function text(value, fallback) {
      if (value === undefined || value === null || value === "") {
        return fallback === undefined ? "—" : fallback;
      }
      return String(value);
    }

    function current(token) {
      return active && generation === token && !!holder;
    }

    function safeDevices(items) {
      return (Array.isArray(items) ? items : []).map(function (item) {
        item = item || {};
        /* GET 列表按白名单读取；意外字段（尤其 credential）不会进入本地视图状态。 */
        return {
          name: text(item.name, "未命名设备"),
          device_id: text(item.device_id, ""),
          agent_id: text(item.agent_id, ""),
          principal_id: text(item.principal_id, ""),
          created_at: text(item.created_at, ""),
          revoked: item.revoked === true || (typeof item.revoked === "string" && item.revoked !== ""),
          revoked_at: typeof item.revoked === "string" ? item.revoked : "",
        };
      });
    }

    function listenerValue() {
      var listener = data && data.listener || {};
      return {
        running: listener.running === true,
        host: text(listener.host, "—"),
        port: text(listener.port, "—"),
        error: text(listener.error, ""),
      };
    }

    function notify(el, message, isError) {
      if (!el) return;
      el.className = "sub pet-devices-message" + (isError ? " err" : "");
      el.textContent = String(message || "");
      el.hidden = !message;
    }

    function rowField(label, value) {
      return ctx.UI.h("div", { class: "sub pet-device-field" }, [
        ctx.UI.h("span", { class: "muted", text: label + "：" }),
        ctx.UI.h("span", { text: text(value, "—") }),
      ]);
    }

    function clearIssued() {
      issued = null;
      if (credentialArea) {
        credentialArea.value = "";
        credentialArea.textContent = "";
      }
      credentialArea = null;
      copyNote = null;
    }

    function draw() {
      if (!holder || !active) return;
      var UI = ctx.UI;
      if (!supported()) {
        UI.clear(holder);
        holder.appendChild(UI.h("section", { class: "card panel pet-devices" }, [
          UI.h("h3", { text: "桌宠设备" }),
          UI.h("p", { class: "sub", text: "现有 v1 状态同步只绑定原有实例。请切换到原有实例管理设备；这里不会复用它的凭据。" }),
        ]));
        return;
      }
      var listener = listenerValue();
      var agentId = data && text(data.agent_id, "") || "";
      credentialArea = null;
      copyNote = null;
      UI.clear(holder);

      var refreshButton = UI.h("button", {
        class: "btn btn-sm", type: "button", text: loading ? "刷新中…" : "刷新设备列表",
        disabled: loading || writeBusy,
        onclick: function () { refresh({ manual: true }); },
      });
      var card = UI.h("section", { class: "card panel pet-devices" }, [
        UI.h("div", { class: "pet-devices-heading" }, [
          UI.h("div", {}, [
            UI.h("h3", { text: "桌宠设备" }),
            UI.h("div", { class: "sub", text: "为已配对桌宠提供当前角色的只读状态同步；与聊天后端分开。" }),
          ]),
          refreshButton,
        ]),
      ]);

      var listenerStatus = UI.h("div", { class: "pet-listener-status" }, [
        UI.h("div", { class: "row-title", text: "独立监听" }),
        UI.h("div", { class: "sub", text: (listener.running ? "运行中" : "未运行")
          + " · " + listener.host + ":" + listener.port
          + (agentId ? " · agent_id：" + agentId : "") }),
      ]);
      card.appendChild(listenerStatus);
      if (listener.error) card.appendChild(UI.h("div", { class: "set-hint warn", text: "监听错误：" + listener.error }));

      if (loading && !loaded) {
        card.appendChild(UI.h("div", { class: "sub", text: "正在读取设备列表…" }));
      } else if (listError) {
        card.appendChild(UI.h("div", { class: "err-box", text: "设备列表读取失败：" + listError }));
      }
      if (actionError) card.appendChild(UI.h("div", { class: "err-box pet-device-action-error", text: actionError }));
      if (notice) card.appendChild(UI.h("div", { class: "sub pet-device-notice", text: notice }));

      var ownerReady = owners.length > 0;
      var issueButton = null;
      function updateIssueButton() {
        if (issueButton) issueButton.disabled = !ownerReady || writeBusy || !!issued
          || !deviceName.trim() || !selectedPrincipal;
      }
      var select = UI.h("select", {
        class: "set-input pet-device-principal", "aria-label": "主人 principal_id",
        disabled: !ownerReady || writeBusy || !!issued,
      });
      if (!ownerReady) {
        select.appendChild(UI.h("option", { value: "", text: "请先配置最亲密名单" }));
      } else {
        owners.forEach(function (owner) {
          select.appendChild(UI.h("option", {
            value: owner, text: owner, selected: owner === selectedPrincipal,
          }));
        });
      }
      select.addEventListener("change", function () {
        selectedPrincipal = String(select.value || "");
        updateIssueButton();
      });

      var nameInput = UI.h("input", {
        class: "set-input pet-device-name", type: "text", maxlength: "60",
        placeholder: "例如：Windows 桌宠", value: deviceName,
        disabled: writeBusy || !!issued, "aria-label": "设备名称",
      });
      nameInput.addEventListener("input", function () {
        deviceName = String(nameInput.value || "");
        updateIssueButton();
      });

      issueButton = UI.h("button", {
        class: "btn btn-sm btn-primary", type: "button", text: writeBusy ? "正在签发…" : "签发设备凭据",
        disabled: !ownerReady || writeBusy || !!issued || !deviceName.trim() || !selectedPrincipal,
        "data-act": "pet-device-issue",
        onclick: issue,
      });
      var issueForm = UI.h("div", { class: "pet-device-issue-form" }, [
        UI.h("label", { class: "pet-device-form-field" }, [
          UI.h("span", { text: "设备名称" }), nameInput,
        ]),
        UI.h("label", { class: "pet-device-form-field" }, [
          UI.h("span", { text: "主人 principal_id" }), select,
        ]),
        issueButton,
      ]);
      card.appendChild(UI.h("div", { class: "pet-device-section" }, [
        UI.h("h4", { text: "配对新设备" }),
        ownerReady
          ? UI.h("div", { class: "sub", text: "凭据只在签发成功后显示这一次。插件不会自动复制；请在桌宠里完成配对。" })
          : UI.h("div", { class: "set-hint warn", text: "请先配置最亲密名单，再签发桌宠设备凭据。" }),
        issueForm,
      ]));

      if (issued) {
        function readOnlyBinding(label, value) {
          var field = UI.h("input", {
            class: "set-input pet-device-binding", type: "text", readonly: true,
            value: value || "（接口未返回）", "aria-label": "签发设备 " + label,
          });
          field.addEventListener("click", function () {
            if (field.focus) field.focus();
            if (field.select) field.select();
          });
          return UI.h("label", { class: "pet-device-form-field" }, [
            UI.h("span", { text: label }), field,
          ]);
        }
        credentialArea = UI.h("textarea", {
          class: "set-input pet-device-credential", rows: "3", readonly: true,
          "aria-label": "一次性设备凭据",
        });
        credentialArea.value = issued.credential;
        credentialArea.textContent = issued.credential;
        copyNote = UI.h("div", { class: "sub pet-device-copy-note" });
        var copyButton = UI.h("button", {
          class: "btn btn-sm", type: "button", text: "复制凭据",
          onclick: copyCredential,
        });
        card.appendChild(UI.h("div", { class: "pet-device-credential-box" }, [
          UI.h("strong", { text: "一次性凭据，请立即粘贴到桌宠" }),
          UI.h("div", { class: "sub", text: "离开此页或手动刷新设备列表后，原始凭据会立即隐藏。不要把它发到聊天或截图中。" }),
          issued.demo ? UI.h("div", { class: "set-hint warn", text: issued.notice || "离线预览演示值，不是可用凭据，不会授权任何真实设备。" }) : null,
          UI.h("div", { class: "pet-device-bindings" }, [
            readOnlyBinding("device_id", issued.device_id),
            readOnlyBinding("agent_id", issued.agent_id),
          ]),
          credentialArea,
          UI.h("div", { class: "pet-device-copy-actions" }, [copyButton, copyNote]),
        ]));
      }

      var listBox = UI.h("div", { class: "pet-device-section" }, [
        UI.h("h4", { text: "已配对设备" }),
      ]);
      if (!devices.length && !loading && !listError) {
        listBox.appendChild(UI.h("div", { class: "sub", text: "还没有配对设备。" }));
      }
      devices.forEach(function (device) {
        var revoked = device.revoked;
        var row = UI.h("div", { class: "row pet-device-row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: device.name + (revoked ? " · 已撤销" : " · 已配对") }),
            rowField("device_id", device.device_id),
            rowField("agent_id", device.agent_id),
            rowField("principal_id", device.principal_id),
            rowField("created_at", device.created_at),
            revoked && device.revoked_at ? rowField("revoked", device.revoked_at) : null,
          ]),
        ]);
        if (!revoked && device.device_id) {
          var revokeLabel = confirmRevoke === device.device_id ? "确认撤销" : "撤销";
          row.appendChild(UI.h("button", {
            class: "btn btn-sm" + (confirmRevoke === device.device_id ? " is-danger" : ""),
            type: "button", text: writeBusy ? "处理中…" : revokeLabel,
            disabled: writeBusy,
            "data-act": "pet-device-revoke",
            "data-device-id": device.device_id,
            onclick: function () {
              if (confirmRevoke !== device.device_id) {
                confirmRevoke = device.device_id;
                actionError = "";
                notice = "再次点击“确认撤销”才会撤销设备 " + device.name + "。";
                draw();
                return;
              }
              revoke(device);
            },
          }));
          if (confirmRevoke === device.device_id) {
            row.appendChild(UI.h("button", {
              class: "btn btn-sm", type: "button", text: "取消",
              disabled: writeBusy,
              onclick: function () { confirmRevoke = ""; notice = ""; draw(); },
            }));
          }
        }
        listBox.appendChild(row);
      });
      card.appendChild(listBox);
      holder.appendChild(card);
    }

    async function readDevices(options) {
      options = options || {};
      if (!active) return;
      if (!supported()) { clearIssued(); draw(); return; }
      if (options.manual) {
        clearIssued();
        notice = "";
        actionError = "";
        confirmRevoke = "";
      }
      var token = generation;
      var readToken = ++readGeneration;
      var mutationToken = mutationGeneration;
      loading = true;
      listError = "";
      draw();
      var result;
      try {
        result = await ctx.apiGet("petDevices", {});
      } catch (error) {
        if (!current(token) || readToken !== readGeneration || mutationToken !== mutationGeneration) return;
        loading = false;
        listError = String((error && error.message) || error || "请求失败");
        draw();
        return;
      }
      if (!current(token) || readToken !== readGeneration || mutationToken !== mutationGeneration) return;
      loading = false;
      loaded = true;
      if (!result || result.ok === false) {
        listError = String((result && (result.error || result.message)) || "接口未返回设备列表");
      } else {
        data = {
          listener: result.listener && typeof result.listener === "object" ? result.listener : {},
          agent_id: text(result.agent_id, ""),
        };
        owners = Array.isArray(result.owners)
          ? result.owners.map(function (one) { return String(one || "").trim(); }).filter(Boolean)
          : [];
        if (owners.indexOf(selectedPrincipal) < 0) selectedPrincipal = owners[0] || "";
        devices = safeDevices(result.devices);
        listError = "";
      }
      draw();
    }

    async function refresh(options) {
      return readDevices(options || {});
    }

    async function issue() {
      if (!active || writeBusy || issued || !owners.length) return;
      var name = String(deviceName || "").trim();
      var principal = String(selectedPrincipal || "").trim();
      if (!name || !principal || owners.indexOf(principal) < 0) {
        actionError = "请填写设备名称，并选择最亲密名单中的主人。";
        draw();
        return;
      }
      var token = generation;
      var writeToken = ++writeGeneration;
      mutationGeneration += 1;
      readGeneration += 1; /* 签发开始后，先前启动的列表读取不得覆盖新状态。 */
      loading = false;
      writeBusy = true;
      actionError = "";
      notice = "正在为“" + name + "”签发设备凭据…";
      draw();
      var result;
      try {
        result = await ctx.apiPost("petDeviceIssue", { name: name, principal_id: principal });
      } catch (error) {
        if (!current(token) || writeToken !== writeGeneration) return;
        writeBusy = false;
        notice = "";
        actionError = "签发失败：" + String((error && error.message) || error || "请求失败");
        draw();
        return;
      }
      if (!current(token) || writeToken !== writeGeneration) return;
      if (!result || result.ok === false || !result.credential) {
        writeBusy = false;
        notice = "";
        actionError = "签发失败：" + String((result && (result.error || result.message)) || "接口没有返回设备凭据");
        draw();
        return;
      }
      issued = {
        device_id: text(result.device_id, ""),
        agent_id: text(result.agent_id, ""),
        credential: String(result.credential),
        demo: result.demo === true,
        notice: text(result.notice, ""),
      };
      notice = issued.demo
        ? "离线预览已生成演示值，不会连接或授权任何真实设备。"
        : "设备凭据已签发。请现在复制到桌宠；自动刷新设备列表不会隐藏这次凭据。";
      confirmRevoke = "";
      /* 签发成功后列表刷新是静默读取；不能清除刚收到的一次性凭据。 */
      await readDevices({ preserveIssued: true });
      if (!current(token) || writeToken !== writeGeneration) return;
      writeBusy = false;
      draw();
    }

    async function revoke(device) {
      if (!active || writeBusy || !device || !device.device_id || confirmRevoke !== device.device_id) return;
      var token = generation;
      var writeToken = ++writeGeneration;
      mutationGeneration += 1;
      readGeneration += 1;
      loading = false;
      writeBusy = true;
      actionError = "";
      notice = "正在撤销设备“" + device.name + "”…";
      draw();
      var result;
      try {
        result = await ctx.apiPost("petDeviceRevoke", { device_id: device.device_id });
      } catch (error) {
        if (!current(token) || writeToken !== writeGeneration) return;
        writeBusy = false;
        confirmRevoke = "";
        notice = "";
        actionError = "撤销失败：" + String((error && error.message) || error || "请求失败");
        draw();
        return;
      }
      if (!current(token) || writeToken !== writeGeneration) return;
      if (!result || result.ok === false) {
        writeBusy = false;
        confirmRevoke = "";
        notice = "";
        actionError = "撤销失败：" + String((result && (result.error || result.message)) || "接口拒绝撤销");
        draw();
        return;
      }
      devices = devices.map(function (item) {
        if (item.device_id !== device.device_id) return item;
        return Object.assign({}, item, { revoked: true, revoked_at: "" });
      });
      if (issued && issued.device_id === device.device_id) {
        /* 这次撤销会立即作废当前一次性凭据；先抹掉变量和输入框，再触发后续列表读取。 */
        clearIssued();
      }
      confirmRevoke = "";
      notice = "设备“" + device.name + "”已撤销。";
      /* 与签发相同，成功后的列表读取不得清除当前凭据或覆盖更新后的撤销状态。 */
      await readDevices({ preserveIssued: true });
      if (!current(token) || writeToken !== writeGeneration) return;
      writeBusy = false;
      draw();
    }

    async function copyCredential() {
      if (!active || !issued || !credentialArea) return;
      var token = generation;
      var credential = issued.credential;
      var clipboard = global.navigator && global.navigator.clipboard;
      if (clipboard && typeof clipboard.writeText === "function") {
        try {
          await clipboard.writeText(credential);
          if (!current(token) || !copyNote) return;
          copyNote.textContent = "已复制。请粘贴到桌宠配对界面。";
          return;
        } catch (error) { /* 受限 iframe 不一定能访问剪贴板，继续手动选择。 */ }
      }
      if (!current(token) || !credentialArea || !copyNote) return;
      if (credentialArea.focus) credentialArea.focus();
      if (credentialArea.select) credentialArea.select();
      copyNote.textContent = "浏览器未允许自动复制，凭据文本已选中，请手动复制。";
    }

    function mount(target) {
      if (!target) return;
      var firstMount = !active;
      if (firstMount) {
        active = true;
        generation += 1;
        loaded = false;
        data = null;
        devices = [];
        owners = [];
        selectedPrincipal = "";
        deviceName = "";
        issued = null;
        listError = "";
        actionError = "";
        notice = "";
        confirmRevoke = "";
      }
      holder = target;
      draw();
      if (firstMount) readDevices();
    }

    function unmount() {
      generation += 1;
      readGeneration += 1;
      writeGeneration += 1;
      mutationGeneration += 1;
      active = false;
      clearIssued();
      if (holder) ctx.UI.clear(holder);
      holder = null;
      writeBusy = false;
      loading = false;
    }

    return { mount: mount, refresh: refresh, unmount: unmount };
  };
})(window);
