/* imagery.js 视图测试（node --test）。
   钉住任务 2 的前端验收：只读不打扰、生成成功才换图、失败保留旧图、
   生成中锁按钮、旧返回不覆盖新页、能力如实展示。 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.events = {};
    this.style = {}; this.dataset = {}; this.value = ''; this._disabled = false; this.hidden = false;
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
  get firstChild() { return this.children[0]; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(n => n.textContent || '').join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'value') this.value = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  set disabled(value) { this._disabled = !!value; if (value) this.attributes.disabled = ''; else delete this.attributes.disabled; }
  get disabled() { return this._disabled; }
  querySelector(selector) { return selector === '[data-act="generate"]' ? nodes(this).find(n => n.attributes?.['data-act'] === 'generate') : null; }
  addEventListener(name, callback) { this.events[name] = callback; }
  focus() {}
  async emit(name, value) { if (value !== undefined) this.value = value; return this.events[name]?.({ target: this, preventDefault() {} }); }
}

const OLD_URL = 'data:image/png;base64,AAAAOLD';
const NEW_URL = 'data:image/png;base64,BBBBNEW';

function statusPayload(overrides = {}) {
  return Object.assign({
    ok: true, enabled: true,
    provider: 'a1111', provider_label: '本机 Stable Diffusion WebUI（A1111 兼容 API）',
    model: '', base_url: 'http://127.0.0.1:7860', api_key_env: '', api_key_present: false,
    size: '512x512', steps: 20, seed: -1, denoising: 60, use_reference: true,
    timeout_seconds: 90, character_hint: '', style_hint: '',
    capabilities: { known: true, reference_image: true, seed: true, negative_prompt: true,
      size_mode: 'w+h', auth_required: false, consistency_note: '支持参考图，一致性较强。' },
    reference_ready: false,
    daily: { date: '2026-10-26', used: 0, limit: 3 },
    latest: null,
  }, overrides);
}

function deferred() { let resolve; const promise = new Promise(done => resolve = done); return {promise, resolve}; }

function fixture(statusOverrides = {}) {
  const document = {createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'),
    createTextNode: text => ({textContent: String(text)}), getElementById: () => null};
  const window = {}; const sandbox = {window, document, setTimeout, clearTimeout, console};
  vm.createContext(sandbox);
  for (const file of ['ui.js', 'views/imagery.js'])
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);
  const gets = [], posts = [], toasts = [];
  const ctx = {
    UI: window.UI, toast: (...args) => toasts.push(args),
    apiGet: async key => { gets.push(key); return statusPayload(statusOverrides); },
    apiPost: async (key, body) => { posts.push({key, body}); return {ok: false, error: '未配置'}; },
    who: () => 'owner',
  };
  const root = new Element();
  const view = window.createImageryView(ctx);
  view.mount(root);
  return {ctx, root, view, gets, posts, toasts};
}

function nodes(root) { return [root, ...(root.children || []).flatMap(nodes)]; }
function action(f, name) { const node = nodes(f.root).find(n => n.attributes?.['data-act'] === name); assert.ok(node, name); return node; }
function imageOf(f) { return nodes(f.root).find(n => n.tagName === 'IMG'); }
function buttonOf(f) { return action(f, 'generate'); }
async function click(node) { await node.emit('click'); await new Promise(done => setImmediate(done)); }

test('refresh only reads status and image, never posts', async () => {
  const overrides = {latest: {key: 'k1', created_at: '2026-10-26T10:00:00', prompt: 'p'}};
  const f = fixture(overrides);
  f.ctx.apiGet = async key => { f.gets.push(key); return key === 'imageryImage'
    ? {ok: true, image: {key: 'k1', data_url: OLD_URL, mime: 'image/png', created_at: 'x', prompt: 'p'}}
    : statusPayload(overrides); };
  await f.view.refresh();
  assert.ok(f.gets.includes('imagery')); assert.ok(f.gets.includes('imageryImage'));
  assert.equal(f.posts.length, 0, '打开页面绝不触发生成');
  assert.ok(imageOf(f), '有图就显示');
  assert.equal(imageOf(f).attributes.src, OLD_URL);
});

test('empty state shows placeholder and no image element', async () => {
  const f = fixture();
  await f.view.refresh();
  assert.ok(!imageOf(f));
  assert.match(f.root.textContent, /还没画过/);
  assert.ok(f.gets.includes('imagery'));
  assert.ok(!f.gets.includes('imageryImage'), '没有最近一张就不发图片请求');
});

test('generate success swaps in the new image and updates usage', async () => {
  const f = fixture();
  await f.view.refresh();
  f.ctx.apiPost = async (key, body) => {
    f.posts.push({key, body});
    return {ok: true, cached: false, notice: [],
      image: {key: 'k2', data_url: NEW_URL, mime: 'image/png', created_at: 'now', prompt: '她在浇花', provider: 'a1111', model: ''},
      daily: {date: '2026-10-26', used: 1, limit: 3}};
  };
  await click(buttonOf(f));
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].key, 'imageryGenerate');
  assert.equal(imageOf(f).attributes.src, NEW_URL, '新图成功后才换图');
  assert.match(f.root.textContent, /1 \/ 3/);
});

test('generate failure keeps the old image and explains the error', async () => {
  const f = fixture({latest: {key: 'k1', created_at: 'x', prompt: 'p'}});
  f.ctx.apiGet = async key => key === 'imageryImage'
    ? {ok: true, image: {key: 'k1', data_url: OLD_URL, mime: 'image/png', created_at: 'x', prompt: 'p'}}
    : statusPayload({latest: {key: 'k1', created_at: 'x', prompt: 'p'}});
  await f.view.refresh();
  f.ctx.apiPost = async (key, body) => ({ok: false, error: '今日生图次数已用完（3/3）', daily: {date: '2026-10-26', used: 3, limit: 3}});
  await click(buttonOf(f));
  assert.equal(imageOf(f).attributes.src, OLD_URL, '失败必须保留旧图');
  assert.match(f.root.textContent, /已用完/);
  assert.match(f.root.textContent, /3 \/ 3/, '失败带回的最新用量要如实显示');
});

test('button is disabled while generating and after quota is exhausted', async () => {
  const f = fixture();
  await f.view.refresh();
  const d = deferred();
  f.ctx.apiPost = () => d.promise;
  const pending = click(buttonOf(f));
  assert.ok(Object.hasOwn(buttonOf(f).attributes, 'disabled'), '生成中按钮必须锁住');
  d.resolve({ok: true, cached: true, notice: [], image: {key: 'k', data_url: NEW_URL, mime: 'image/png'}, daily: {used: 1, limit: 3}});
  await pending;
  assert.ok(!Object.hasOwn(buttonOf(f).attributes, 'disabled'), '完成后恢复可用');

  const f2 = fixture({daily: {date: '2026-10-26', used: 3, limit: 3}});
  await f2.view.refresh();
  assert.ok(Object.hasOwn(buttonOf(f2).attributes, 'disabled'), '次数用完按钮不可用');
  assert.ok(f2.posts.length === 0);
});

test('late response after unmount cannot repopulate the view', async () => {
  const f = fixture();
  await f.view.refresh();
  const d = deferred();
  f.ctx.apiPost = () => d.promise;
  const pending = click(buttonOf(f));
  f.view.unmount();
  d.resolve({ok: true, cached: false, notice: [], image: {key: 'k', data_url: NEW_URL, mime: 'image/png'}, daily: {used: 1, limit: 3}});
  await pending;
  assert.ok(!imageOf(f) || imageOf(f).attributes.src !== NEW_URL, '卸载后旧返回一律丢弃');
});

test('a stale image read cannot overwrite a successful generation', async () => {
  const f = fixture({latest: {key: 'old'}});
  const read = deferred();
  f.ctx.apiGet = async key => key === 'imageryImage' ? read.promise : statusPayload({latest: {key: 'old'}});
  const pendingRead = f.view.refresh();
  await new Promise(done => setImmediate(done));
  f.ctx.apiPost = async () => ({ok: true, image: {key: 'new', data_url: NEW_URL}, daily: {used: 1, limit: 3}});
  await click(buttonOf(f));
  read.resolve({ok: true, image: {key: 'old', data_url: OLD_URL}});
  await pendingRead;
  assert.equal(imageOf(f).attributes.src, NEW_URL);
});

test('returning to a view cannot leave it permanently busy after an old generation', async () => {
  const f = fixture();
  await f.view.refresh();
  const request = deferred();
  f.ctx.apiPost = () => request.promise;
  const pending = click(buttonOf(f));
  f.view.unmount();
  f.view.mount(f.root);
  request.resolve({ok: true, image: {key: 'stale', data_url: NEW_URL}, daily: {used: 1, limit: 3}});
  await pending;
  assert.equal(buttonOf(f).disabled, false);
  assert.ok(!imageOf(f), '旧代次的生成图不能覆盖当前页面');
});

test('a failed image refresh preserves the previous usable image', async () => {
  const f = fixture({latest: {key: 'old'}});
  f.ctx.apiGet = async key => key === 'imageryImage'
    ? {ok: true, image: {key: 'old', data_url: OLD_URL}} : statusPayload({latest: {key: 'old'}});
  await f.view.refresh();
  f.ctx.apiGet = async key => key === 'imageryImage'
    ? {ok: false, error: '图片读取暂时失败'} : statusPayload({latest: {key: 'old'}});
  await f.view.refresh();
  assert.equal(imageOf(f).attributes.src, OLD_URL);
  assert.match(f.root.textContent, /图片读取暂时失败/);
});

test('quota exhaustion remains disabled after DOM properties synchronize and blocks scripted clicks', async () => {
  const f = fixture({daily: {used: 3, limit: 3}});
  await f.view.refresh();
  assert.equal(buttonOf(f).disabled, true);
  await click(buttonOf(f));
  assert.equal(f.posts.length, 0);
});

test('unsupported capabilities are stated plainly for openai', async () => {
  const f = fixture({
    provider: 'openai', model: 'gpt-image-1',
    capabilities: {known: true, reference_image: false, seed: false, negative_prompt: false,
      size_mode: 'enum', auth_required: true, consistency_note: '这个接口不支持参考图与 seed：一致性较弱。'},
    notice: '当前提供方不支持参考图：界面上的立绘参考不会生效（不会假装生效）。',
  });
  await f.view.refresh();
  assert.match(f.root.textContent, /不支持（不生效）/, 'seed 行如实显示不生效');
  assert.match(f.root.textContent, /立绘参考不生效/);
  assert.match(f.root.textContent, /一致性较弱/);
  assert.match(f.root.textContent, /不会假装生效/);
});

test('damaged quota disables generation and displays the readable reason', async () => {
  const f = fixture({daily: {used: null, limit: 3, error: '次数账本损坏，已停止生成'}});
  await f.view.refresh();
  assert.equal(buttonOf(f).disabled, true);
  assert.match(f.root.textContent, /次数账本损坏/);
  await click(buttonOf(f));
  assert.equal(f.posts.length, 0);
});

test('disabled feature keeps the button off and explains where to enable', async () => {
  const f = fixture({enabled: false});
  await f.view.refresh();
  assert.ok(Object.hasOwn(buttonOf(f).attributes, 'disabled'));
  assert.match(f.root.textContent, /状态生图当前关闭/);
  f.ctx.apiPost = async (key, body) => { f.posts.push({key, body}); return {ok: false, error: 'x'}; };
  await click(buttonOf(f));
  assert.equal(f.posts.length, 0, '关闭时按钮点了也不发请求');
});

test('load error is shown without wiping the page', async () => {
  const f = fixture();
  f.ctx.apiGet = async () => { throw new Error('网络断了'); };
  await f.view.refresh();
  assert.match(f.root.textContent, /网络断了/);
  assert.ok(f.posts.length === 0);
});
