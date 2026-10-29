/* 桌宠设备卡交互测试：一次性凭据、异步竞态、双击保护和撤销错误反馈。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class TextNode {
  constructor(value) { this.textContent = String(value); this.parentNode = null; }
}

class Element {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.attributes = {};
    this.children = [];
    this.events = {};
    this.style = {};
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.parentNode = null;
    this.className = '';
    this.selected = false;
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === 'value') this.value = String(value);
    if (key === 'disabled') this.disabled = true;
    if (key === 'readonly') this.readOnly = true;
  }
  getAttribute(key) { return this.attributes[key]; }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  get firstChild() { return this.children[0] || null; }
  set textContent(value) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this._text = String(value);
  }
  get textContent() {
    return (this._text || '') + this.children.map(child => child.textContent || '').join('');
  }
  addEventListener(name, handler) { this.events[name] = handler; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  async emit(name) {
    const handler = this.events[name];
    if (handler) return handler({ target: this, preventDefault() {} });
  }
}

function nodes(root) {
  return [root, ...(root.children || []).flatMap(child => child instanceof Element ? nodes(child) : [])];
}
function byAttr(root, attr, value) {
  return nodes(root).find(node => node.getAttribute && node.getAttribute(attr) === value);
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function flush() { return new Promise(resolve => setImmediate(resolve)); }

const LIST = {
  ok: true,
  owners: ['owner-10001'],
  agent_id: 'yaya',
  listener: { running: false, host: '127.0.0.1', port: 6196, error: '' },
  devices: [{ name: '旧桌宠', device_id: 'dev-old', agent_id: 'yaya',
    principal_id: 'owner-10001', created_at: '2026-10-27T09:00:00+08:00', revoked: false,
    credential: 'MUST_NOT_BE_RENDERED_FROM_LIST' }],
};

async function fixture(options = {}) {
  const document = {
    createElement: tag => new Element(tag),
    createTextNode: value => new TextNode(value),
    getElementById: () => null,
  };
  const clipboardWrites = [];
  const window = options.clipboard ? { navigator: { clipboard: { writeText: async value => clipboardWrites.push(value) } } } : {};
  const sandbox = { window, document, setTimeout, clearTimeout, console };
  vm.createContext(sandbox);
  const base = path.join(__dirname, '../../pages/diary');
  for (const file of ['ui.js', 'views/pet-devices.js']) {
    vm.runInContext(fs.readFileSync(path.join(base, file), 'utf8'), sandbox, { filename: file });
  }

  const gets = [], posts = [];
  const ctx = {
    UI: window.UI,
    apiGet: async (key, params) => {
      gets.push({ key, params });
      return options.get ? options.get(key, params) : LIST;
    },
    apiPost: async (key, body) => {
      posts.push({ key, body });
      if (options.post) return options.post(key, body);
      return { ok: true };
    },
  };
  const root = new Element('main');
  const view = window.DiaryViews.PetDevices(ctx);
  view.mount(root);
  await view.refresh();
  await flush();
  return { root, view, gets, posts, clipboardWrites, ctx };
}

async function setName(root, value) {
  const input = nodes(root).find(node => node.getAttribute && node.getAttribute('aria-label') === '设备名称');
  assert.ok(input, 'device name field exists');
  input.value = value;
  await input.emit('input');
}

test('issue shows one-time credential; silent list read preserves it and manual refresh hides it', async () => {
  const credential = 'one-time-secret-123';
  const f = await fixture({ clipboard: true, post: async () => ({
    ok: true, device_id: 'dev-new', agent_id: 'yaya', credential,
  }) });
  assert.match(f.root.textContent, /127\.0\.0\.1:6196/);
  assert.match(f.root.textContent, /dev-old/);
  assert.doesNotMatch(f.root.textContent, /MUST_NOT_BE_RENDERED_FROM_LIST/);

  await setName(f.root, 'Win 桌宠');
  assert.equal(nodes(f.root).find(node => node.getAttribute && node.getAttribute('aria-label') === '设备名称').getAttribute('maxlength'), '60');
  const issueButton = byAttr(f.root, 'data-act', 'pet-device-issue');
  assert.ok(issueButton && !issueButton.disabled, 'issue should enable after name and owner are selected: ' + f.root.textContent);
  await issueButton.emit('click');
  await flush();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].key, 'petDeviceIssue');
  assert.equal(f.posts[0].body.name, 'Win 桌宠');
  assert.equal(f.posts[0].body.principal_id, 'owner-10001');
  assert.equal(f.clipboardWrites.length, 0, 'credentials are never copied automatically');

  let field = nodes(f.root).find(node => node.tagName === 'TEXTAREA');
  assert.ok(field, 'issued credential is visible in a text field');
  assert.equal(field.value, credential);
  assert.equal(field.readOnly, true);
  const deviceId = nodes(f.root).find(node => node.getAttribute && node.getAttribute('aria-label') === '签发设备 device_id');
  const agentId = nodes(f.root).find(node => node.getAttribute && node.getAttribute('aria-label') === '签发设备 agent_id');
  assert.ok(deviceId && agentId, 'issue response binding identifiers are displayed beside the credential');
  assert.equal(deviceId.value, 'dev-new');
  assert.equal(agentId.value, 'yaya');
  assert.equal(deviceId.readOnly, true);
  assert.equal(agentId.readOnly, true);
  assert.match(f.root.textContent, /显示这一次/);
  assert.ok(f.gets.filter(call => call.key === 'petDevices').length >= 3,
    'initial, readiness and post-issue list reads ran');

  /* The copy action is explicit; the component never writes to the clipboard while issuing. */
  const copyButton = nodes(f.root).find(node => node.tagName === 'BUTTON' && node.textContent === '复制凭据');
  assert.ok(copyButton);
  await copyButton.emit('click');
  assert.deepEqual(f.clipboardWrites, [credential], 'clipboard is only used after an explicit click');

  await f.view.refresh({ manual: true });
  assert.equal(nodes(f.root).some(node => node.tagName === 'TEXTAREA'), false,
    'user-requested list refresh immediately removes the raw credential');
  assert.doesNotMatch(f.root.textContent, new RegExp(credential));
  f.view.unmount();
});

