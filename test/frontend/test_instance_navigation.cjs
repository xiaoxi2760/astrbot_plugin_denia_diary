/* 三端整合 P0：前端实例（scope）选择与请求隔离。
   覆盖：导航/草稿/滚动按实例隔开、GET+POST 的 scope 固定、迟到响应不覆盖新实例、
   旧 context 不能写新实例、上传不拼 query、缺实例不能操作、legacy 模式完全不变。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attributes = {}; this.events = {};
    this.style = {}; this.dataset = {}; this.className = ''; this.scrollTop = 0; this.scrollLeft = 0;
    this.hidden = false; this.textContent = ''; this.value = '';
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { const i = this.children.indexOf(child); if (i >= 0) this.children.splice(i, 1); }
  get firstChild() { return this.children[0]; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, handler) { this.events[name] = handler; }
  removeEventListener() {}
  closest() { return null; }
  focus() {}
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

/* 两个实例，同一个 who（u_1001）——最容易被串的一档。 */
const INSTANCES = {
  ok: true, isolation_enabled: true, legacy_scope_id: 'legacy-1',
  default_scope_id: 'scope-a',
  items: [
    { scope_id: 'scope-a', label: '甲实例' },
    { scope_id: 'scope-b', label: '乙实例' },
    { scope_id: 'legacy-1', label: '旧实例', legacy: true },
  ],
};

async function fixture(options = {}) {
  const elements = new Map();
  const document = {
    readyState: 'complete', documentElement: new Element(),
    createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }),
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    querySelectorAll: () => [], addEventListener() {},
  };
  const storage = new Map();
  if (options.storedScope) storage.set('denia.diary.scope', options.storedScope);
  const gets = [];
  const posts = [];
  const uploads = [];
  const pending = new Map();
  const contexts = [];
  const instancePayload = options.instances === undefined ? INSTANCES : options.instances;

  const window = {
    location: { search: options.search || '' },
    localStorage: {
      getItem: key => (storage.has(key) ? storage.get(key) : ''),
      setItem: (key, value) => storage.set(key, String(value)),
    },
    FileReader: class {
      readAsDataURL(file) {
        this.result = 'data:image/png;base64,' + Buffer.from(file.name || 'x').toString('base64');
        if (this.onload) this.onload();
      }
    },
    AstrBotPluginPage: {
      ready: async () => {},
      apiGet: async (endpoint, params) => {
        gets.push({ endpoint, params });
        if (endpoint === 'instances') {
          if (options.instancesError) throw new Error(options.instancesError);
          return instancePayload;
        }
        if (pending.has(endpoint)) return pending.get(endpoint).promise;
        if (endpoint === 'settings' && options.settings) {
          return { ok: true, values: options.settings[params.scope_id || 'legacy'] || {} };
        }
        if (endpoint === 'status') {
          return { ok: true, who_options: [{ id: 'u_1001', name: '希', is_owner: true }] };
        }
        return { ok: true, values: {} };
      },
      apiPost: async (endpoint, body) => {
        posts.push({ endpoint, body });
        if (pending.has('POST:' + endpoint)) return pending.get('POST:' + endpoint).promise;
        return { ok: true };
      },
      upload: async (endpoint, file) => { uploads.push({ endpoint, file }); return { ok: true }; },
    },
  };
  for (const name of ['Overview', 'Diary', 'Settings']) {
    window['create' + name + 'View'] = ctx => {
      contexts.push(ctx);
      const content = new Element(); content.textContent = name;
      return {
        mount: body => body.appendChild(content), unmount() {},
        refresh: async () => {
          if (pending.has(name)) return pending.get(name).promise;
          if (name === 'Overview') {
            const status = await ctx.apiGet('status', { who: ctx.who() });
            ctx.mergeOptions(status.who_options);
          }
        },
      };
    };
  }
  const sandbox = { window, document, setTimeout, clearTimeout, Buffer, console };
  vm.createContext(sandbox);
  for (const file of ['ui.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);
  }
  await new Promise(resolve => setImmediate(resolve));
  return {
    panel: window.DiaryPanel, body: document.getElementById('stage-body'),
    document, gets, posts, uploads, pending, contexts, storage,
    scopePicker: document.getElementById('scope-picker'),
  };
}

