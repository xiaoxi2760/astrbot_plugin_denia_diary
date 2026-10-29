const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attributes = {}; this.events = {};
    this.style = {}; this.dataset = {}; this.className = ''; this.scrollTop = 0; this.scrollLeft = 0;
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  get firstChild() { return this.children[0]; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name, handler) { this.events[name] = handler; }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
async function fixture() {
  const elements = new Map();
  const document = {
    readyState: 'complete', documentElement: new Element(),
    createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }),
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    querySelectorAll: () => [], addEventListener() {},
  };
  let context;
  const pending = new Map();
  const window = {
    location: { search: '' }, localStorage: { getItem: () => '', setItem() {} },
    AstrBotPluginPage: { apiGet: async () => ({ values: {} }) },
  };
  for (const name of ['Overview', 'Diary', 'Settings']) {
    window['create' + name + 'View'] = ctx => {
      context = ctx;
      const content = new Element(); content.textContent = name;
      return {
        mount: body => body.appendChild(content), unmount() {},
        refresh: () => pending.get(name)?.promise || Promise.resolve(),
      };
    };
  }
  const sandbox = { window, document, setTimeout, clearTimeout };
  vm.createContext(sandbox);
  for (const file of ['ui.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);
  }
  await new Promise(resolve => setImmediate(resolve));
  return { panel: window.DiaryPanel, body: document.getElementById('stage-body'), pending, ctx: context };
}

test('a delayed error from the previous tab cannot clear the diary', async () => {
  const f = await fixture();
  const old = deferred(); f.pending.set('Overview', old);
  const previous = f.panel.show('overview');
  await f.panel.show('diary');
  old.reject(new Error('old page disconnected'));
  await previous;
  assert.equal(f.body.firstChild.textContent, 'Diary');
});

test('tab and person changes restore their own reading position', async () => {
  const f = await fixture();
  await f.panel.show('diary'); f.body.scrollTop = 480;
  await f.panel.show('settings'); f.body.scrollTop = 200;
  await f.panel.show('diary'); assert.equal(f.body.scrollTop, 480);
  f.ctx.setWho('other'); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.body.scrollTop, 0);
  f.body.scrollTop = 90;
  f.ctx.setWho(''); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.body.scrollTop, 480);
});

test('late success cannot restore old scroll into a new view or override manual scrolling', async () => {
  const f = await fixture();
  await f.panel.show('diary'); f.body.scrollTop = 480;
  await f.panel.show('settings');
  const old = deferred(); f.pending.set('Diary', old);
  const previous = f.panel.show('diary');
  await f.panel.show('settings'); f.body.scrollTop = 77;
  old.resolve(); await previous;
  assert.equal(f.body.scrollTop, 77);
  const current = deferred(); f.pending.set('Diary', current);
  const loading = f.panel.show('diary'); f.body.scrollTop = 35;
  current.resolve(); await loading;
  assert.equal(f.body.scrollTop, 35);
});

test('an explicit chart date opens at the top; active errors remain readable', async () => {
  const f = await fixture();
  await f.panel.show('diary'); f.body.scrollTop = 480;
  await f.panel.show('settings');
  await f.ctx.openDiaryDate('2026-10-18');
  assert.equal(f.body.scrollTop, 0);
  const failed = deferred(); f.pending.set('Diary', failed);
  const loading = f.panel.show('diary'); failed.reject(new Error('current page disconnected'));
  await loading;
  assert.match(f.body.firstChild.textContent, /current page disconnected/);
});
