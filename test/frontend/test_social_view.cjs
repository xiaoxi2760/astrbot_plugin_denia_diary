/* 群社交视图的离线测试：渲染、登记 CRUD、启用开关、测试桩预览。
   手法照 test_life_view.cjs：vm 加载 ui.js + views/social.js，假 ctx 不碰网络。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.events = {};
    this.style = {}; this.dataset = {}; this.value = ''; this.disabled = false; this.hidden = false;
    this.checked = false;
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { this.parentNode?.removeChild(this); }
  get firstChild() { return this.children[0]; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || '') + this.children.map(n => n.textContent || '').join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'value') this.value = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name, callback) { this.events[name] = callback; }
  focus() {}
  async emit(name) { return this.events[name]?.({ target: this, preventDefault() {} }); }
}

const payload = () => ({
  ok: true, enabled: true, scope_mode: 'all', self_names: ['小澄'],
  config: { topic_enabled: true, topic_max_items: 12, topic_ttl_hours: 24, max_rounds: 3, session_window_minutes: 10, session_cooldown_minutes: 30 },
  groups: [{
    umo: 'aiocqhttp:GroupMessage:555', platform: 'aiocqhttp', group_id: '555',
    name: '测试群', enabled: false,
    participants: [{ id: 'bot1', name: '小助手', is_bot: false, allow_interaction: false }],
    topics: [{ ts: '2026-10-26T14:00:00', speaker_id: 'p1', speaker_name: '小明', text: '去看流星雨吗', hash: 'x' }],
    topic_total: 1, chat: { active: null, cooldown_until: '', recent_hashes: [] },
  }],
  seen: [{ umo: 'aiocqhttp:GroupMessage:666', platform: 'aiocqhttp', group_id: '666', name: '待登记群', last_seen_at: '2026-10-26T13:00:00' }],
  decisions: [{ ts: '2026-10-26T14:05:00', umo: 'aiocqhttp:GroupMessage:555', action: 'ignore', reason: '重复消息（去重）', slot: 'group_chat', fragment: '', round: 0 }],
});

function fixture(posts, data) {
  const document = { createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), createTextNode: text => ({ textContent: String(text) }), getElementById: () => null };
  const window = {}; const sandbox = { window, document, setTimeout, clearTimeout, console }; vm.createContext(sandbox);
  for (const file of ['ui.js', 'views/social.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../../pages/diary', file), 'utf8'), sandbox);
  const root = new Element();
  const ctx = {
    UI: window.UI, toast: () => {},
    apiGet: async key => { if (key !== 'social') throw new Error('unexpected GET ' + key); return data || payload(); },
    apiPost: async (key, body) => { posts.push({ key, body }); return { ok: true }; },
  };
  const view = window.createSocialView(ctx);
  view.mount(root);
  return { root, view, posts, ctx, window };
}

function nodes(root) { return [root, ...(root.children || []).flatMap(nodes)]; }
function byLabel(f, label) { return nodes(f.root).find(n => n.attributes?.['aria-label'] === label); }
function buttonWith(f, text) { return nodes(f.root).find(n => n.tagName === 'BUTTON' && n.textContent?.includes(text)); }
function buttonExact(f, text) { return nodes(f.root).find(n => n.tagName === 'BUTTON' && n.textContent === text); }
async function click(node) { await node.emit('click'); await new Promise(done => setImmediate(done)); }
function deferred() {
  let resolve; let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test('social view renders groups, participants, seen and decisions', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const text = f.root.textContent;
  assert.match(text, /测试群/);
  assert.match(text, /aiocqhttp:GroupMessage:555/);
  assert.match(text, /小助手（bot1）/);
  assert.match(text, /去看流星雨吗/);
  assert.match(text, /待登记群/);
  assert.match(text, /重复消息（去重）/);
  assert.equal(f.posts.length, 0); // 纯读取不发请求
});

test('scope warning shows when mode is not all', async () => {
  const posts = [];
  const private_payload = { ...payload(), scope_mode: 'private' };
  const f = fixture(posts, private_payload);
  await f.view.refresh();
  assert.match(f.root.textContent, /只私聊/);
  assert.match(f.root.textContent, /全部启用/);
});

test('enable toggle posts social/group save with flipped enabled', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const toggle = buttonWith(f, '未启用（点开）');
  assert.ok(toggle, 'enable button exists');
  await click(toggle);
  const post = posts.find(p => p.key === 'socialGroup');
  assert.ok(post);
  assert.equal(post.body.action, 'save');
  assert.equal(post.body.umo, 'aiocqhttp:GroupMessage:555');
  assert.equal(post.body.enabled, true);
});

test('registering a seen group defaults to disabled', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const register = buttonWith(f, '一键登记');
  assert.ok(register);
  await click(register);
  const post = posts.find(p => p.key === 'socialGroup');
  assert.ok(post);
  assert.equal(post.body.action, 'save');
  assert.equal(post.body.umo, 'aiocqhttp:GroupMessage:666');
  assert.equal(post.body.enabled, false); // 默认不开主动发言
});

test('marking a participant as bot keeps identity and posts full record', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const mark = buttonWith(f, '改标为 bot');
  assert.ok(mark);
  await click(mark);
  const post = posts.find(p => p.key === 'socialParticipant');
  assert.ok(post);
  assert.equal(post.body.action, 'save');
  assert.equal(post.body.group, 'aiocqhttp:GroupMessage:555');
  assert.equal(post.body.id, 'bot1');
  assert.equal(post.body.is_bot, true);
  assert.equal(post.body.allow_interaction, false); // 标注 bot 不顺手放开互动
});

test('new participant form posts real id with flags', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const idIn = byLabel(f, '参与者真实 id');
  const botBox = byLabel(f, '是 bot');
  const allowBox = byLabel(f, '允许互动');
  idIn.value = '20002'; botBox.checked = true; allowBox.checked = true;
  byLabel(f, '她在群里的称呼').value = '澄澄';
  byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）').value = 'aiocqhttp:GroupMessage:909';
  const add = buttonWith(f, '登记参与者');
  await click(add);
  const post = posts.find(p => p.key === 'socialParticipant');
  assert.ok(post);
  assert.equal(post.body.id, '20002');
  assert.equal(post.body.is_bot, true);
  assert.equal(post.body.allow_interaction, true);
  assert.equal(byLabel(f, '参与者真实 id').value, '', 'only the submitted participant draft clears on success');
  assert.equal(byLabel(f, '是 bot').checked, false);
  assert.equal(byLabel(f, '允许互动').checked, false);
  assert.equal(byLabel(f, '她在群里的称呼').value, '澄澄');
  assert.equal(byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）').value, 'aiocqhttp:GroupMessage:909');
});

test('successful manual registration clears only its own form draft', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const umo = byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）');
  const name = byLabel(f, '群显示名');
  const participant = byLabel(f, '参与者真实 id');
  umo.value = 'aiocqhttp:GroupMessage:919'; name.value = '新群'; participant.value = 'participant-9';
  await click(buttonExact(f, '登记'));
  const post = posts.find(p => p.key === 'socialGroup');
  assert.equal(post?.body.umo, 'aiocqhttp:GroupMessage:919');
  assert.equal(umo.value, '');
  assert.equal(name.value, '');
  assert.equal(byLabel(f, '参与者真实 id').value, 'participant-9');
});

test('refresh and tab remount preserve unsaved social form drafts in memory', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  byLabel(f, '她在群里的称呼').value = '澄澄、小澄';
  byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）').value = 'aiocqhttp:GroupMessage:777';
  byLabel(f, '群显示名').value = '新的群';
  byLabel(f, '参与者真实 id').value = 'participant-7';
  byLabel(f, '显示名').value = '群友七号';
  byLabel(f, '是 bot').checked = true;
  byLabel(f, '允许互动').checked = true;

  await f.view.refresh();
  assert.equal(byLabel(f, '她在群里的称呼').value, '澄澄、小澄');
  assert.equal(byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）').value, 'aiocqhttp:GroupMessage:777');
  assert.equal(byLabel(f, '群显示名').value, '新的群');
  assert.equal(byLabel(f, '参与者真实 id').value, 'participant-7');
  assert.equal(byLabel(f, '显示名').value, '群友七号');
  assert.equal(byLabel(f, '是 bot').checked, true);
  assert.equal(byLabel(f, '允许互动').checked, true);

  f.view.unmount();
  f.ctx.UI.clear(f.root);
  const nextRoot = new Element();
  const nextView = f.window.createSocialView(f.ctx);
  nextView.mount(nextRoot);
  await nextView.refresh();
  const next = { root: nextRoot };
  assert.equal(byLabel(next, '她在群里的称呼').value, '澄澄、小澄');
  assert.equal(byLabel(next, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）').value, 'aiocqhttp:GroupMessage:777');
  assert.equal(byLabel(next, '群显示名').value, '新的群');
  assert.equal(byLabel(next, '参与者真实 id').value, 'participant-7');
  assert.equal(byLabel(next, '显示名').value, '群友七号');
  assert.equal(byLabel(next, '是 bot').checked, true);
  assert.equal(byLabel(next, '允许互动').checked, true);
});

test('a late response after unmount cannot populate a remounted view', async () => {
  const posts = []; const f = fixture(posts); const response = deferred();
  f.ctx.apiGet = () => response.promise;
  const pending = f.view.refresh();
  f.view.unmount();
  f.ctx.UI.clear(f.root);
  const nextRoot = new Element();
  f.view.mount(nextRoot);
  response.resolve(payload());
  await pending;
  assert.equal(nextRoot.children.length, 0);
  assert.equal(nextRoot.textContent, '');
});

test('only the newest overlapping refresh may replace the visible groups', async () => {
  const posts = []; const f = fixture(posts); const older = deferred(); const newer = deferred();
  let calls = 0;
  f.ctx.apiGet = () => (++calls === 1 ? older.promise : newer.promise);
  const first = f.view.refresh();
  const second = f.view.refresh();
  newer.resolve({ ...payload(), groups: [{ ...payload().groups[0], name: '最新响应' }] });
  await second;
  older.resolve({ ...payload(), groups: [{ ...payload().groups[0], name: '较早响应' }] });
  await first;
  assert.match(f.root.textContent, /最新响应/);
  assert.doesNotMatch(f.root.textContent, /较早响应/);
});

test('a failed refresh keeps loaded content and reports the service error', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  f.ctx.apiGet = async () => { throw new Error('暂时离线'); };
  await f.view.refresh();
  assert.match(f.root.textContent, /测试群/);
  assert.match(f.root.textContent, /加载失败：\s*暂时离线/);
});

test('an ok=false payload shows its reason and keeps the previous panel', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  f.ctx.apiGet = async () => ({ ok: false, reason: '当前用户无权查看' });
  await f.view.refresh();
  assert.match(f.root.textContent, /测试群/);
  assert.match(f.root.textContent, /当前用户无权查看/);
});

test('a failed manual registration keeps the entered values', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const umo = byLabel(f, '手动登记（平台:GroupMessage:群号 或 平台 与 群号）');
  const name = byLabel(f, '群显示名');
  umo.value = 'aiocqhttp:GroupMessage:888'; name.value = '未登记成功';
  f.ctx.apiPost = async () => { throw new Error('写入失败'); };
  await click(buttonExact(f, '登记'));
  assert.equal(umo.value, 'aiocqhttp:GroupMessage:888');
  assert.equal(name.value, '未登记成功');
});

test('stub preview posts social/preview and never sends messages', async () => {
  const posts = []; const f = fixture(posts);
  await f.view.refresh();
  const btn = buttonWith(f, '模拟 bot 短聊判定');
  assert.ok(btn);
  posts.length = 0;
  await click(btn);
  const preview = posts.find(p => p.key === 'socialPreview');
  assert.ok(preview, 'preview endpoint called');
  assert.equal(preview.body.kind, 'chat');
  assert.equal(preview.body.group, 'aiocqhttp:GroupMessage:555');
  assert.ok(!posts.some(p => p.key !== 'socialPreview'), 'no other endpoint was hit');
});
