/* 生图：「画一下现在的她」（任务 2 面板层）。
   只做按需触发与结果展示：闸门 / 提示词 / 缓存 / 配额 / 适配器全在后端 core.imagery。

   几条硬规矩（别改）：
   - GET 是只读的：refresh() 只读，绝不 POST；页面打开**不会**触发生成。
   - 生成失败保留旧图：img 的 src 不因失败而清空，错误用提示条说明。
   - 生成中禁用按钮：并发点击在按钮层就挡住（后端还有 conflict 闸门双保险）。
   - 能力如实说明：提供方不支持参考图 / seed 时，界面明确写「不生效」，
     不把没用的参数装成有用。
   - 本页没有可编辑字段：配置在「设置 → 生活 → 状态生图」里改（那边有未保存草稿保护）。
*/
(function (global) {
  "use strict";

  var SIZE_MODE_TEXT = {
    "w+h": "自定义宽高（宽x高）",
    "enum": "按服务端枚举传（宽x高字符串）",
    "none": "不支持指定尺寸",
  };

  global.createImageryView = function createImageryView(ctx) {
    var holder = null;
    var gen = 0;            /* 生命周期代次：卸载后旧响应一律丢弃 */
    var readSeq = 0;        /* 读序号：晚到的旧 GET 不许覆盖新状态 */
    var writeSeq = 0;
    var busy = false;       /* 生成请求在途 */
    var status = null;      /* GET imagery 的回包 */
    var image = null;       /* {data_url, prompt, created_at, provider, model} */
    var imageError = "";    /* 图片取不到的原因（与状态错误分开显示） */
    var loadError = "";     /* 状态读取失败原因 */
    var notice = null;      /* { kind:"error"|"warn"|"info", text } */

    function alive(token) { return token === gen && !!holder; }

    function toast(text, isErr) {
      var fn = ctx.toast || (ctx.UI && ctx.UI.toast);
      if (typeof fn === "function") fn(text, isErr);
    }

    /* ---------------- 数据读取（只读） ---------------- */

    async function refresh() {
      var token = gen;
      var requestId = ++readSeq;
      var data = null, img = null, errText = "", imgErr = "";
      try {
        data = await ctx.apiGet("imagery");
      } catch (error) {
        errText = String((error && error.message) || error || "读取失败");
      }
      if (!alive(token) || requestId !== readSeq) return;
      loadError = errText;
      if (data && data.ok !== false) {
        status = data;
        if (status.latest) {
          try {
            img = await ctx.apiGet("imageryImage");
          } catch (error) {
            imgErr = String((error && error.message) || error || "图片读取失败");
          }
          if (!alive(token) || requestId !== readSeq) return;
          if (img && img.ok !== false && img.image && img.image.data_url) {
            image = img.image;
            imageError = "";
          } else {
            imageError = imgErr || String((img && img.error) || "图片暂时读不到，保留上一次结果。");
          }
        } else {
          image = null;
          imageError = "";
        }
      } else if (data && data.ok === false) loadError = String(data.error || "状态读取失败");
      render();
    }

    /* ---------------- 写操作（唯一入口：按需生成） ---------------- */

    async function doGenerate() {
      if (busy || !canGenerate()) return;   /* 与后端闸门双保险：关着 / 没读到状态就不发 */
      var requestId = ++writeSeq;
      ++readSeq;                     /* 生成开始后，旧 GET 不得再写回页面 */
      busy = true;
      render();                      /* 立即锁按钮，别等网络 */
      var token = gen;
      var res = null, err = "";
      try {
        res = await ctx.apiPost("imageryGenerate", {});
      } catch (error) {
        err = String((error && error.message) || error || "生成失败");
      }
      if (requestId !== writeSeq) return;
      busy = false;
      ++readSeq;                     /* 丢弃生成过程中发起、晚于生成返回的旧读取 */
      if (!alive(token)) {           /* 已切页不应用旧结果，但不能留下永久 busy */
        if (holder) render();
        return;
      }
      if (err) { notify("error", err); render(); return; }
      if (res && res.ok) {
        /* 新图成功才换图：旧图在任何失败路径下都原地不动 */
        if (res.image && res.image.data_url) image = res.image;
        if (status && res.daily) status.daily = res.daily;
        if (status && res.image) {
          status.latest = {
            key: res.image.key, mime: res.image.mime, created_at: res.image.created_at,
            prompt: res.image.prompt, provider: res.image.provider, model: res.image.model,
          };
        }
        var notes = (res.notice || []).join(" ");
        notify("info", (res.cached ? "命中缓存：同参数上次画过，直接复用。" : "画好了。") + (notes ? " " + notes : ""));
      } else {
        /* 失败：旧图保留，后端可能带回最新用量（扣了次数要如实显示） */
        if (status && res && res.daily) status.daily = res.daily;
        notify("error", String((res && res.error) || "生成失败"));
      }
      render();
    }

    function notify(kind, text) {
      notice = text ? { kind: kind, text: String(text) } : null;
    }

    /* ---------------- 渲染 ---------------- */

    function noticeBox() {
      var UI = ctx.UI;
      if (!notice) return null;
      var style = notice.kind === "error"
        ? "border-color:var(--err);color:var(--err);"
        : "border-color:var(--accent-line);color:var(--fg-dim);";
      return UI.h("div", { class: "err-box", style: style, role: "status", "data-kind": notice.kind, text: notice.text });
    }

    function setBusy() {
      var btn = holder && holder.querySelector && holder.querySelector('[data-act="generate"]');
      if (btn) {
        try { btn.disabled = busy || !canGenerate(); } catch (error) { /* 元素已移除 */ }
      }
    }

    function canGenerate() {
      var daily = (status && status.daily) || {};
      var exhausted = typeof daily.used === "number" && typeof daily.limit === "number" && daily.used >= daily.limit;
      return !!status && status.enabled !== false && status.ok !== false && !daily.error && !exhausted;
    }

    function actionBar() {
      var UI = ctx.UI;
      var box = UI.h("div", { class: "row-actions" });
      var daily = (status && status.daily) || {};
      var out = typeof daily.used === "number" && typeof daily.limit === "number" && daily.used >= daily.limit;
      var btn = UI.h("button", {
        class: "btn btn-primary",
        type: "button",
        "data-act": "generate",
        disabled: busy || !canGenerate() || out,
        title: busy ? "正在画，等它完成" : "调用当前提供方画一张「现在的她」（可能计费）",
        text: busy ? "正在画…" : "画一下现在的她",
        onclick: function () { doGenerate(); },
      }, "");
      box.appendChild(btn);
      if (!status) {
        box.appendChild(UI.h("span", { class: "sub", text: "正在读取状态…" }));
      } else if (status.enabled === false) {
        box.appendChild(UI.h("span", { class: "sub", style: "color:var(--err);", text: "状态生图当前关闭：按钮不可用，也不会发任何请求。去「设置 → 生活 → 状态生图」开启。" }));
      } else if (daily.error) {
        box.appendChild(UI.h("span", { class: "sub", style: "color:var(--err);", text: String(daily.error) }));
      } else if (out) {
        box.appendChild(UI.h("span", { class: "sub", style: "color:var(--err);", text: "今日次数已用完（" + daily.used + "/" + daily.limit + "）。" }));
      }
      return box;
    }

    function statusCard() {
      var UI = ctx.UI;
      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "当前能力与配置" })]);
      if (!status) {
        box.appendChild(loadError ? UI.errorBox(loadError) : UI.empty("正在读取…"));
        return box;
      }
      var caps = status.capabilities || {};
      box.appendChild(UI.kv("提供方", status.provider_label || status.provider || "—"));
      box.appendChild(UI.kv("模型绑定", status.model || (status.provider === "a1111" ? "（用服务当前加载的模型）" : "—")));
      if (status.provider === "a1111" && !status.model) {
        box.appendChild(UI.h("div", { class: "sub", text: "未绑定 checkpoint：使用服务当前模型，每次点击都会重新请求模型并计入用量。填写模型名称后才能复用同参数缓存。" }));
      }
      box.appendChild(UI.kv("服务地址", status.base_url || "（未配置）"));
      box.appendChild(UI.kv("密钥", status.api_key_env
        ? status.api_key_present ? "环境变量 " + status.api_key_env + "（已设置）" : "环境变量 " + status.api_key_env + "（未设置！云端会报错）"
        : "（未配置密钥）"));
      box.appendChild(UI.kv("尺寸", (status.size || "—") + " · " + (SIZE_MODE_TEXT[caps.size_mode] || "")));
      if (caps.seed) box.appendChild(UI.kv("随机种子", String(status.seed) + (Number(status.seed) < 0 ? "（每次随机）" : "")));
      else box.appendChild(UI.kv("随机种子", "不支持（不生效）"));
      if (caps.seed) {
        box.appendChild(UI.kv("步数 / 重绘幅度", status.steps + " 步 / " + status.denoising + "%"));
      } else {
        box.appendChild(UI.kv("步数 / 重绘幅度", "不支持（不生效）"));
      }
      box.appendChild(UI.kv("超时", (status.timeout_seconds || "—") + " 秒"));
      var daily = status.daily || {};
      box.appendChild(UI.kv("今日用量", (daily.used || 0) + " / " + (daily.limit || 0) + " 次（按请求已发出记账，失败与超时不返还）"));
      if (caps.known) {
        var ref = caps.reference_image
          ? (status.use_reference ? "支持" + (status.reference_ready ? "（将带当前立绘做参考）" : "（还没有立绘可参考，会按文字锚定画）") : "支持（当前关闭）")
          : "不支持（立绘参考不生效，一致性较弱）";
        box.appendChild(UI.kv("参考图", ref));
        box.appendChild(UI.h("div", { class: "sub", text: caps.consistency_note || "" }));
      } else {
        box.appendChild(UI.h("div", { class: "err-box", text: "提供方「" + (status.provider || "") + "」不认识：生图会被拒绝。" }));
      }
      if (status.notice) box.appendChild(UI.h("div", { class: "sub", style: "color:var(--accent-line);", text: status.notice }));
      box.appendChild(UI.h("div", { class: "sub", text: "提示词只用她的公开日常（当前活动 / 作息 / 自报心情）加上这里的人物与画风锚定，不含私聊日记。配置在「设置 → 生活 → 状态生图」里改。" }));
      return box;
    }

    function imageCard() {
      var UI = ctx.UI;
      var box = UI.h("div", { class: "card panel" }, [UI.h("h3", { text: "最近一张" })]);
      if (image && image.data_url) {
        box.appendChild(UI.h("figure", { class: "imagery-figure", style: "margin:0;" }, [
          UI.h("img", {
            src: image.data_url,
            alt: "最近生成的状态图",
            style: "max-width:100%;height:auto;border-radius:8px;display:block;",
          }),
        ]));
        var bits = [];
        if (image.created_at) bits.push("画于 " + UI.shortTime(image.created_at));
        if (image.provider) bits.push(image.provider + (image.model ? " · " + image.model : ""));
        if (bits.length) box.appendChild(UI.h("div", { class: "sub", text: bits.join(" · ") }));
        if (image.prompt) {
          box.appendChild(UI.h("details", { style: "margin-top:6px;" }, [
            UI.h("summary", { class: "sub", text: "这次用的提示词" }),
            UI.h("div", { class: "sub", style: "white-space:pre-wrap;word-break:break-word;", text: image.prompt }),
          ]));
        }
      } else if (imageError) {
        box.appendChild(UI.h("div", { class: "sub", style: "color:var(--err);", text: imageError }));
      } else if (status && status.latest) {
        box.appendChild(UI.empty("有记录但图片读不到，刷新试试。"));
      } else {
        box.appendChild(UI.empty(status && status.enabled === false
          ? "还没画过。先在设置里配置并开启状态生图。"
          : "还没画过。点上面的按钮来一张。"));
      }
      return box;
    }

    function render() {
      if (!holder) return;
      var UI = ctx.UI;
      var top = holder.scrollTop, left = holder.scrollLeft;
      var frag = UI.h("div", { class: "imagery-view" });
      var nb = noticeBox();
      if (nb) frag.appendChild(nb);
      if (loadError) frag.appendChild(UI.errorBox(loadError));
      if (imageError && image) frag.appendChild(UI.errorBox(imageError));
      frag.appendChild(actionBar());
      frag.appendChild(imageCard());
      frag.appendChild(statusCard());
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
      },
      refresh: refresh,
      unmount: function () {
        holder = null;
        gen++;          /* 在途请求回来时 token 不匹配，直接丢弃 */
      },
    };
  };
})(window);
