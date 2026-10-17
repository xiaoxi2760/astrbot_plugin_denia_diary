const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.attributes = {}; this.children = []; this.events = {}; this.style = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.open = false; }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'disabled') this.disabled = true; }
  getAttribute(key) { return this.attributes[key]; }
  appendChild(child) { if (child.parentNode) child.remove(); this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get firstChild() { return this.children[0]; }
  addEventListener(name, handler) { this.events[name] = handler; }
  contains(target) { return this === target || this.children.some(child => child.contains && child.contains(target)); }
  querySelector(tag) { return nodes(this, tag)[0] || null; }
  focus() { this.focused = true; }
}
function nodes(root, tag) { return (root.children || []).flatMap(child => [...(child.tagName === tag ? [child] : []), ...nodes(child, tag)]); }
function allText(root) { return [root.textContent || '', ...(root.children || []).map(allText)].join(' '); }
function button(root, text) { return nodes(root, 'button').find(n => n.textContent === text); }
function click(root, text) { const node = button(root, text); assert.ok(node, 'missing button: ' + text); return node.events.click(); }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }

async function fixture(options = {}) {
  const document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: text => ({ textContent: text }), getElementById: () => null };
  const sandbox = { window: { addEventListener() {}, removeEventListener() {} }, document, setTimeout, clearTimeout };
  vm.createContext(sandbox);
  for (const file of ['ui.js', 'views/diary.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);
  const calls = [], posts = [];
  let items = options.items || [];
  let getHandler = options.get;
  let postHandler = options.post;
  const context = {
    UI: sandbox.window.UI, who: () => 'owner', state: options.state || {}, toast() {},
    apiGet: async (key, payload) => {
      calls.push({ key, payload });
      if (getHandler) { const result = await getHandler(key, payload); if (result !== undefined) return result; }
      if (key === 'diaryList') return { books: [{ book: 'normal', display: '日记', entries: 1, days: [], latest_date: '2026-10-16' }] };
      if (key === 'diaryContent') return { entries: [{ date: '2026-10-16', text: '原始日记正文', seg_id: options.noSeg ? undefined : 'seg-1' }], edit_window: {}, total: 1 };
      if (key === 'diaryTrash') return { items: [] };
      if (key === 'diaryComments') return { ok: true, items: structuredClone(items) };
      throw new Error('unexpected endpoint ' + key);
    },
    apiPost: async (key, payload) => {
      posts.push({ key, payload });
      if (postHandler) return await postHandler(key, payload);
      if (key === 'diaryCommentSave') {
        const draft = items.find(row => row.status === 'draft');
        if (draft) draft.text = payload.text;
        else items.push({ id: 'c' + items.length, text: payload.text, status: 'draft', source_valid: true, publishable: true });
      } else {
        const row = items.find(row => row.id === payload.id);
        row.status = { diaryCommentPublish: 'pending', diaryCommentWithdraw: 'withdrawn', diaryCommentDelete: 'deleted' }[key];
      }
      return { ok: true, entry: items.at(-1) };
    },
  };
  const holder = new Element('main');
  const view = sandbox.window.createDiaryView(context);
  view.mount(holder);
  await view.refresh();
  function detail() { return nodes(holder, 'details')[0]; }
  async function open() { const box = detail(); box.open = true; await box.events.toggle(); return box; }
  return { view, holder, calls, posts, items, context, detail, open, setGet: f => { getHandler = f; } };
}

test('comments stay folded and lazy, with no entry point for legacy segments', async () => {
  const f = await fixture();
  assert.ok(f.detail());
  assert.equal(f.calls.filter(c => c.key === 'diaryComments').length, 0);
  await f.open();
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls.at(-1))), { key: 'diaryComments', payload: { book: 'normal', seg_id: 'seg-1' } });
  assert.ok(button(f.detail(), '保存草稿'));
  assert.match(allText(f.holder), /原始日记正文/);
  const legacy = await fixture({ noSeg: true });
  assert.equal(legacy.detail(), undefined);
});

test('save draft is separate from publish; publish, withdraw and delete repaint their actual status', async () => {
  const f = await fixture();
  const box = await f.open();
  const area = nodes(box, 'textarea')[0];
  area.value = '我看到了，明天陪你聊'; area.events.input();
  await click(box, '保存草稿');
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].key, 'diaryCommentSave');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].payload)), { book: 'normal', seg_id: 'seg-1', text: '我看到了，明天陪你聊' });
  assert.match(allText(box), /草稿已保存/);
  assert.ok(button(box, '让她知道'));
  await click(box, '让她知道');
  assert.match(allText(box), /待交给她/);
  assert.equal(button(box, '让她知道'), undefined);
  await click(box, '撤回');
  assert.match(allText(box), /已撤回/);
  assert.ok(button(box, '编辑为新草稿'));
  await click(box, '删除留言');
  assert.equal(button(box, '删除留言'), undefined);
});

