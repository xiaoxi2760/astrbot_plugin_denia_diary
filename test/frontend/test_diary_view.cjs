/* 日记页交互（第 19/20 步）：单本常驻切换、按人记忆、异步纪律、按需回收站。

   这些是**交互**测试，不是接口测试：钉的是"用户点一串按钮之后看到什么、
   发了哪些请求"，以及最要命的竞态——旧响应晚到时不能盖掉新选择。 */
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
  replaceChild(next, old) {
    const at = this.children.indexOf(old);
    assert.notEqual(at, -1, 'replaceChild: old node is not a child');
    this.children[at] = next;
    if (next.parentNode) next.remove();
    next.parentNode = this;
    old.parentNode = null;
    return old;
  }
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
function byAttr(root, name, value) { return nodes(root, 'button').find(n => n.getAttribute(name) === value); }
function click(root, text) { const node = button(root, text); assert.ok(node, 'missing button: ' + text); return node.events.click(); }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
/* 让挂起的 promise 链跑到"该画的都画了"：refresh() 里先渲按钮、后发正文请求。 */
function flush() { return new Promise(done => setTimeout(done, 0)); }
async function until(predicate, what) {
  for (let i = 0; i < 20; i += 1) {
    if (predicate()) return;
    await flush();
  }
  assert.fail('timed out waiting for: ' + what);
}

const OWNER = 'owner-id';
const FRIEND = 'friend-id';

/* 两本都有内容，且恋爱日记有篇数——主人应当直接落在恋爱本上。 */
const BOOKS = [
  { book: 'normal', display: '日记', is_love: false, entries: 12, chars: 900, latest_date: '2026-10-16', days: [{ date: '2026-10-16', count: 2, chars: 80 }], first_date: '2026-10-16' },
  { book: 'love', display: '恋爱日记', is_love: true, entries: 5, chars: 400, latest_date: '2026-10-15', days: [{ date: '2026-10-15', count: 1, chars: 90 }], first_date: '2026-10-15' },
];