test('the instance list comes from the backend items only', async () => {
  const f = await fixture();
  assert.ok(f.gets.some(g => g.endpoint === 'instances'));
  assert.equal(f.panel.state.instances.length, 3);
  assert.equal(f.panel.state.isolationEnabled, true);
  /* 默认落在有效默认 scope 上，不偷跑 legacy。 */
  assert.equal(f.panel.state.scopeId, 'scope-a');
  const values = f.scopePicker.children.map(o => o.attributes.value);
  assert.deepEqual(values, ['scope-a', 'scope-b', 'legacy-1']);
});

test('an explicit stored scope wins over the default', async () => {
  const f = await fixture({ storedScope: 'scope-b' });
  assert.equal(f.panel.state.scopeId, 'scope-b');
});

test('GET and POST carry the scope of the mounted context', async () => {
  const f = await fixture();
  const ctx = f.panel.ctxFor('scope-a');
  await ctx.apiGet('status', { who: 'u_1001' });
  await ctx.apiPost('diaryDelete', { id: 'x' });
  const get = f.gets.filter(g => g.endpoint === 'status').pop();
  const post = f.posts.filter(p => p.endpoint === 'diary/delete').pop();
  assert.equal(get.params.scope_id, 'scope-a');
  assert.equal(post.body.scope_id, 'scope-a');
});

test('an old context cannot write into the newly selected instance', async () => {
  const f = await fixture();
  const oldCtx = f.panel.ctxFor('scope-a');
  await f.panel.switchScope('scope-b');
  await assert.rejects(() => oldCtx.apiPost('diaryDelete', { id: 'x' }), /实例已切换/);
  const post = f.posts.filter(p => p.endpoint === 'diary/delete').pop();
  assert.equal(post, undefined, '旧 context 的写请求不该真的发出去');
});

test('a late response from the previous scope cannot overwrite the new scope view', async () => {
  const f = await fixture();
  const late = deferred();
  f.pending.set('Overview', late);
  const loading = f.panel.show('overview');
  f.pending.delete('Overview');
  await f.panel.switchScope('scope-b');
  late.resolve({ ok: true });
  await loading;
  /* 新实例的视图仍然是新挂载的那个，不是旧请求画出来的。 */
  assert.equal(f.body.firstChild.textContent, 'Overview');
});

test('switching scope clears the previous candidates and reloads', async () => {
  const f = await fixture();
  f.panel.ctxFor('scope-a').setWho('u_1001');
  f.panel.ctxFor('scope-a').mergeOptions([{ id: 'u_1001', name: 'personal only' }]);
  assert.ok(f.panel.state.options.length > 0);
  await f.panel.switchScope('scope-b');
  assert.equal(f.panel.state.scopeId, 'scope-b');
  /* 乙实例的候选人重新拉，不沿用甲实例的。 */
  const status = f.gets.filter(g => g.endpoint === 'status');
  assert.ok(status.length >= 1);
});

test('scroll position is kept per scope', async () => {
  const f = await fixture();
  await f.panel.show('diary'); f.body.scrollTop = 480;
  await f.panel.switchScope('scope-b');
  assert.equal(f.body.scrollTop, 0, '新实例从头看起');
  await f.panel.show('settings');
  await f.panel.switchScope('scope-a');
  await f.panel.show('diary');
  assert.equal(f.body.scrollTop, 480, '切回甲实例回到自己读到的位置');
});

test('drafts of the same who in two scopes never mix', async () => {
  const f = await fixture();
  const a = f.panel.ctxFor('scope-a');
  const b = f.panel.ctxFor('scope-b');
  a.state.diaryUI = a.state.diaryUI || {};
  a.state.diaryUI['scope-a|u_1001'] = { edits: { 'normal|s1': '甲的草稿' }, comments: {} };
  b.state.diaryUI = b.state.diaryUI || {};
  b.state.diaryUI['scope-b|u_1001'] = { edits: {}, comments: {} };
  assert.equal(a.state.diaryUI['scope-a|u_1001'].edits['normal|s1'], '甲的草稿');
  assert.equal(b.state.diaryUI['scope-b|u_1001'].edits['normal|s1'], undefined);
});

