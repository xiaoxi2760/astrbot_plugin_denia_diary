/* 小工具：h / clear / toast / 格式化 / 手写 SVG 折线。
   零依赖、零构建——不引任何外部库。 */
(function (global) {
  "use strict";

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "class") el.className = value;
        else if (key === "text") el.textContent = value;
        else if (key === "html") el.innerHTML = value;
        else if (key.slice(0, 2) === "on" && typeof value === "function") {
          el.addEventListener(key.slice(2).toLowerCase(), value);
        } else el.setAttribute(key, value === true ? "" : String(value));
      });
    }
    (Array.isArray(children) ? children : children === undefined || children === null ? [] : [children])
      .forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        el.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      });
    return el;
  }

  /* SVG 元素要带命名空间 */
  function s(tag, attrs, children) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") el.textContent = value;
        else if (key.slice(0, 2) === "on" && typeof value === "function") {
          el.addEventListener(key.slice(2).toLowerCase(), value);
        } else el.setAttribute(key, String(value));
      });
    }
    (Array.isArray(children) ? children : children === undefined || children === null ? [] : [children])
      .forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        el.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      });
    return el;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  var toastTimer = null;
  function toast(message, isError) {
    var box = document.getElementById("toast");
    if (!box) return;
    box.textContent = String(message || "");
    box.className = "toast" + (isError ? " err" : "");
    box.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.hidden = true; }, 2600);
  }

  function bytes(size) {
    var value = Number(size) || 0;
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    return (value / 1024 / 1024).toFixed(2) + " MB";
  }

  function shortTime(value) {
    var text = String(value || "");
    if (!text) return "—";
    var date = new Date(text);
    if (isNaN(date.getTime())) return text;
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
      " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function dayOnly(value) {
    var text = String(value || "");
    return text ? text.slice(0, 10) : "";
  }

  function empty(text) {
    return h("div", { class: "empty", text: text || "这里还没有内容" });
  }

  /* 仪表条（手法参考 astrbot_plugin_daily_life 的 appendMeter）。
     **只是把纯文字数字换一种呈现，不改任何数值口径**——原文字由调用方自己留着。
     axis 非空时会在条下画刻度：valence/arousal 是 -2~+2，映射到 0~100 属于
     前端展示，**必须让人看见它是从哪来的**，不许装成百分比。 */
  function meter(label, value, tone, axis, max) {
    var raw = Number(value);
    if (isNaN(raw)) raw = 0;
    var pct = max ? Math.max(0, Math.min(100, raw / max * 100)) : Math.max(0, Math.min(100, raw));
    /* 有真实分母就显示真分母（熟悉度 0~20 写 n/20，不准装成 n/100）；
       没有分母的按 0~100 呈现，靠 axis 标明它是从什么映射来的。 */
    var shown = max ? String(Math.round(raw)) + "/" + max : String(Math.round(pct)) + "/100";
    var top = h("div", { class: "meter-top" }, [
      h("span", { text: label }),
      h("span", { class: "meter-val", text: shown }),
    ]);
    var bar = h("div", { class: "bar" + (tone ? " is-" + tone : "") });
    bar.style.width = pct + "%";
    var kids = [top, h("div", { class: "track" }, [bar])];
    if (axis && axis.length) {
      kids.push(h("div", { class: "meter-axis" }, axis.map(function (t) {
        return h("span", { text: t });
      })));
    }
    return h("div", { class: "meter" }, kids);
  }

  function errorBox(message) {
    return h("div", { class: "err-box", text: "读取失败：" + String(message || "未知错误") });
  }

  function card(title, value, sub, extraClass) {
    return h("div", { class: "card" + (extraClass ? " " + extraClass : "") }, [
      h("h3", { text: title }),
      h("div", { class: "big", text: String(value === undefined || value === null ? "—" : value) }),
      sub ? h("div", { class: "sub", text: String(sub) }) : null,
    ]);
  }

  function kv(label, value, cls) {
    return h("div", { class: "kv" }, [
      h("span", { class: "muted", text: label }),
      h("b", { class: cls || "", text: String(value === undefined || value === null ? "—" : value) }),
    ]);
  }

  /* series = [{name, color, points:[{x, y, label, title}]}]。
     x 为 YYYY-MM-DD 时按真实日距共享横轴；旧 points 调用仍可按索引绘制。
     缺测保留为断点，不能用 Number(null) 把它画成 0。 */
  function lineChart(options) {
    var width = options.width || 720;
    var height = options.height || 210;
    var padLeft = 34, padRight = 10, padTop = 12, padBottom = 24;
    var innerW = Math.max(width - padLeft - padRight, 1);
    var innerH = Math.max(height - padTop - padBottom, 1);
    var yMin = typeof options.yMin === "number" ? options.yMin : -1;
    var yMax = typeof options.yMax === "number" ? options.yMax : 1;
    var svg = s("svg", {
      class: "chart", viewBox: "0 0 " + width + " " + height, preserveAspectRatio: "none",
      role: "group", "aria-label": options.label || "情绪曲线",
    });
    var series = options.series || [{ points: options.points || [], color: options.color, area: options.area }];
    var all = [];
    var dated = !!options.series || (options.points || []).some(function (p) { return p.x !== undefined; });

    function xValue(value) {
      if (typeof value === "number" && isFinite(value)) return value;
      if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
      var stamp = Date.parse(value + "T00:00:00Z");
      return isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value ? stamp : null;
    }
    function validY(value) { return typeof value === "number" && isFinite(value); }
    var prepared = series.map(function (item) {
      var points = (item.points || []).map(function (p, index) {
        return { point: p, x: dated ? xValue(p.x) : index };
      }).filter(function (entry) { return entry.x !== null; });
      points.sort(function (a, b) { return a.x - b.x; });
      points.forEach(function (entry) { all.push(entry); });
      return { points: points, color: item.color || options.color || "var(--accent)", area: item.area, name: item.name || "" };
    });
    all.sort(function (a, b) { return a.x - b.x; });
    var xMin = all.length ? all[0].x : 0;
    var xMax = all.length ? all[all.length - 1].x : 0;
    function px(value) {
      return xMin === xMax ? padLeft + innerW / 2 : padLeft + innerW * (value - xMin) / (xMax - xMin);
    }
    function py(value) {
      return padTop + innerH - innerH * (value - yMin) / ((yMax - yMin) || 1);
    }
    function activate(node, point) {
      if (typeof options.onPointClick !== "function") return;
      node.setAttribute("role", "button");
      node.setAttribute("tabindex", "0");
      node.setAttribute("aria-label", "查看 " + (point.label || point.x || "") + " 的日记");
      node.style.cursor = "pointer";
      node.addEventListener("click", function () { options.onPointClick(point); });
      node.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault(); options.onPointClick(point);
        }
      });
    }

    [yMax, 0, yMin].filter(function (value, index, list) {
      return value >= yMin && value <= yMax && list.indexOf(value) === index;
    }).forEach(function (value) {
      var y = py(value);
      svg.appendChild(s("line", {
        x1: padLeft, y1: y, x2: width - padRight, y2: y,
        style: "stroke:" + (value === 0 ? "var(--zero)" : "var(--grid)") + ";stroke-width:1",
      }));
      svg.appendChild(s("text", {
        x: 4, y: y + 3, style: "fill:var(--fg-faint);font-size:9px", text: String(value),
      }));
    });

    if (!all.some(function (entry) { return validY(entry.point.y); })) {
      svg.appendChild(s("text", {
        x: width / 2, y: height / 2, style: "fill:var(--fg-faint);font-size:12px",
        "text-anchor": "middle", text: "还没有情绪数据",
      }));
      return svg;
    }

    prepared.forEach(function (item) {
      var segments = [], segment = [];
      item.points.forEach(function (entry) {
        if (validY(entry.point.y)) segment.push(entry);
        else if (segment.length) { segments.push(segment); segment = []; }
      });
      if (segment.length) segments.push(segment);
      var paths = segments.map(function (part) {
        return part.map(function (entry, index) {
          return (index === 0 ? "M" : "L") + px(entry.x).toFixed(1) + " " + py(entry.point.y).toFixed(1);
        }).join(" ");
      });
      if (item.area) {
        segments.forEach(function (part, index) {
          svg.appendChild(s("path", {
            d: paths[index] + " L" + px(part[part.length - 1].x).toFixed(1) + " " + py(yMin).toFixed(1) +
              " L" + px(part[0].x).toFixed(1) + " " + py(yMin).toFixed(1) + " Z",
            style: "fill:" + item.area + ";stroke:none",
          }));
        });
      }
      if (paths.length) svg.appendChild(s("path", {
        d: paths.join(" "), "data-series": item.name,
        style: "fill:none;stroke:" + item.color + ";stroke-width:1.9;stroke-linejoin:round;stroke-linecap:round",
      }));
      item.points.forEach(function (entry) {
        var p = entry.point;
        if (!validY(p.y)) return;
        var dot = s("circle", {
          cx: px(entry.x).toFixed(1), cy: py(p.y).toFixed(1), r: p.strong ? 3.2 : 2.2,
          "data-date": p.label || p.x,
          style: "fill:" + (p.strong ? item.color : "var(--bg)") + ";stroke:" + item.color + ";stroke-width:1.2",
        });
        dot.appendChild(s("title", { text: String(p.title || p.label || "") }));
        activate(dot, p);
        svg.appendChild(dot);
      });
    });

    /* 首尾刻度共用横轴；日期点可用鼠标或键盘打开当天日记。 */
    [all[0], all[all.length - 1]].filter(function (entry, index) {
      return index === 0 || xMin !== xMax;
    }).forEach(function (entry, index) {
      var label = s("text", {
        x: px(entry.x), y: height - 6, style: "fill:var(--fg-faint);font-size:9px",
        "text-anchor": xMin === xMax ? "middle" : index ? "end" : "start",
        text: String(entry.point.label || ""),
      });
      activate(label, entry.point);
      svg.appendChild(label);
    });
    return svg;
  }

  function legend(items) {
    return h("div", { class: "legend" }, items.map(function (item) {
      return h("span", null, [
        h("i", { style: "background:" + item.color }),
        item.text,
      ]);
    }));
  }

  /* ---- 启用范围（scope.mode）：三段分段控件 ----
     **唯一实现**，顶栏和设置页共用同一份，免得两处各画一遍、以后文案各改各的。
     三档的含义与文案照任务书定死，别自己发明。面板本身永远可用，不受这一档影响。 */
  var SCOPE_MODES = [
    { value: "owner", label: "只主人", title: "只在主人（最亲密名单里那个人）的私聊里工作：注入、记日记 / 小本本、观察情绪。群聊一律不介入" },
    { value: "private", label: "只私聊", title: "任何私聊都工作；群聊不介入" },
    { value: "all", label: "全部启用", title: "私聊 + 群聊都工作；主动消息这时才可能发到群里（受群聊上限与四道闸约束）" },
  ];

  function scopeValue(value) {
    var v = String(value === undefined || value === null ? "" : value);
    return SCOPE_MODES.some(function (m) { return m.value === v; }) ? v : "private";
  }

  function scopeLabel(value) {
    var v = scopeValue(value);
    return (SCOPE_MODES.filter(function (m) { return m.value === v; })[0] || SCOPE_MODES[1]).label;
  }

  function scopeControl(value, onPick) {
    var cur = scopeValue(value);
    var buttons = SCOPE_MODES.map(function (mode) {
      var btn = h("button", {
        class: "seg-btn", type: "button", "data-scope": mode.value,
        title: mode.title, text: mode.label,
        onclick: function () {
          if (cur === mode.value) return;
          var prev = cur;            /* 先记住旧的：切换失败要靠它回滚 */
          cur = mode.value; paint();
          if (onPick) onPick(mode.value, prev);
        },
      });
      return btn;
    });
    function paint() {
      buttons.forEach(function (btn) {
        var on = btn.getAttribute("data-scope") === cur;
        btn.className = "seg-btn" + (on ? " is-on" : "");
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    paint();
    return {
      el: h("div", { class: "seg", role: "group", "aria-label": "启用范围" }, buttons),
      get: function () { return cur; },
      set: function (v) { cur = scopeValue(v); paint(); },
    };
  }

  global.UI = {
    h: h, s: s, clear: clear, toast: toast, bytes: bytes, shortTime: shortTime,
    dayOnly: dayOnly, empty: empty, errorBox: errorBox, card: card, kv: kv,
    lineChart: lineChart, legend: legend, meter: meter,
    scopeControl: scopeControl, scopeValue: scopeValue, scopeLabel: scopeLabel,
    SCOPE_MODES: SCOPE_MODES,
  };
})(window);