async function fixture(options = {}) {
  const document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: text => ({ textContent: text }), getElementById: () => null, body: new Element('body') };
  const sandbox = { window: { addEventListener() {}, removeEventListener() {} }, document, setTimeout, clearTimeout };
  vm.createContext(sandbox);
  for (const file of ['ui.js', 'views/diary.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);

  const calls = [], posts = [];
  let getHandler = options.get;
  let postHandler = options.post;
  /* ctx.state 是 app.js 那个跨视图的 state；这里换成可变 who，模拟换人。 */
  const state = Object.assign({ options: [
    { id: OWNER, name: '希', label: '希(1)', is_owner: true, out_of_scope: false },
    { id: FRIEND, name: '小满', label: '小满(2)', is_owner: false, out_of_scope: false },
  ] }, options.state || {});
  let who = options.who || OWNER;

  const context = {
    UI: sandbox.window.UI,
    who: () => who,
    state,
    toast() {},
    apiGet: async (key, payload) => {
      calls.push({ key, payload });
      if (getHandler) { const result = await getHandler(key, payload); if (result !== undefined) return result; }
      if (key === 'diaryList') return { ok: true, books: BOOKS };
      if (key === 'diaryContent') {
        const book = payload.book;
        const date = payload.date;
        if (book === 'love') return { entries: [{ date: date || '2026-10-15', text: '恋爱正文', time: '21:00', seg_id: 'love-seg' }], edit_window: { can_edit: true, unlimited: true }, total: 5, count: 1 };
        return { entries: [{ date: date || '2026-10-16', text: '普通正文', time: '20:00', seg_id: 'normal-seg' }], edit_window: { can_edit: true, unlimited: true }, total: 12, count: 1 };
      }
      if (key === 'diaryTrash') return { ok: true, items: [], count: 0, cap: 30 };
      if (key === 'diaryComments') return { ok: true, items: [] };
      if (key === 'diaryCard') return { ok: true, html: '<p>卡片正文</p>', days: ['2026-10-15'], index: 0, date: '2026-10-15', style: 'paper', styles: ['paper'], style_labels: { paper: '纸' } };
      throw new Error('unexpected endpoint ' + key);
    },
    apiPost: async (key, payload) => {
      posts.push({ key, payload });
      if (postHandler) return await postHandler(key, payload);
      return { ok: true };
    },
  };

  const holder = new Element('main');
  const view = sandbox.window.createDiaryView(context);
  view.mount(holder);
  if (options.autoRefresh !== false) await view.refresh();
  return {
    view, holder, calls, posts, context, state,
    setWho(next) { who = next; },
    setGet(f2) { getHandler = f2; },
    trashCalls: () => calls.filter(c => c.key === 'diaryTrash').length,
    contentCalls: () => calls.filter(c => c.key === 'diaryContent'),
    bookButtons: () => nodes(holder, 'button').filter(b => b.getAttribute('data-book')),
    activeBook: () => (byAttr(holder, 'data-book', 'love').className.includes('is-on') ? 'love'
      : byAttr(holder, 'data-book', 'normal').className.includes('is-on') ? 'normal' : ''),
    viewButtons: () => nodes(holder, 'button').filter(b => b.getAttribute('data-view')),
    activeView: () => (byAttr(holder, 'data-view', 'card').className.includes('is-on') ? 'card' : 'text'),
  };
}

test('主人首次直接落在恋爱日记上，不需要先展开', async () => {
  const f = await fixture();
  assert.equal(f.activeBook(), 'love');
  assert.equal(f.contentCalls()[0].payload.book, 'love');
  /* 两本都在顶栏常驻，不用点"展开" */
  assert.deepEqual(f.bookButtons().map(b => b.getAttribute('data-book')), ['normal', 'love']);
  assert.match(allText(f.holder), /恋爱正文/);
});

test('本子的选择会被记住：切回普通日记再刷新，仍停在普通日记', async () => {
  const f = await fixture();
  await byAttr(f.holder, 'data-book', 'normal').events.click();
  assert.equal(f.activeBook(), 'normal');
  assert.match(allText(f.holder), /普通正文/);
  /* 模拟切 tab 回来：同一个 ctx.state，全新一个视图 */
  const fresh = await fixture({ state: f.state });
  assert.equal(fresh.activeBook(), 'normal');
  assert.match(allText(fresh.holder), /普通正文/);
});

test('选中的日期、月份和显示方式都按人记住', async () => {
  const f = await fixture();
  /* 点日历上 2026-10-15 那天（恋爱本的 days 只有这一天） */
  const cell = nodes(f.holder, 'div').find(n => n.className && n.className.startsWith('cal-cell') && n.className.includes('has-diary'));
  assert.ok(cell, 'expected a diary day cell');
  await cell.events.click();
  assert.equal(f.state.diaryUI[OWNER].per.love.date, '2026-10-15');
  /* 切到日记本（卡片）模式 */
  await byAttr(f.holder, 'data-view', 'card').events.click();
  assert.equal(f.state.diaryUI[OWNER].view, 'card');

  const fresh = await fixture({ state: f.state });
  assert.equal(fresh.state.diaryUI[OWNER].per.love.date, '2026-10-15');
  assert.equal(fresh.state.diaryUI[OWNER].view, 'card');
});

test('不同人各记各的：状态和草稿都不串', async () => {
  const f = await fixture();
  await byAttr(f.holder, 'data-book', 'normal').events.click();
  assert.equal(f.state.diaryUI[OWNER].book, 'normal');
  /* 朋友还没有任何记忆，且她不是主人 → 落到普通日记（恋爱本是主人的那本） */
  f.setWho(FRIEND);
  const friend = await fixture({ state: f.state, who: FRIEND });
  assert.equal(friend.activeBook(), 'normal');
  assert.notEqual(f.state.diaryUI[FRIEND], undefined);
  /* 主人的编辑草稿不会出现在朋友那边 */
  f.state.diaryUI[OWNER].editing = 'normal-seg';
  assert.equal(friend.state.diaryUI[FRIEND].editing, '');
  /* 朋友自己选了恋爱本，也只记在她名下 */
  await byAttr(friend.holder, 'data-book', 'love').events.click();
  assert.equal(f.state.diaryUI[FRIEND].book, 'love');
  assert.equal(f.state.diaryUI[OWNER].book, 'normal');
});

test('图表指定的日期仍然优先，并且用掉后不清掉别人的', async () => {
  const f = await fixture({ state: { diaryDate: '2026-10-15' } });
  assert.equal(f.contentCalls()[0].payload.date, '2026-10-15');
  assert.equal(f.state.diaryDate, undefined);
  assert.equal(f.state.diaryUI[OWNER].per.love.date, '2026-10-15');
});

test('换本时旧本的响应晚到，不会盖掉新选的那本', async () => {
  const slowLove = deferred();
  let first = true;
  const f = await fixture({
    autoRefresh: false,
    get: async (key, payload) => {
      if (key === 'diaryContent' && payload.book === 'love' && first) {
        first = false;
        return slowLove.promise;      /* 恋爱本这次卡住 */
      }
      return undefined;
    },
  });
  const loading = f.view.refresh();
  /* 等本子切换按钮画出来（refresh 先渲按钮，才去发被卡住的正文请求） */
  await until(() => byAttr(f.holder, 'data-book', 'normal'), 'book switcher');
  /* 用户等不及，切到普通日记 */
  await byAttr(f.holder, 'data-book', 'normal').events.click();
  await until(() => /普通正文/.test(allText(f.holder)), 'normal content');
  /* 旧响应现在才到——正文不该被恋爱本覆盖 */
  slowLove.resolve({ entries: [{ date: '2026-10-15', text: '迟到的恋爱正文', seg_id: 'love-seg' }], edit_window: { can_edit: true, unlimited: true }, total: 5 });
  await loading;
  await flush();
  assert.doesNotMatch(allText(f.holder), /迟到的恋爱正文/);
  assert.match(allText(f.holder), /普通正文/);
  assert.equal(f.activeBook(), 'normal');
});

test('回收站按需加载：翻页、换日期、切本都不发 diary/trash', async () => {
  const f = await fixture();
  assert.equal(f.trashCalls(), 0);
  /* 翻页 / 换日期 / 切本 —— 全都不该碰回收站 */
  await byAttr(f.holder, 'data-book', 'normal').events.click();
  await byAttr(f.holder, 'data-view', 'text').events.click();
  const cell = nodes(f.holder, 'div').find(n => n.className && n.className.startsWith('cal-cell') && n.className.includes('has-diary'));
  if (cell) await cell.events.click();
  assert.equal(f.trashCalls(), 0);
  /* 只有点"展开"才真的去读 */
  await click(f.holder, '展开');
  assert.equal(f.trashCalls(), 1);
  /* 收起不再发第二次 */
  await click(f.holder, '收起');
  assert.equal(f.trashCalls(), 1);
});

test('编辑与取消都原位替换，不重拉正文', async () => {
  const f = await fixture();
  const before = f.contentCalls().length;
  /* 每次都重新取：原位替换换掉的是节点本身，旧引用会指向已脱离的节点。 */
  const article = () => nodes(f.holder, 'article')[0];
  await click(article(), '编辑');
  /* 编辑器就在这一条里，没整屏重画 */
  assert.ok(nodes(article(), 'textarea').length, 'editor should appear in place');
  assert.equal(f.contentCalls().length, before);
  assert.equal(f.state.diaryUI[OWNER].editing, 'love-seg');
  /* 取消原位换回正文，同样不重拉 */
  await click(article(), '取消');
  assert.equal(nodes(f.holder, 'textarea').length, 0);
  assert.equal(f.contentCalls().length, before);
  assert.match(allText(f.holder), /恋爱正文/);
});

test('删除确认原位展开，确认后才发删除请求', async () => {
  const f = await fixture();
  const before = f.contentCalls().length;
  const article = () => nodes(f.holder, 'article')[0];
  await click(article(), '删除');
  assert.ok(button(article(), '确认删除'));
  assert.equal(f.posts.length, 0);
  assert.equal(f.contentCalls().length, before);
  await click(article(), '确认删除');
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].key, 'diaryDelete');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].payload)), { book: 'love', seg_id: 'love-seg' });
});