test('isolation upload goes through the JSON data endpoint and never appends a query', async () => {
  const f = await fixture();
  const ctx = f.panel.ctxFor('scope-a');
  await ctx.apiUpload('portraitUpload', { name: 'a.png', size: 10 });
  assert.equal(f.uploads.length, 0, '隔离模式不走官方 upload()');
  const post = f.posts.filter(p => p.endpoint === 'portrait/upload-data').pop();
  assert.ok(post, '隔离模式走 portrait/upload-data');
  assert.equal(post.body.scope_id, 'scope-a');
  assert.match(post.body.data_url, /^data:/);
  assert.ok(!String(post.endpoint).includes('?'), 'endpoint 禁止拼 query');
});

test('without a selected scope, instance business requests are refused', async () => {
  const f = await fixture({ instances: { ok: true, isolation_enabled: true, legacy_scope_id: 'legacy-1', items: [] } });
  assert.equal(f.panel.state.scopeId, '');
  const ctx = f.panel.ctxFor('');
  await assert.rejects(() => ctx.apiGet('status', {}), /还没选实例/);
  await assert.rejects(() => ctx.apiPost('diaryDelete', {}), /还没选实例/);
});

test('legacy mode keeps the old behaviour: no scope_id anywhere', async () => {
  const f = await fixture({ instances: { ok: true, isolation_enabled: false, legacy_scope_id: '', items: [] } });
  assert.equal(f.panel.state.isolationEnabled, false);
  assert.equal(f.panel.state.scopeId, '');
  const ctx = f.panel.ctxFor('');
  await ctx.apiGet('status', { who: 'u_1001' });
  await ctx.apiPost('diaryDelete', { id: 'x' });
  const get = f.gets.filter(g => g.endpoint === 'status').pop();
  const post = f.posts.filter(p => p.endpoint === 'diary/delete').pop();
  assert.equal(get.params.scope_id, undefined);
  assert.equal(post.body.scope_id, undefined);
});

test('legacy mode still uploads through the official bridge upload', async () => {
  const f = await fixture({ instances: { ok: true, isolation_enabled: false, items: [] } });
  const ctx = f.panel.ctxFor('');
  await ctx.apiUpload('portraitUpload', { name: 'a.png', size: 10 });
  assert.equal(f.uploads.length, 1);
  assert.equal(f.uploads[0].endpoint, 'portrait/upload');
  assert.equal(f.posts.filter(p => p.endpoint === 'portrait/upload-data').length, 0);
});

test('the endpoint table stays reconcilable with the backend routes', async () => {
  const f = await fixture();
  assert.equal(f.panel.ENDPOINTS.instances, 'instances');
  assert.equal(f.panel.ENDPOINTS.portraitUploadData, 'portrait/upload-data');
});

test('a response from a previous visit is stale after switching away and back', async () => {
  const f = await fixture();
  const old = deferred(); f.pending.set('history', old);
  const response = f.panel.ctxFor('scope-a').apiGet('history', {});
  const rejected = assert.rejects(response, /实例已切换/);
  await f.panel.switchScope('scope-b');
  await f.panel.switchScope('scope-a');
  old.resolve({ private: 'old visit' });
  await rejected;
});

test('an unavailable instance registry does not start unscoped business requests', async () => {
  const f = await fixture({ instancesError: 'network unavailable' });
  assert.equal(f.gets.length, 1);
  assert.equal(f.gets[0].endpoint, 'instances');
  assert.equal(f.posts.length, 0);
});

function scopeButton(f, value) {
  return f.document.getElementById('scope-bar').firstChild.children
    .find(button => button.getAttribute('data-scope') === value);
}
async function pickMode(f, value) {
  scopeButton(f, value).events.click();
  await new Promise(resolve => setImmediate(resolve));
}

test('startup loads the selected instance settings for the header and rename editor', async () => {
  const f = await fixture({ settings: { 'scope-a': {
    scope: { mode: 'owner' }, panel: { brand: '甲的日记' }, name_preference: { u_1001: '旧名字' },
  } } });
  assert.ok(f.gets.some(g => g.endpoint === 'settings' && g.params.scope_id === 'scope-a'));
  assert.equal(f.panel.state.scope, 'owner');
  assert.equal(scopeButton(f, 'owner').getAttribute('aria-pressed'), 'true');
  assert.equal(f.document.getElementById('brand').textContent, '甲的日记');
  f.panel.ctxFor('scope-a').setWho('u_1001');
  f.document.getElementById('who-name-edit').hidden = true;
  f.document.getElementById('who-rename').events.click();
  assert.equal(f.document.getElementById('who-name-input').value, '旧名字');
});

