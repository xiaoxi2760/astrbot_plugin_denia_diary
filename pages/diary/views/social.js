/* 群社交：登记的群与参与者、待登记、最近为何发/为何没发、测试桩预览。

   数据源是 GET social（core.social.panel_payload）；写操作只有三张小表：
   social/group、social/participant（登记 CRUD）与 social/preview（**测试桩**，
   后端 dry_run 只算不发，绝不产生真实群消息）。
   「她在群里的称呼」内联编辑走既有 settings 通道（social.self_names）。 */
(function (global) {
  "use strict";

  /* 只在当前页面的 ctx 生命周期内保存草稿；不写入 localStorage 或磁盘。 */
  var draftsByContext = new WeakMap();

  function draftsFor(ctx) {
    var drafts = draftsByContext.get(ctx);
    if (!drafts) {
      drafts = {
        names: null,
        savedNames: null,
        namesDirty: false,
        manual: { umo: "", name: "" },
        participants: Object.create(null),
      };
      draftsByContext.set(ctx, drafts);
    }
    return drafts;
  }

  global.createSocialView = function createSocialView(ctx) {
    var UI = ctx.UI;
    var holder = null;
    var drafts = draftsFor(ctx);
    var activeDraftControls = { names: null, manual: null, participants: Object.create(null) };
    var readSequence = 0;
    var mountSequence = 0;
    var loadingNode = null;
    var readIssueNode = null;

    async function post(key, body) {
      var result = await ctx.apiPost(key, body);
      if (result && result.ok === false) throw new Error(String(result.reason || result.error || "没有改成"));
      return result;
    }

    function noteBox(title, text, tone) {
      return UI.h("div", { class: "err-box" + (tone ? " " + tone : ""), style: "white-space:pre-wrap;word-break:break-word" }, [
        UI.h("b", { text: title + " " }),
        String(text || ""),
      ]);
    }

    function updateNamesDraft(value) {
      drafts.names = String(value || "");
      drafts.namesDirty = drafts.savedNames !== null && drafts.names !== drafts.savedNames;
    }

    function participantDraft(umo) {
      var key = String(umo || "");
      if (!drafts.participants[key]) drafts.participants[key] = { id: "", name: "", isBot: false, allow: false };
      return drafts.participants[key];
    }

    function captureDrafts() {
      if (activeDraftControls.names) updateNamesDraft(activeDraftControls.names.value);
      if (activeDraftControls.manual) {
        drafts.manual.umo = String(activeDraftControls.manual.umo.value || "");
        drafts.manual.name = String(activeDraftControls.manual.name.value || "");
      }
      Object.keys(activeDraftControls.participants).forEach(function (umo) {
        var controls = activeDraftControls.participants[umo];
        var draft = participantDraft(umo);
        draft.id = String(controls.id.value || "");
        draft.name = String(controls.name.value || "");
        draft.isBot = !!controls.isBot.checked;
        draft.allow = !!controls.allow.checked;
      });
    }

    function currentRead(root, sequence, mounted) {
      return holder === root && readSequence === sequence && mountSequence === mounted;
    }

    function removeNode(root, node) {
      if (node && node.parentNode === root) root.removeChild(node);
    }

    function showReadIssue(root, message) {
      removeNode(root, loadingNode);
      removeNode(root, readIssueNode);
      loadingNode = null;
      readIssueNode = noteBox("加载失败：", message, "warn");
      root.appendChild(readIssueNode);
    }

    /* ---- 登记群卡 ---- */
    function groupCard(group, reload) {
      var umo = String(group.umo || "");
      var on = !!group.enabled;
      var toggle = UI.h("button", {
        class: "btn btn-sm", type: "button",
        text: on ? "已启用（点关）" : "未启用（点开）",
        title: "开＝她可能在这个群里主动发言/接话（仍走免打扰、间隔、每日上限）；关＝只登记不参与",
        onclick: async function () {
          toggle.disabled = true;
          try {
            await post("socialGroup", { action: "save", umo: umo, enabled: !on });
            UI.toast(on ? "已停用「" + (group.name || umo) + "」" : "已启用「" + (group.name || umo) + "」");
            await reload();
          } catch (error) {
            UI.toast("没改成：" + String((error && error.message) || error), true);
          } finally { toggle.disabled = false; }
        },
      });
      var cardEl = UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "stage-tools", style: "justify-content:space-between;margin:14px 0 6px" }, [
          UI.h("div", { class: "panel-title", text: (group.name || "未命名群") + (on ? " · 参与中" : " · 只登记") }),
          UI.h("div", { class: "row-actions" }, [
            toggle,
            UI.h("button", {
              class: "btn btn-sm btn-danger", type: "button", text: "删除登记",
              title: "删除登记会连这个群的话题与短聊状态一起清掉",
              onclick: async function () {
                try {
                  await post("socialGroup", { action: "delete", umo: umo });
                  UI.toast("已删除登记");
                  await reload();
                } catch (error) {
                  UI.toast("没删成：" + String((error && error.message) || error), true);
                }
              },
            }),
          ]),
        ]),
        UI.h("div", { class: "row-sub", text: umo + "（平台 " + (group.platform || "—") + " · 群号 " + (group.group_id || "—") + "）" }),
        participantsPanel(group, reload),
        topicsPanel(group),
        stubPanel(umo, reload),
      ]);
      return cardEl;
    }

    function participantsPanel(group, reload) {
      var umo = String(group.umo || "");
      var list = UI.h("div", { class: "panel" });
      (group.participants || []).forEach(function (person) {
        var bot = !!person.is_bot;
        var allow = !!person.allow_interaction;
        list.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: (person.name || "未命名") + "（" + person.id + "）" }),
            UI.h("div", { class: "row-sub", text: (bot ? "bot" : "人") + " · " + (allow ? "允许互动" : "不允许互动") }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: bot ? "改标为人" : "改标为 bot",
              title: "是人是 bot 由主人标注，绝不按昵称猜",
              onclick: async function () {
                try {
                  await post("socialParticipant", {
                    action: "save", group: umo, id: person.id, name: person.name || "",
                    is_bot: !bot, allow_interaction: allow,
                  });
                  await reload();
                } catch (error) { UI.toast("没改成：" + String((error && error.message) || error), true); }
              },
            }),
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: allow ? "停互动" : "允互动",
              title: "允许＝登记 bot 叫她时她可以回（轮数/冷却/去重照常生效）",
              onclick: async function () {
                try {
                  await post("socialParticipant", {
                    action: "save", group: umo, id: person.id, name: person.name || "",
                    is_bot: bot, allow_interaction: !allow,
                  });
                  await reload();
                } catch (error) { UI.toast("没改成：" + String((error && error.message) || error), true); }
              },
            }),
            UI.h("button", {
              class: "btn btn-sm btn-danger", type: "button", text: "删",
              onclick: async function () {
                try {
                  await post("socialParticipant", { action: "delete", group: umo, id: person.id });
                  await reload();
                } catch (error) { UI.toast("没删成：" + String((error && error.message) || error), true); }
              },
            }),
          ]),
        ]));
      });
      if (!(group.participants || []).length) list.appendChild(UI.empty("还没有参与者登记。群友在群里说话后会自动补一条「人」，是 bot 要你标注。"));

      var draft = participantDraft(umo);
      var idIn = UI.h("input", {
        class: "pill-select", "aria-label": "参与者真实 id", placeholder: "参与者真实 id（不按昵称猜）",
        oninput: function () { draft.id = String(idIn.value || ""); },
      });
      var nameIn = UI.h("input", {
        class: "pill-select", "aria-label": "显示名", placeholder: "显示名（可空）",
        oninput: function () { draft.name = String(nameIn.value || ""); },
      });
      var isBot = UI.h("input", {
        type: "checkbox", "aria-label": "是 bot",
        onchange: function () { draft.isBot = !!isBot.checked; },
      });
      var allow = UI.h("input", {
        type: "checkbox", "aria-label": "允许互动",
        onchange: function () { draft.allow = !!allow.checked; },
      });
      idIn.value = draft.id;
      nameIn.value = draft.name;
      isBot.checked = draft.isBot;
      allow.checked = draft.allow;
      activeDraftControls.participants[umo] = { id: idIn, name: nameIn, isBot: isBot, allow: allow };
      var saving = false;
      var addBtn = UI.h("button", {
        class: "btn btn-sm", type: "button", text: "登记参与者",
        onclick: async function () {
          if (saving) return;
          captureDrafts();
          var submitted = { id: draft.id, name: draft.name, isBot: draft.isBot, allow: draft.allow };
          var pid = String(idIn.value || "").trim();
          if (!pid) { UI.toast("先填参与者的真实 id", true); return; }
          saving = true;
          addBtn.disabled = true;
          try {
            await post("socialParticipant", {
              action: "save", group: umo, id: pid, name: String(nameIn.value || "").trim(),
              is_bot: submitted.isBot, allow_interaction: submitted.allow,
            });
            if (draft.id === submitted.id && draft.name === submitted.name && draft.isBot === submitted.isBot && draft.allow === submitted.allow) {
              draft.id = ""; draft.name = ""; draft.isBot = false; draft.allow = false;
              idIn.value = ""; nameIn.value = ""; isBot.checked = false; allow.checked = false;
            }
            UI.toast("已登记参与者");
            await reload();
          } catch (error) { UI.toast("没记成：" + String((error && error.message) || error), true); }
          finally { saving = false; addBtn.disabled = false; }
        },
      });
      var form = UI.h("div", { class: "row", style: "flex-wrap:wrap;gap:6px" }, [
        idIn, nameIn,
        UI.h("label", { class: "row-sub" }, [isBot, " 是 bot"]),
        UI.h("label", { class: "row-sub" }, [allow, " 允许互动"]),
        addBtn,
      ]);
      return UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "参与者（身份 = 真实 id）" }),
        list, form,
      ]);
    }

    function topicsPanel(group) {
      var items = group.topics || [];
      var kids = [UI.h("div", { class: "panel-title", text: "近期话题（有界：" + (group.topic_total || 0) + " 条在保鲜期内，只展示最近几条）" })];
      if (items.length) {
        items.forEach(function (item) {
          var clock = String(item.ts || "").slice(11, 16) || "--:--";
          var who = item.speaker_name || "有人";
          kids.push(UI.h("div", { class: "row-sub", text: clock + " " + who + "：「" + item.text + "」" }));
        });
      } else {
        kids.push(UI.empty("还没有话题素材。她被 @ 或群里说话时才会攒（只对已启用的群）。"));
      }
      return UI.h("div", { class: "panel" }, kids);
    }

    function stubPanel(umo, reload) {
      var out = UI.h("div", {});
      async function run(kind) {
        out.replaceChildren(UI.h("div", { class: "row-sub", text: "正在生成测试桩…" }));
        try {
          var result = await ctx.apiPost("socialPreview", { kind: kind, group: umo });
          out.replaceChildren();
          if (result && result.ok === false) {
            out.appendChild(noteBox("没有候选：", result.reason || "现在没有可用素材"));
            return;
          }
          if (kind === "chat") {
            out.appendChild(noteBox("测试桩（没发任何真实消息）：", "模拟文本＝" + (result.stub_text || "")));
            (result.trail || []).forEach(function (step) {
              out.appendChild(UI.h("div", { class: "row-sub", text: "· " + step.gate + "：" + step.note }));
            });
            out.appendChild(UI.h("div", { class: "row-title", text: "判定：" + (result.action === "wake" ? "回一句（第 " + result.round + " 轮）" : "不回") + (result.reason ? "（" + result.reason + "）" : "") }));
          } else {
            out.appendChild(UI.h("div", { class: "row-title", text: "候选素材：" + (result.fragment || "—") }));
          }
          if (result.note) out.appendChild(noteBox("唤醒 note（预览）：", result.note));
        } catch (error) {
          out.replaceChildren(UI.errorBox(String((error && error.message) || error)));
        }
      }
      return UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "测试桩（只生成预览，不发真实消息）" }),
        UI.h("div", { class: "row-actions", style: "flex-wrap:wrap;gap:6px" }, [
          UI.h("button", { class: "btn btn-sm", type: "button", text: "预览「接话题」", onclick: function () { return run("topic"); } }),
          UI.h("button", { class: "btn btn-sm", type: "button", text: "预览「分享日程」", onclick: function () { return run("share"); } }),
          UI.h("button", { class: "btn btn-sm", type: "button", text: "模拟 bot 短聊判定", onclick: function () { return run("chat"); } }),
        ]),
        out,
      ]);
    }

    /* ---- 待登记 ---- */
    function seenPanel(seen, reload) {
      var wrap = UI.h("div", { class: "panel" });
      (seen || []).forEach(function (item) {
        wrap.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: item.name || "未命名群" }),
            UI.h("div", { class: "row-sub", text: item.umo + " · 最近看到 " + UI.shortTime(item.last_seen_at) }),
          ]),
          UI.h("div", { class: "row-actions" }, [
            UI.h("button", {
              class: "btn btn-sm", type: "button", text: "一键登记",
              title: "登记默认不开主动发言；登记后在本群卡片上手动启用",
              onclick: async function () {
                try {
                  await post("socialGroup", {
                    action: "save", umo: item.umo,
                    platform: item.platform || "", group_id: item.group_id || "",
                    name: item.name || "", enabled: false,
                  });
                  UI.toast("已登记（默认未启用）");
                  await reload();
                } catch (error) { UI.toast("没记成：" + String((error && error.message) || error), true); }
              },
            }),
          ]),
        ]));
      });
      if (!(seen || []).length) wrap.appendChild(UI.empty("启用范围内还没观察到新群。"));
      return UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "最近看到的群（待登记，有界保留）" }),
        wrap,
      ]);
    }

    /* ---- 最近决策 ---- */
    function decisionsPanel(decisions) {
      var wrap = UI.h("div", { class: "panel" });
      (decisions || []).slice().reverse().forEach(function (item) {
        wrap.appendChild(UI.h("div", { class: "row" }, [
          UI.h("div", { class: "row-main" }, [
            UI.h("div", { class: "row-title", text: (item.action === "wake" ? "回一句" : item.action === "reject" ? "面板操作被拒" : "没回") + (item.round ? "（第 " + item.round + " 轮）" : "") }),
            UI.h("div", { class: "row-sub", text: UI.shortTime(item.ts) + " · " + (item.umo || "") + (item.fragment ? " · " + item.fragment : "") }),
            item.reason ? UI.h("div", { class: "row-sub", text: "原因：" + item.reason }) : null,
          ]),
        ]));
      });
      if (!(decisions || []).length) wrap.appendChild(UI.empty("还没有短聊判定记录。"));
      return UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "最近的短聊判定（为何回 / 为何没回，有界保留）" }),
        wrap,
      ]);
    }

    /* ---- 她在群里的称呼（内联） ---- */
    function namesPanel(reload) {
      var input = UI.h("input", {
        class: "pill-select", "aria-label": "她在群里的称呼",
        placeholder: "如：小澄、澄澄（顿号分隔；留空＝不与 bot 短聊）",
        oninput: function () { updateNamesDraft(input.value); },
      });
      input.value = drafts.names || "";
      activeDraftControls.names = input;
      var saving = false;
      var save = UI.h("button", {
        class: "btn btn-sm", type: "button", text: "保存称呼",
        onclick: async function () {
          if (saving) return;
          captureDrafts();
          var submitted = String(input.value || "").trim();
          saving = true;
          save.disabled = true;
          try {
            await post("settings", { changes: { "social.self_names": submitted } });
            if (drafts.names === input.value) {
              drafts.names = submitted;
              drafts.savedNames = submitted;
              drafts.namesDirty = false;
              input.value = submitted;
            }
            UI.toast("称呼已保存");
            await reload();
          } catch (error) { UI.toast("没改成：" + String((error && error.message) || error), true); }
          finally { saving = false; save.disabled = false; }
        },
      });
      return UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "她在群里的称呼（登记 bot 叫到这些名字才会开始短聊）" }),
        UI.h("div", { class: "row", style: "flex-wrap:wrap;gap:6px" }, [input, save]),
      ]);
    }

    async function refresh() {
      var root = holder;
      if (!root) return;
      captureDrafts();
      var sequence = ++readSequence;
      var mounted = mountSequence;
      if (!root.firstChild) {
        loadingNode = UI.h("div", { class: "row-sub", text: "正在载入群社交…" });
        root.appendChild(loadingNode);
      }
      var payload = null;
      try {
        payload = await ctx.apiGet("social", {});
      } catch (error) {
        if (!currentRead(root, sequence, mounted)) return;
        showReadIssue(root, String((error && error.message) || error));
        return;
      }
      if (!currentRead(root, sequence, mounted)) return;
      if (!payload || typeof payload !== "object") {
        showReadIssue(root, "服务器没有返回有效数据");
        return;
      }
      if (payload.ok === false) {
        showReadIssue(root, String(payload.reason || payload.error || payload.message || "服务器暂时无法加载群社交"));
        return;
      }
      var serverNames = Array.isArray(payload.self_names) ? payload.self_names.join("、") : "";
      var previousSavedNames = drafts.savedNames;
      drafts.savedNames = serverNames;
      if (!drafts.namesDirty || drafts.names === previousSavedNames) {
        drafts.names = serverNames;
        drafts.namesDirty = false;
      } else drafts.namesDirty = drafts.names !== serverNames;

      UI.clear(root);
      loadingNode = null;
      readIssueNode = null;
      activeDraftControls = { names: null, manual: null, participants: Object.create(null) };
      root.appendChild(UI.h("div", { class: "panel-title", text: "群社交：主人登记的群里适度参与；登记 bot 短聊有轮数、冷却与去重，默认不开启任何新群" }));
      if (payload.scope_mode !== "all") {
        root.appendChild(noteBox("注意：", "当前启用范围是「" + (payload.scope_mode === "owner" ? "只主人" : "只私聊") + "」，她不在任何群里工作。要群社交请在顶栏切到「全部启用」。", "warn"));
      }
      if (payload.enabled === false) {
        root.appendChild(noteBox("注意：", "群社交子系统已关闭（设置 → 子系统开关 → 群社交）。", "warn"));
      }
      var reload = function () { return refresh(); };
      var groups = Array.isArray(payload.groups) ? payload.groups : [];
      groups.forEach(function (group) { root.appendChild(groupCard(group, reload)); });
      if (!groups.length) root.appendChild(UI.empty("还没有登记的群。在下面「待登记」一键登记，或手动填 平台 + 群号。"));
      root.appendChild(namesPanel(reload));
      root.appendChild(seenPanel(Array.isArray(payload.seen) ? payload.seen : [], reload));
      root.appendChild(decisionsPanel(Array.isArray(payload.decisions) ? payload.decisions : []));
      var manual = UI.h("input", {
        class: "pill-select", "aria-label": "手动登记（平台:GroupMessage:群号 或 平台 与 群号）",
        placeholder: "手动登记：粘贴群 umo（平台:GroupMessage:群号）",
        oninput: function () { drafts.manual.umo = String(manual.value || ""); },
      });
      var manualName = UI.h("input", {
        class: "pill-select", "aria-label": "群显示名", placeholder: "显示名（可空）",
        oninput: function () { drafts.manual.name = String(manualName.value || ""); },
      });
      manual.value = drafts.manual.umo;
      manualName.value = drafts.manual.name;
      activeDraftControls.manual = { umo: manual, name: manualName };
      var manualSaving = false;
      var manualSave = UI.h("button", {
        class: "btn btn-sm", type: "button", text: "登记",
        onclick: async function () {
          if (manualSaving) return;
          captureDrafts();
          var rawUmo = drafts.manual.umo;
          var rawName = drafts.manual.name;
          var umo = String(rawUmo || "").trim();
          if (!umo) { UI.toast("先粘贴群 umo", true); return; }
          manualSaving = true;
          manualSave.disabled = true;
          try {
            await post("socialGroup", { action: "save", umo: umo, name: String(rawName || "").trim(), enabled: false });
            if (drafts.manual.umo === rawUmo && drafts.manual.name === rawName) {
              drafts.manual.umo = ""; drafts.manual.name = "";
              manual.value = ""; manualName.value = "";
            }
            UI.toast("已登记（默认未启用）");
            await reload();
          } catch (error) { UI.toast("没记成：" + String((error && error.message) || error), true); }
          finally { manualSaving = false; manualSave.disabled = false; }
        },
      });
      root.appendChild(UI.h("div", { class: "panel" }, [
        UI.h("div", { class: "panel-title", text: "手动登记（可选）" }),
        UI.h("div", { class: "row", style: "flex-wrap:wrap;gap:6px" }, [
          manual, manualName, manualSave,
        ]),
      ]));
    }

    return {
      mount: function (root) {
        if (holder) captureDrafts();
        holder = root;
        mountSequence += 1;
        activeDraftControls = { names: null, manual: null, participants: Object.create(null) };
      },
      refresh: refresh,
      unmount: function () {
        captureDrafts();
        holder = null;
        mountSequence += 1;
        readSequence += 1;
        activeDraftControls = { names: null, manual: null, participants: Object.create(null) };
      },
    };
  };
})(window);