test('视图卸载后，在途的正文响应不再画到已废弃的节点上', async () => {
  const slow = deferred();
  const f = await fixture({ autoRefresh: false, get: async key => (key === 'diaryContent' ? slow.promise : undefined) });
  const loading = f.view.refresh();
  f.view.unmount();
  slow.resolve({ entries: [{ date: '2026-10-15', text: '卸载后才到', seg_id: 'love-seg' }], edit_window: {}, total: 1 });
  await loading;
  assert.doesNotMatch(allText(f.holder), /卸载后才到/);
});

test('延迟读取时旧内容仍在屏幕上，回收站恒在最后', async () => {
  const slow = deferred();
  const f = await fixture();
  assert.match(allText(f.holder), /恋爱正文/);
  /* 让下一次正文读取卡住 */
  f.setGet(async (key, payload) => (key === 'diaryContent' && payload.book === 'normal' ? slow.promise : undefined));
  const pending = byAttr(f.holder, 'data-book', 'normal').events.click();
  /* 等待期间：旧内容没塌掉，还有小状态提示 */
  await until(() => /读取中/.test(allText(f.holder)), 'busy hint');
  assert.match(allText(f.holder), /恋爱正文/, 'old content must stay during load');
  assert.ok(nodes(f.holder, 'div').some(n => n.className && n.className.includes('np-busy')), 'content slot must be marked busy');
  /* 回收站恒在最后一块 */
  assert.match(f.holder.children.at(-1).className, /np-trash/);
  slow.resolve({ entries: [{ date: '2026-10-16', text: '普通正文', seg_id: 'normal-seg' }], edit_window: { can_edit: true, unlimited: true }, total: 12 });
  await pending;
  assert.match(allText(f.holder), /普通正文/);
  assert.match(f.holder.children.at(-1).className, /np-trash/);
});

