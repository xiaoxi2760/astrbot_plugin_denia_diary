const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.attributes={}; this.events={}; this.value=''; }
  appendChild(node) { this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node),1); }
  get firstChild() { return this.children[0]; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return (this._text||'')+this.children.map(n=>n.textContent||'').join(''); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  addEventListener(name,fn) { this.events[name]=fn; }
  async emit(name,value) { if(value!==undefined)this.value=value; return this.events[name]?.({target:this}); }
}
function deferred() { let resolve; const promise=new Promise(done=>resolve=done);return {promise,resolve}; }
const binding = {agent_id:'yaya',device_id:'dv2_synthetic',scope_id:'official',platform_subject_id:'qq-user',preset_id:'persona',namespace:'short'};
const snapshot = () => ({ok:true,available:true,scope_id:'official',agent_id:'yaya',subjects:[{id:'qq-user',nickname:'Test'}],devices:[],listener:{running:false}});
function fixture() {
  const document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:String(text)})};
  const window={}, sandbox={window,document,setTimeout,clearTimeout,console};vm.createContext(sandbox);
  for(const file of ['ui.js','views/chat-devices.js']) vm.runInContext(fs.readFileSync(path.join(__dirname,'../../pages/diary',file),'utf8'),sandbox);
  const posts=[], toasts=[];
  const ctx={UI:window.UI,toast:(...a)=>toasts.push(a),apiGet:async()=>snapshot(),
    apiPost:async(key,body)=>{posts.push({key,body});return {ok:true,...binding,credential:'synthetic-one-time'};}};
  const root=new Element(), view=window.createChatDevicesView(ctx);view.mount(root);return {ctx,root,view,posts,toasts};
}
function nodes(root) {return [root,...(root.children||[]).flatMap(nodes)];}
function field(f,label) {const found=nodes(f.root).find(n=>n.attributes?.['aria-label']===label);assert.ok(found,label);return found;}
async function click(f,label) {const found=nodes(f.root).find(n=>n.tagName==='button'&&n.textContent===label);assert.ok(found,label);await found.emit('click');await new Promise(done=>setImmediate(done));}
async function fill(f) {await f.view.refresh();await field(f,'统一聊天设备名称').emit('input','PC');await field(f,'绑定核心用户').emit('change','qq-user');}

test('pairing sends only existing subject and name, and public binding accompanies the one-time credential',async()=>{
  const f=fixture();await fill(f);await click(f,'签发统一聊天凭据');
  assert.equal(f.posts.length,1);assert.equal(f.posts[0].key,'petChatDeviceIssue');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].body)),{name:'PC',subject_id:'qq-user'});
  const bundle=JSON.parse(field(f,'一次性统一聊天配对资料').value);
  assert.equal(bundle.scopeId,'official');assert.equal(bundle.platformSubjectId,'qq-user');assert.equal(bundle.credential,'synthetic-one-time');
  assert.equal(f.view.hasUnsavedChanges(),true);
  await f.view.refresh();assert.match(field(f,'一次性统一聊天配对资料').value,/synthetic-one-time/);
});
test('hiding and unmounting erase credentials, and hiding releases navigation guard',async()=>{
  const f=fixture();await fill(f);await click(f,'签发统一聊天凭据');const area=field(f,'一次性统一聊天配对资料');
  await click(f,'已保存，隐藏配对资料');assert.equal(area.value,'');assert.equal(f.view.hasUnsavedChanges(),false);
  assert.ok(!nodes(f.root).some(n=>n.value?.includes('synthetic-one-time')));
  await fill(f);await click(f,'签发统一聊天凭据');const second=field(f,'一次性统一聊天配对资料');f.view.unmount();
  assert.equal(second.value,'');assert.equal(f.root.children.length,0);
});
test('late issuance cannot paint or retain the credential after leaving the view',async()=>{
  const f=fixture(),late=deferred();await fill(f);f.ctx.apiPost=()=>late.promise;
  const pending=click(f,'签发统一聊天凭据');f.view.unmount();late.resolve({ok:true,...binding,credential:'late-secret'});await pending;
  assert.equal(f.root.children.length,0);assert.equal(f.view.hasUnsavedChanges(),false);
});
test('a mismatched scope is rejected without displaying its credential',async()=>{
  const f=fixture();await fill(f);f.ctx.apiPost=async()=>({ok:true,...binding,scope_id:'personal',credential:'wrong-scope-secret'});
  await click(f,'签发统一聊天凭据');assert.match(f.root.textContent,/绑定不匹配/);
  assert.ok(!nodes(f.root).some(n=>n.value?.includes('wrong-scope-secret')));
});
test('revocation needs a second explicit click and erases the currently issued credential',async()=>{
  const f=fixture();await fill(f);await click(f,'签发统一聊天凭据');
  f.ctx.apiGet=async()=>({...snapshot(),devices:[{...binding,name:'PC',permissions:['chat:write','turn:read']}]});await f.view.refresh();
  await click(f,'撤销设备');assert.equal(f.posts.length,1);await click(f,'确认撤销此设备');
  assert.equal(f.posts[1].key,'petChatDeviceRevoke');assert.equal(f.posts[1].body.device_id,binding.device_id);
  assert.ok(!nodes(f.root).some(n=>n.value?.includes('synthetic-one-time')));
});
test('an unavailable core cannot issue devices',async()=>{
  const f=fixture();f.ctx.apiGet=async()=>({...snapshot(),available:false});await f.view.refresh();
  assert.match(f.root.textContent,/尚未就绪/);await field(f,'统一聊天设备名称').emit('input','PC');await field(f,'绑定核心用户').emit('change','qq-user');
  await click(f,'签发统一聊天凭据');assert.equal(f.posts.length,0);
});