test('unmount discards a late list response and a late credential response', async () => {
  const listWait = deferred();
  const document = { createElement: tag => new Element(tag), createTextNode: value => new TextNode(value), getElementById: () => null };
  const window = {};
  const sandbox = { window, document, setTimeout, clearTimeout, console };
  vm.createContext(sandbox);
  const base = path.join(__dirname, '../../pages/diary');
  for (const file of ['ui.js', 'views/pet-devices.js']) vm.runInContext(fs.readFileSync(path.join(base, file), 'utf8'), sandbox);
  const root = new Element('main');
  const view = window.DiaryViews.PetDevices({ UI: window.UI, apiGet: () => listWait.promise, apiPost: async () => ({ ok: true }) });
  view.mount(root);
  view.unmount();
  listWait.resolve(LIST);
  await flush();
  assert.equal(root.textContent, '', 'late list data cannot restore an unmounted card');

  const postWait = deferred();
  const f = await fixture({ post: () => postWait.promise });
  await setName(f.root, '迟到的桌宠');
  const pendingIssue = byAttr(f.root, 'data-act', 'pet-device-issue').emit('click');
  f.view.unmount();
  postWait.resolve({ ok: true, credential: 'must-never-render-after-unmount', device_id: 'dev-late', agent_id: 'yaya' });
  await pendingIssue;
  assert.equal(f.root.textContent, '');
  assert.doesNotMatch(f.root.textContent, /must-never-render-after-unmount/);
});

test('a list read started before issue cannot replace the newer owner list or issued credential', async () => {
  const f = await fixture({ post: async () => ({ ok: true, credential: 'newer-credential', device_id: 'dev-new', agent_id: 'yaya' }) });
  await setName(f.root, '竞态设备');
  const staleRead = deferred();
  let reads = 0;
  f.ctx.apiGet = async () => {
    reads += 1;
    return reads === 1 ? staleRead.promise : LIST;
  };
  const pendingRead = f.view.refresh({ manual: true });
  const pendingIssue = byAttr(f.root, 'data-act', 'pet-device-issue').emit('click');
  await pendingIssue;
  assert.match(f.root.textContent, /newer-credential/);
  staleRead.resolve(Object.assign({}, LIST, { owners: [], devices: [] }));
  await pendingRead;
  assert.match(f.root.textContent, /newer-credential/);
  assert.match(f.root.textContent, /owner-10001/);
  f.view.unmount();
});

test('issued device and agent ids stay visible when the post-issue list refresh fails', async () => {
  let reads = 0;
  const f = await fixture({
    get: async () => {
      reads += 1;
      if (reads > 2) throw new Error('list refresh failed');
      return LIST;
    },
    post: async () => ({ ok: true, credential: 'credential-without-list', device_id: 'dev-from-issue', agent_id: 'agent-from-issue' }),
  });
  await setName(f.root, '列表刷新失败设备');
  await byAttr(f.root, 'data-act', 'pet-device-issue').emit('click');
  assert.match(f.root.textContent, /设备列表读取失败：list refresh failed/);
  assert.match(f.root.textContent, /credential-without-list/);
  const deviceId = nodes(f.root).find(node => node.getAttribute && node.getAttribute('aria-label') === '签发设备 device_id');
  const agentId = nodes(f.root).find(node => node.getAttribute && node.getAttribute('aria-label') === '签发设备 agent_id');
  assert.equal(deviceId.value, 'dev-from-issue');
  assert.equal(agentId.value, 'agent-from-issue');
  f.view.unmount();
});