test('300 个 emoji 不被截半：maxlength 是宽松兜底，真口径是码点', async () => {
  const f = await fixture();
  const details = nodes(f.holder, 'details')[0];
  details.open = true;
  await details.events.toggle();
  const area = nodes(details, 'textarea')[0];
  assert.equal(area.getAttribute('maxlength'), '600');
  area.value = '\u{1F600}'.repeat(301);
  area.events.input();
  assert.equal(Array.from(area.value).length, 300, 'must count code points, not UTF-16 units');
  assert.match(allText(details), /300\/300 字/);
});

test('真实草稿会被记住：重建视图后还在，换人则看不见，写成功即作废', async () => {
  const f = await fixture();
  const article = () => nodes(f.holder, 'article')[0];
  await click(article(), '编辑');
  const area = nodes(article(), 'textarea')[0];
  area.value = '这段还没保存的正文';
  area.events.input();
  /* 草稿落到按人 + 本 + 段的存储里 */
  assert.equal(f.state.diaryUI[OWNER].edits['love|love-seg'], '这段还没保存的正文');
  /* 重建视图（等价于切 tab 回来）：editing 状态也跟着走，编辑器直接开着，
     且里面是**用户真正敲的那段字**，不是服务端旧正文。 */
  const fresh = await fixture({ state: f.state });
  assert.equal(nodes(fresh.holder, 'textarea')[0].value, '这段还没保存的正文');
  /* 换人回来：绝不能看见别人的草稿（对象在 vm 里，deepEqual 会因原型不同误判） */
  const friend = await fixture({ state: f.state, who: FRIEND });
  assert.equal(Object.keys(friend.state.diaryUI[FRIEND].edits).length, 0);
  /* 写成功后草稿作废（seg_id 之后会变，留着会把旧字套到新内容上） */
  await click(nodes(fresh.holder, 'article')[0], '保存');
  assert.equal(f.state.diaryUI[OWNER].edits['love|love-seg'], undefined);
});

