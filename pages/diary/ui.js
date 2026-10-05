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

  function errorBox(message) {
    return h("div", { class: "err-box", text: "读取失败：" + String(message || "未知错误") });
  }

  function card(title, value, sub) {
    return h("div", { class: "card" }, [
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

  /* 手写 SVG 折线图：points = [{x, y, title}]，画轴 + 折线 + 圆点 + 提示。
     不引任何图表库（受限 iframe + CSP）。 */
  function lineChart(options) {
    var width = options.width || 720;
    var height = options.height || 210;
    var padLeft = 34, padRight = 10, padTop = 12, padBottom = 24;
    var points = (options.points || []).filter(function (p) {
      return typeof p.y === "number" && !isNaN(p.y);
    });
    var svg = s("svg", { class: "chart", viewBox: "0 0 " + width + " " + height, preserveAspectRatio: "none" });

    var innerW = Math.max(width - padLeft - padRight, 1);
    var innerH = Math.max(height - padTop - padBottom, 1);
    var yMin = typeof options.yMin === "number" ? options.yMin : -1;
    var yMax = typeof options.yMax === "number" ? options.yMax : 1;

    function px(index) {
      if (points.length <= 1) return padLeft + innerW / 2;
      return padLeft + (innerW * index) / (points.length - 1);
    }
    function py(value) {
      var span = (yMax - yMin) || 1;
      var ratio = (value - yMin) / span;
      return padTop + innerH - innerH * ratio;
    }

    /* 网格与刻度：0 线 + 上下界。颜色走 CSS 变量，好跟着主题换。 */
    [yMax, 0, yMin].forEach(function (value) {
      var y = py(value);
      svg.appendChild(s("line", {
        x1: padLeft, y1: y, x2: width - padRight, y2: y,
        style: "stroke:" + (value === 0 ? "var(--zero)" : "var(--grid)") + ";stroke-width:1",
      }));
      svg.appendChild(s("text", {
        x: 4, y: y + 3, style: "fill:var(--fg-faint);font-size:9px", text: String(value),
      }));
    });

    if (!points.length) {
      svg.appendChild(s("text", {
        x: width / 2, y: height / 2, style: "fill:var(--fg-faint);font-size:12px",
        "text-anchor": "middle", text: "还没有情绪数据",
      }));
      return svg;
    }

    var d = points.map(function (p, i) {
      return (i === 0 ? "M" : "L") + px(i).toFixed(1) + " " + py(p.y).toFixed(1);
    }).join(" ");

    if (options.area) {
      svg.appendChild(s("path", {
        d: d + " L" + px(points.length - 1).toFixed(1) + " " + py(yMin).toFixed(1) +
           " L" + px(0).toFixed(1) + " " + py(yMin).toFixed(1) + " Z",
        style: "fill:" + options.area + ";stroke:none",
      }));
    }
    svg.appendChild(s("path", {
      d: d, style: "fill:none;stroke:" + (options.color || "var(--accent)") +
        ";stroke-width:1.9;stroke-linejoin:round;stroke-linecap:round",
    }));
    points.forEach(function (p, i) {
      var dot = s("circle", {
        cx: px(i).toFixed(1), cy: py(p.y).toFixed(1), r: p.strong ? 3.2 : 2.2,
        style: "fill:" + (p.strong ? (options.color || "var(--accent)") : "var(--bg)") +
          ";stroke:" + (options.color || "var(--accent)") + ";stroke-width:1.2",
      });
      dot.appendChild(s("title", { text: String(p.title || "") }));
      svg.appendChild(dot);
    });

    /* x 轴：只标首尾，别挤成一团 */
    if (points.length) {
      svg.appendChild(s("text", {
        x: padLeft, y: height - 6, style: "fill:var(--fg-faint);font-size:9px",
        text: String(points[0].label || ""),
      }));
      if (points.length > 1) {
        svg.appendChild(s("text", {
          x: width - padRight, y: height - 6, style: "fill:var(--fg-faint);font-size:9px",
          "text-anchor": "end", text: String(points[points.length - 1].label || ""),
        }));
      }
    }
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

  global.UI = {
    h: h, s: s, clear: clear, toast: toast, bytes: bytes, shortTime: shortTime,
    dayOnly: dayOnly, empty: empty, errorBox: errorBox, card: card, kv: kv,
    lineChart: lineChart, legend: legend,
  };
})(window);