test('the actual header control posts its instance and reloads status', async () => {
  const f = await fixture();
  const count = f.gets.filter(g => g.endpoint === 'status').length;
  await pickMode(f, 'all');
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].endpoint, 'settings');
  assert.equal(f.posts[0].body.scope_id, 'scope-a');
  assert.equal(f.posts[0].body.changes['scope.mode'], 'all');
  assert.equal(f.panel.state.scope, 'all');
  assert.ok(f.gets.filter(g => g.endpoint === 'status').length > count);
  assert.ok(f.gets.filter(g => g.endpoint === 'status').every(g => g.params.scope_id === 'scope-a'));
  assert.doesNotMatch(f.document.getElementById('toast').textContent, /not defined|没切/);
});

test('a failed header write rolls back the current instance control', async () => {
  const f = await fixture();
  const result = deferred(); f.pending.set('POST:settings', result);
  await pickMode(f, 'all');
  result.resolve({ ok: false, error: '保存失败' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.posts.length, 1);
  assert.equal(f.panel.state.scope, 'private');
  assert.equal(scopeButton(f, 'private').getAttribute('aria-pressed'), 'true');
  assert.match(f.document.getElementById('toast').textContent, /保存失败/);
});

test('a late header write cannot roll back the next instance mode', async () => {
  const f = await fixture({ settings: { 'scope-b': { scope: { mode: 'owner' } } } });
  const result = deferred(); f.pending.set('POST:settings', result);
  await pickMode(f, 'all');
  await f.panel.switchScope('scope-b');
  result.resolve({ ok: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.panel.state.scope, 'owner');
  assert.equal(scopeButton(f, 'owner').getAttribute('aria-pressed'), 'true');
  assert.equal(f.posts[0].body.scope_id, 'scope-a');
});

test('renaming merges existing names and writes only the selected instance', async () => {
  const f = await fixture({ settings: { 'scope-a': { name_preference: { u_2002: '保留' } } } });
  f.panel.ctxFor('scope-a').setWho('u_1001');
  await new Promise(resolve => setImmediate(resolve));
  f.document.getElementById('who-name-input').value = '新名字';
  await f.document.getElementById('who-name-ok').events.click();
  assert.equal(f.posts.length, 1);
  const body = f.posts[0].body;
  assert.equal(body.scope_id, 'scope-a');
  assert.equal(body.changes.name_preference.u_2002, '保留');
  assert.equal(body.changes.name_preference.u_1001, '新名字');
  assert.equal(f.panel.state.options.find(o => o.id === 'u_1001').name, '新名字');
});

test('a rename reply from a previous visit cannot alter names after switching away and back', async () => {
  const f = await fixture({ settings: { 'scope-a': { name_preference: { u_1001: '服务器名字' } } } });
  f.panel.ctxFor('scope-a').setWho('u_1001');
  await new Promise(resolve => setImmediate(resolve));
  const result = deferred(); f.pending.set('POST:settings', result);
  f.document.getElementById('who-name-input').value = '旧请求的名字';
  const saving = f.document.getElementById('who-name-ok').events.click();
  await f.panel.switchScope('scope-b');
  await f.panel.switchScope('scope-a');
  f.document.getElementById('who-name-edit').hidden = true;
  f.document.getElementById('who-rename').events.click();
  result.resolve({ ok: true }); await saving;
  assert.equal(f.document.getElementById('who-name-edit').hidden, false);
  assert.equal(f.document.getElementById('who-name-input').value, '服务器名字');
  assert.notEqual(f.panel.state.options.find(o => o.id === 'u_1001').name, '旧请求的名字');
  assert.doesNotMatch(f.document.getElementById('toast').textContent, /没改成/);
});

test('legacy header settings remain unscoped', async () => {
  const f = await fixture({ instances: { ok: true, isolation_enabled: false, items: [] },
    settings: { legacy: { scope: { mode: 'owner' } } } });
  assert.equal(f.panel.state.scope, 'owner');
  await pickMode(f, 'all');
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].body.scope_id, undefined);
  assert.equal(f.posts[0].body.changes['scope.mode'], 'all');
});