test('issue action locks immediately and ignores a second invocation while pending', async () => {
  const postWait = deferred();
  const f = await fixture({ post: () => postWait.promise });
  await setName(f.root, '双击测试');
  const firstButton = byAttr(f.root, 'data-act', 'pet-device-issue');
  const pending = firstButton.emit('click');
  const busyButton = byAttr(f.root, 'data-act', 'pet-device-issue');
  assert.ok(busyButton.disabled, 'the rendered button locks while the request is pending');
  await busyButton.emit('click');
  assert.equal(f.posts.length, 1, 'even a synthetic second click cannot submit twice');
  postWait.resolve({ ok: true, credential: 'one-secret', device_id: 'dev-new', agent_id: 'yaya' });
  await pending;
  assert.equal(f.posts.length, 1);
  f.view.unmount();
});

test('revoke uses inline two-step confirmation and keeps the device after failure', async () => {
  const f = await fixture({ post: async () => ({ ok: false, error: '服务端拒绝撤销' }) });
  const revoke = byAttr(f.root, 'data-act', 'pet-device-revoke');
  assert.ok(revoke);
  assert.equal(revoke.getAttribute('data-device-id'), 'dev-old');
  await revoke.emit('click');
  assert.match(f.root.textContent, /再次点击“确认撤销”/);
  assert.ok(byAttr(f.root, 'data-act', 'pet-device-revoke').textContent === '确认撤销');
  await byAttr(f.root, 'data-act', 'pet-device-revoke').emit('click');
  await flush();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].key, 'petDeviceRevoke');
  assert.equal(f.posts[0].body.device_id, 'dev-old');
  assert.match(f.root.textContent, /撤销失败：服务端拒绝撤销/);
  assert.match(f.root.textContent, /dev-old/, 'failed revoke keeps the paired device visible');
  f.view.unmount();
});

test('revoking the currently issued device clears the credential and its DOM immediately', async () => {
  const afterRevokeRead = deferred();
  let reads = 0;
  const f = await fixture({
    get: async () => {
      reads += 1;
      return reads === 4 ? afterRevokeRead.promise : LIST;
    },
    post: async key => key === 'petDeviceIssue'
      ? { ok: true, credential: 'credential-to-revoke', device_id: 'dev-old', agent_id: 'yaya' }
      : { ok: true },
  });
  await setName(f.root, '即撤回设备');
  await byAttr(f.root, 'data-act', 'pet-device-issue').emit('click');
  assert.match(f.root.textContent, /credential-to-revoke/);

  await byAttr(f.root, 'data-act', 'pet-device-revoke').emit('click');
  await byAttr(f.root, 'data-act', 'pet-device-revoke').emit('click');
  await flush();
  assert.equal(nodes(f.root).some(node => node.tagName === 'TEXTAREA'), false,
    'credential field is removed before waiting for the follow-up device list');
  assert.equal(nodes(f.root).some(node => node.getAttribute && node.getAttribute('aria-label') === '签发设备 device_id'), false);
  assert.doesNotMatch(f.root.textContent, /credential-to-revoke/);
  afterRevokeRead.resolve(LIST);
  await flush();
  f.view.unmount();
});

test('copy button falls back to selecting read-only text when clipboard is unavailable', async () => {
  const f = await fixture({ post: async () => ({ ok: true, credential: 'manual-copy-me', device_id: 'dev-new', agent_id: 'yaya' }) });
  await setName(f.root, '手动复制');
  await byAttr(f.root, 'data-act', 'pet-device-issue').emit('click');
  const field = nodes(f.root).find(node => node.tagName === 'TEXTAREA');
  await nodes(f.root).find(node => node.tagName === 'BUTTON' && node.textContent === '复制凭据').emit('click');
  assert.equal(field.selected, true);
  assert.match(f.root.textContent, /文本已选中，请手动复制/);
  f.view.unmount();
});

test('issue stays unavailable until the configured closest-owner list has a principal', async () => {
  const f = await fixture({ get: async () => Object.assign({}, LIST, { owners: [], devices: [] }) });
  await setName(f.root, '无主人设备');
  assert.match(f.root.textContent, /请先配置最亲密名单/);
  assert.ok(byAttr(f.root, 'data-act', 'pet-device-issue').disabled);
  assert.equal(f.posts.length, 0);
  f.view.unmount();
});