test('留言草稿同样按人保存，换人不串', async () => {
  const f = await fixture();
  const details = nodes(f.holder, 'details')[0];
  details.open = true;
  await details.events.toggle();
  const area = nodes(details, 'textarea')[0];
  area.value = '没发出去的半句话';
  area.events.input();
  assert.equal(f.state.diaryUI[OWNER].comments['love|love-seg'], '没发出去的半句话');
  const friend = await fixture({ state: f.state, who: FRIEND });
  assert.equal(Object.keys(friend.state.diaryUI[FRIEND].comments).length, 0);
});

test('切回正文后，卡片的晚响应盖不掉正文', async () => {
  const slowCard = deferred();
  const f = await fixture({
    autoRefresh: false,
    get: async key => (key === 'diaryCard' ? slowCard.promise : undefined),
  });
  const loading = f.view.refresh();
  await until(() => byAttr(f.holder, 'data-view', 'card'), 'view switch');
  await byAttr(f.holder, 'data-view', 'card').events.click();
  /* 用户又切回正文 */
  await byAttr(f.holder, 'data-view', 'text').events.click();
  await until(() => /恋爱正文/.test(allText(f.holder)), 'text content back');
  /* 卡片的响应这时才到——必须被丢弃 */
  slowCard.resolve({ ok: true, html: '<p>迟到的卡片</p>', days: [], index: 0, date: '2026-10-15', style: '', styles: [], style_labels: {} });
  await loading;
  await flush();
  assert.doesNotMatch(allText(f.holder), /迟到的卡片/);
  assert.match(allText(f.holder), /恋爱正文/);
});

test('选择日期后正文与卡片保持同一天，换本不会沿用别本日期', async () => {
  const f = await fixture({ get: async (key, payload) => {
    if (key === 'diaryCard') return { ok: true, html: '<p>card</p>', date: payload.date || '2026-10-16', days: [], styles: [] };
  }});
  const day = nodes(f.holder, 'div').find(n => n.className?.includes('has-diary'));
  await day.events.click();
  await byAttr(f.holder, 'data-view', 'card').events.click();
  await until(() => f.calls.some(c => c.key === 'diaryCard'), 'selected card');
  assert.equal(f.calls.find(c => c.key === 'diaryCard').payload.date, '2026-10-15');
  await byAttr(f.holder, 'data-book', 'normal').events.click();
  assert.equal(f.calls.filter(c => c.key === 'diaryCard').at(-1).payload.date, '');
  await byAttr(f.holder, 'data-view', 'text').events.click();
  assert.equal(f.contentCalls().at(-1).payload.date, '2026-10-16');
});

test('写入失败后能原位重试，成功后才清草稿并更新篇数', async () => {
  let attempts = 0;
  const f = await fixture({ post: async () => {
    if (++attempts === 1) throw new Error('temporary network error');
    return { ok: true };
  }});
  await click(nodes(f.holder, 'article')[0], '编辑');
  const area = nodes(f.holder, 'textarea')[0];
  area.value = '不要丢掉的新正文'; area.events.input();
  await click(f.holder, '保存');
  assert.equal(area.value, '不要丢掉的新正文');
  assert.equal(f.state.diaryUI[OWNER].edits['love|love-seg'], area.value);
  await click(f.holder, '保存');
  assert.equal(attempts, 2);
  assert.equal(f.state.diaryUI[OWNER].edits['love|love-seg'], undefined);
  assert.equal(f.calls.filter(c => c.key === 'diaryList').length, 2);
});

