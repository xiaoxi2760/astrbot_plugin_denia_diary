/* Run with Node: node --test test/frontend/test_line_chart.cjs
   Minimal DOM checks rendered SVG output; no browser package or build step. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.attributes = {}; this.children = []; this.events = {}; this.style = {}; this.textContent = ''; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  get firstChild() { return this.children[0]; }
  addEventListener(name, handler) { this.events[name] = handler; }
}
function setup() {
  const document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: text => ({ textContent: text }), getElementById: () => null };
  const sandbox = { window: {}, document, setTimeout, clearTimeout };
  vm.createContext(sandbox);
  const root = path.join(__dirname, '../../pages/diary');
  for (const file of ['ui.js', 'views/proactive.js']) vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), sandbox, { filename: file });
  return sandbox.window;
}
function nodes(root, tag) { return (root.children || []).flatMap(child => [...(child.tagName === tag ? [child] : []), ...nodes(child, tag)]); }
function point(x, y) { return { x, y, label: x }; }

test('dated series share chronological spacing and independent colored paths over full mood range', () => {
  const UI = setup().UI;
  const chart = UI.lineChart({ width: 344, height: 236, yMin: -2, yMax: 2, series: [
    { name: 'now', color: '#pink', points: [point('2026-10-05', -2), point('2026-10-01', 2)] },
    { name: 'baseline', color: '#gray', points: [point('2026-10-02', 0), point('2026-10-05', 1)] },
  ] });
  const paths = nodes(chart, 'path');
  assert.equal(paths.length, 2);
  assert.equal(paths[0].getAttribute('d'), 'M34.0 12.0 L334.0 212.0');
  assert.equal(paths[1].getAttribute('d'), 'M109.0 112.0 L334.0 62.0');
  assert.match(paths[0].getAttribute('style'), /stroke:#pink;/);
  assert.match(paths[1].getAttribute('style'), /stroke:#gray;/);
  assert.deepEqual(nodes(chart, 'text').slice(0, 3).map(n => n.textContent), ['2', '0', '-2']);
  assert.deepEqual(nodes(chart, 'text').slice(-2).map(n => n.textContent), ['2026-10-01', '2026-10-05']);
});

test('null, undefined and nonfinite values create gaps without zeros or invalid SVG coordinates', () => {
  const chart = setup().UI.lineChart({ width: 344, height: 236, yMin: -2, yMax: 2, series: [{ points: [
    point('2026-10-01', 1), point('2026-10-02', null), point('2026-10-03', -1),
    point('2026-10-04', undefined), point('2026-10-05', Infinity),
  ] }] });
  assert.equal(nodes(chart, 'circle').length, 2);
  assert.equal(nodes(chart, 'path')[0].getAttribute('d'), 'M34.0 62.0 M184.0 162.0');
  assert.ok(!nodes(chart, 'path')[0].getAttribute('d').includes('NaN'));
});

test('empty and entirely missing charts render a readable empty state', () => {
  const UI = setup().UI;
  for (const series of [[], [{ points: [] }], [{ points: [point('2026-10-01', null), point('2026-10-02', NaN)] }]]) {
    const chart = UI.lineChart({ series });
    assert.equal(nodes(chart, 'path').length, 0);
    assert.equal(nodes(chart, 'circle').length, 0);
    assert.ok(nodes(chart, 'text').some(n => n.textContent === '还没有情绪数据'));
  }
});

test('invalid dates are skipped and a single date remains centered', () => {
  const chart = setup().UI.lineChart({ width: 344, series: [{ points: [
    point('2026-02-30', 1), point('nonsense', 1), point('2026-10-01', 1),
  ] }] });
  assert.equal(nodes(chart, 'circle').length, 1);
  assert.equal(nodes(chart, 'circle')[0].getAttribute('cx'), '184.0');
  assert.equal(nodes(chart, 'text').filter(n => n.textContent === '2026-10-01').length, 1);
});

test('legacy points retain index positions, color, and optional area', () => {
  const chart = setup().UI.lineChart({ width: 344, points: [{ y: 1 }, { y: 0 }, { y: -1 }], color: '#green', area: '#fill' });
  assert.equal(nodes(chart, 'path').length, 2);
  assert.match(nodes(chart, 'path')[0].getAttribute('style'), /fill:#fill/);
  assert.deepEqual(nodes(chart, 'circle').map(n => n.getAttribute('cx')), ['34.0', '184.0', '334.0']);
});

test('point and date activation support pointer and keyboard without making read-only points buttons', () => {
  const UI = setup().UI;
  const selected = [];
  const options = { series: [{ points: [point('2026-10-01', 0)] }], onPointClick: p => selected.push(p.label) };
  const chart = UI.lineChart(options);
  const dot = nodes(chart, 'circle')[0];
  assert.equal(dot.getAttribute('tabindex'), '0');
  dot.events.click();
  let prevented = false;
  dot.events.keydown({ key: ' ', preventDefault() { prevented = true; } });
  nodes(chart, 'text').at(-1).events.click();
  assert.deepEqual(selected, ['2026-10-01', '2026-10-01', '2026-10-01']);
  assert.ok(prevented);
  const readOnly = UI.lineChart({ series: options.series });
  assert.equal(nodes(readOnly, 'circle')[0].getAttribute('role'), undefined);
});

test('proactive view renders actual independent baseline and current values and navigates to diary dates', async () => {
  const global = setup();
  const opened = [];
  const holder = new Element('div');
  const view = global.createProactiveView({
    UI: global.UI,
    apiGet: async key => key === 'history' ? { points: [
      { date: '2026-10-01', layer: 'now', valence: 2, arousal: null, n: 2 },
      { date: '2026-10-01', layer: 'baseline', valence: -2, arousal: 1, n: 1 },
      { date: '2026-10-03', layer: 'now', valence: 0, arousal: -2, n: 1 },
    ] } : { sessions: [], log: [] },
    openDiaryDate: date => opened.push(date),
  });
  view.mount(holder);
  await view.refresh();
  const charts = nodes(holder, 'svg');
  assert.equal(charts.length, 2);
  assert.equal(nodes(charts[0], 'path').length, 2);
  const moodDots = nodes(charts[0], 'circle');
  assert.equal(moodDots[0].getAttribute('cy'), '12.0');
  assert.equal(moodDots[2].getAttribute('cy'), '186.0');
  assert.equal(nodes(charts[1], 'circle').length, 2, 'missing arousal must not become a neutral zero');
  moodDots[0].events.click();
  assert.deepEqual(opened, ['2026-10-01']);
  assert.match(nodes(moodDots[0], 'title')[0].textContent, /显示首条/);
});
