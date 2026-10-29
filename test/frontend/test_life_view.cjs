const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.events = {};
    this.style = {}; this.dataset = {}; this.value = ''; this.disabled = false; this.hidden = false;
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
  async emit(name, value) { if (value !== undefined) this.value = value; return this.events[name]?.({ target: this, preventDefault() {} }); }
}
const snapshot = () => ({ ok: true, enabled: true, date: '2026-10-26', timezone: 'Asia/Shanghai', revision: 1,
  generation: {status: 'ready', attempts: 1, max_attempts: 3, error: '', next_retry_at: ''},
  activities: [
    {id:'past',start:'09:00',end:'10:00',title:'读一篇短篇',location:'书桌',status:'elapsed',outcome:''},
    {id:'now',start:'11:00',end:'13:00',title:'整理读书笔记',location:'窗边',status:'current',outcome:''},
    {id:'next',start:'14:00',end:'15:00',title:'去河边散步',location:'河边',status:'planned',outcome:''},
  ], current:{id:'now',start:'11:00',end:'13:00',title:'整理读书笔记',location:'窗边',status:'current'},
  next:{id:'next',start:'14:00',end:'15:00',title:'去河边散步',location:'河边',status:'planned'}, recent:[], updated_at:'2026-10-26T08:00:00+08:00',
});
function deferred() { let resolve; const promise = new Promise(done => resolve = done); return {promise,resolve}; }
function fixture() {
  const document = {createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), createTextNode: text => ({textContent:String(text)}), getElementById: () => null};
  const window = {}; const sandbox = {window,document,setTimeout,clearTimeout,console}; vm.createContext(sandbox);
  for (const file of ['ui.js','views/life.js']) vm.runInContext(fs.readFileSync(path.join(__dirname,'../../pages/diary',file),'utf8'),sandbox);
  const gets = [], posts = [], toasts = [];
  const ctx = {UI:window.UI, toast:(...args)=>toasts.push(args), apiGet:async key => {gets.push(key);return snapshot();},
    apiPost:async (key,body)=>{posts.push({key,body});return {ok:true,snapshot:{...snapshot(),revision:2}};}, who:()=> 'owner'};
  const root = new Element(); const view = window.createLifeView(ctx); view.mount(root);
  return {ctx,root,view,gets,posts,toasts};
}
function nodes(root) { return [root,...(root.children || []).flatMap(nodes)]; }
function action(f, name) { const node = nodes(f.root).find(n => n.attributes?.['data-act'] === name); assert.ok(node, name); return node; }
function field(f, name) { const node = nodes(f.root).find(n => n.attributes?.['aria-label'] === name); assert.ok(node, name); return node; }
async function click(node) { await node.emit('click'); await new Promise(done => setImmediate(done)); }

test('today refresh only reads and renders current and next public activities', async () => {
  const f = fixture(); await f.view.refresh();
  assert.ok(f.gets.includes('life')); assert.equal(f.posts.length,0);
  assert.match(f.root.textContent,/整理读书笔记/); assert.match(f.root.textContent,/去河边散步/);
  assert.match(f.root.textContent,/时段已过/);
});

test('an old request cannot repopulate the view after unmount', async () => {
  const f = fixture(); await f.view.refresh(); const d = deferred();
  f.ctx.apiGet = () => d.promise; const old = f.view.refresh(); f.view.unmount();
  const before = f.root.textContent; d.resolve({...snapshot(),current:{title:'过期的活动'}}); await old;
  assert.equal(f.root.textContent,before); assert.doesNotMatch(f.root.textContent,/过期的活动/);
});

test('saving future activities excludes current and uses the returned revision for the next edit', async () => {
  const f = fixture(); await f.view.refresh(); f.root.scrollTop = 480;
  await click(action(f,'open-editor')); await field(f,'标题（第 1 行）').emit('input','去茶馆坐坐');
  await click(action(f,'save-plan'));
  assert.equal(f.posts.length,1); assert.equal(f.posts[0].body.expected_revision,1);
  assert.equal(f.posts[0].body.activities.length,1);
  assert.equal(f.posts[0].body.activities[0].title,'去茶馆坐坐');
  assert.equal(f.root.scrollTop,480);
  await click(action(f,'open-editor')); await click(action(f,'save-plan'));
  assert.equal(f.posts[1].body.expected_revision,2);
});

test('conflict retains text and old revision until the owner explicitly rebases', async () => {
  const f = fixture(); await f.view.refresh(); await click(action(f,'open-editor'));
  await field(f,'标题（第 1 行）').emit('input','我没保存的活动');
  f.ctx.apiPost = async (key,body) => { f.posts.push({key,body}); return f.posts.length < 3
    ? {ok:false,conflict:true,error:'changed',snapshot:{...snapshot(),revision:9}}
    : {ok:true,snapshot:{...snapshot(),revision:10}}; };
  await click(action(f,'save-plan'));
  assert.equal(field(f,'标题（第 1 行）').value,'我没保存的活动');
  await click(action(f,'save-plan')); assert.equal(f.posts[1].body.expected_revision,1);
  await click(action(f,'rebase-plan')); assert.equal(f.posts.length,2);
  await click(action(f,'save-plan')); assert.equal(f.posts[2].body.expected_revision,9);
});

test('newer reads win and tab changes preserve global drafts', async () => {
  const f = fixture(); await f.view.refresh(); const old = deferred(), current = deferred(); let count=0;
  f.ctx.apiGet = () => ++count === 1 ? old.promise : current.promise;
  const first = f.view.refresh(), second = f.view.refresh();
  current.resolve({...snapshot(),current:{title:'最新的活动'}}); await second;
  old.resolve({...snapshot(),current:{title:'过期的活动'}}); await first;
  assert.match(f.root.textContent,/最新的活动/); assert.doesNotMatch(f.root.textContent,/过期的活动/);
  await click(action(f,'open-editor')); await field(f,'标题（第 1 行）').emit('input','切页时保留');
  f.view.unmount(); f.view.mount(f.root); f.ctx.apiGet=async()=>snapshot(); await f.view.refresh();
  assert.equal(field(f,'标题（第 1 行）').value,'切页时保留'); assert.equal(f.posts.length,0);
});

test('a damaged data snapshot explains the error and keeps writes disabled', async () => {
  const f = fixture(); f.ctx.apiGet=async()=>({...snapshot(),ok:false,generation:{status:'failed',error:'日程文件是未来版本'}});
  await f.view.refresh(); assert.match(f.root.textContent,/未来版本/);
  assert.ok(Object.hasOwn(action(f,'open-editor').attributes,'disabled'));
  assert.equal(f.posts.length,0);
});