test('旧人的保存回包不清除新人草稿，也不重拉新人的正文', async () => {
  const saved = deferred();
  const f = await fixture({ post: async () => saved.promise });
  await click(nodes(f.holder, 'article')[0], '编辑');
  const area = nodes(f.holder, 'textarea')[0];
  area.value = '主人的草稿'; area.events.input();
  const writing = click(f.holder, '保存');
  f.view.unmount(); f.setWho(FRIEND);
  const friend = await fixture({ state: f.state, who: FRIEND });
  friend.state.diaryUI[FRIEND].edits['love|love-seg'] = '朋友自己的草稿';
  const callsBefore = f.calls.length;
  saved.resolve({ ok: true }); await writing;
  assert.equal(friend.state.diaryUI[FRIEND].edits['love|love-seg'], '朋友自己的草稿');
  assert.equal(f.state.diaryUI[OWNER].edits['love|love-seg'], undefined);
  assert.equal(f.calls.length, callsBefore);
});

test('保存期间正文只读并明确显示保存中，防止继续输入后丢字', async () => {
  const saved = deferred();
  const f = await fixture({ post: () => saved.promise });
  await click(f.holder, '编辑');
  const area = nodes(f.holder, 'textarea')[0];
  area.value = '待保存正文'; area.events.input();
  const writing = click(f.holder, '保存');
  assert.equal(area.readOnly, true);
  assert.equal(button(f.holder, '保存中…').disabled, true);
  assert.equal(button(f.holder, '取消').disabled, true);
  saved.resolve({ ok: true }); await writing;
});

test('业务冲突刷新最新正文，并提供可复制的未保存文字', async () => {
  let changed = false;
  const f = await fixture({
    post: async () => { changed = true; return { ok: false, error: '日记已改写，请刷新' }; },
    get: async key => {
      if (key === 'diaryContent' && changed) return { entries: [{date:'2026-10-15',text:'别处已更新的正文',seg_id:'changed-seg'}], edit_window:{can_edit:true,unlimited:true} };
    },
  });
  await click(f.holder, '编辑');
  const area = nodes(f.holder, 'textarea')[0];
  area.value = '我未保存的文字'; area.events.input();
  await click(f.holder, '保存');
  assert.match(allText(f.holder), /别处已更新的正文/);
  const recovery = nodes(f.holder, 'textarea').find(el=>el.getAttribute('aria-label')==='保留的未保存正文');
  assert.equal(recovery.value, '我未保存的文字');
  assert.equal(f.contentCalls().length, 2);
});

test('最近若干条从最新一条打开，按天阅读仍保持当天时间顺序', async () => {
  const rows = [
    {date:'2026-10-15',time:'09:00',text:'昨天早上',seg_id:'old'},
    {date:'2026-10-16',time:'09:00',text:'今天早上',seg_id:'morning'},
    {date:'2026-10-16',time:'21:00',text:'今天晚上',seg_id:'latest'},
  ];
  const f = await fixture({get: async (key,p) => key==='diaryContent' ? {entries:rows.filter(r=>!p.date || r.date===p.date),edit_window:{},total:3} : undefined});
  assert.match(allText(f.holder), /今天晚上/);
  assert.doesNotMatch(allText(f.holder), /昨天早上/);
  await click(f.holder, '下一页'); await flush();
  assert.match(allText(f.holder), /今天早上/);
  f.context.state.diaryDate = '2026-10-16';
  await f.view.refresh();
  assert.match(allText(nodes(f.holder,'article')[0]), /今天早上/);
  assert.match(allText(nodes(f.holder,'article')[1]), /今天晚上/);
});