test('published/consumed text is reused only through a new saved draft and displayed as plain text', async () => {
  const text = '<img src=x onerror=alert(1)> 你说的话我记住了';
  const f = await fixture({ items: [{ id: 'old', text, status: 'retired', source_valid: true, publishable: true }] });
  const box = await f.open();
  assert.match(allText(box), /已交给她/);
  assert.equal(nodes(box, 'img').length, 0);
  await click(box, '编辑为新草稿');
  assert.equal(nodes(box, 'textarea')[0].value, text);
  await click(box, '保存草稿');
  assert.equal(f.items[0].status, 'retired');
  assert.equal(f.items[1].status, 'draft');
  assert.ok(button(box, '让她知道'));
});

test('invalid sources and entries without private ownership never get publish controls', async () => {
  for (const data of [
    { source_valid: false, publishable: true },
    { source_valid: true, publishable: false },
    { source_valid: undefined, publishable: undefined },
  ]) {
    const f = await fixture({ items: [{ id: 'c', text: 'test', status: 'draft', ...data }] });
    const box = await f.open();
    assert.equal(button(box, '让她知道'), undefined);
    assert.ok(button(box, '保存草稿'));
    assert.match(allText(box), /不能发布|暂不能交给她/);
  }
});

test('unicode input is limited to 300 characters, and blank or oversized saves do not post', async () => {
  const f = await fixture();
  const box = await f.open();
  let area = nodes(box, 'textarea')[0];
  area.value = '😀'.repeat(301); area.events.input();
  assert.equal(Array.from(area.value).length, 300);
  assert.match(allText(box), /300\/300 字/);
  area.value = '字'.repeat(301);
  await click(box, '保存草稿');
  assert.equal(f.posts.length, 0);
  assert.match(allText(box), /最多 300 字/);
  area = nodes(box, 'textarea')[0]; area.value = '  ';
  await click(box, '保存草稿');
  assert.equal(f.posts.length, 0);
  assert.match(allText(box), /留言不能为空/);
});

test('GET/POST failures are readable and an ambiguous refresh requires a fresh read', async () => {
  let failRead = true;
  const f = await fixture({ get: async key => { if (key === 'diaryComments' && failRead) throw new Error('网络断开'); } });
  const box = await f.open();
  assert.match(allText(box), /读取留言失败：网络断开/);
  failRead = false;
  await click(box, '重试读取留言');
  assert.ok(button(box, '保存草稿'));
  nodes(box, 'textarea')[0].value = '保留这句';
  f.setGet(async key => { if (key === 'diaryComments') throw new Error('刷新超时'); });
  await click(box, '保存草稿');
  assert.match(allText(box), /操作已保存，但刷新留言失败/);
  assert.equal(button(box, '保存草稿'), undefined);
  assert.ok(button(box, '重试读取留言'));
  const rejected = await fixture({ post: async () => ({ ok: false, error: '日记已改写，请刷新' }) });
  const rejectedBox = await rejected.open();
  nodes(rejectedBox, 'textarea')[0].value = '这条草稿';
  await click(rejectedBox, '保存草稿');
  assert.match(allText(rejectedBox), /日记已改写，请刷新/);
  assert.equal(nodes(rejectedBox, 'textarea')[0].value, '这条草稿');
});

test('mutation double clicks are locked and unmounted comment responses never repaint detached nodes', async () => {
  const pending = deferred();
  const f = await fixture({ post: () => pending.promise });
  const box = await f.open();
  nodes(box, 'textarea')[0].value = 'one';
  const save = button(box, '保存草稿');
  const operation = save.events.click();
  save.events.click();
  assert.equal(f.posts.length, 1);
  assert.ok(button(box, '保存草稿').disabled);
  const before = allText(box);
  f.view.unmount();
  pending.resolve({ ok: true });
  await operation;
  assert.equal(allText(box), before);
  assert.equal(f.calls.filter(c => c.key === 'diaryComments').length, 1);
});

test('a late comment GET cannot modify a page that has been replaced, and diary date navigation is preserved', async () => {
  const f = await fixture({ state: { diaryDate: '2026-10-12' } });
  assert.equal(f.calls.find(c => c.key === 'diaryContent').payload.date, '2026-10-12');
  assert.equal(f.context.state.diaryDate, undefined);
  const pending = deferred();
  f.setGet(async key => { if (key === 'diaryComments') return pending.promise; });
  const oldBox = f.detail(); oldBox.open = true;
  const loading = oldBox.events.toggle();
  await f.view.refresh();
  const before = allText(oldBox);
  pending.resolve({ ok: true, items: [{ id: 'late', text: 'late reply', status: 'draft', source_valid: true, publishable: true }] });
  await loading;
  assert.equal(allText(oldBox), before);
  assert.doesNotMatch(allText(f.holder), /late reply/);
});
