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
function snapshot() { return {ok:true,subject_id:'same-user',subjects:[{id:'same-user',nickname:'Test'}],
  presets:[{id:'persona',name:'Persona',description:'description',system_prompt:'original prompt',revision:'p1'}],
  active:{preset_id:'persona',namespace:'short',revision:'s1'}, styles:[],
  memory:[{id:1,value:'original memory',importance:.7,revision:'m1'}],history:[],archived_memory:[]}; }
function fixture() {
  const document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:String(text)}),getElementById:()=>null};
  const window={}; const sandbox={window,document,setTimeout,clearTimeout,console};vm.createContext(sandbox);
  for(const file of ['ui.js','views/core.js']) vm.runInContext(fs.readFileSync(path.join(__dirname,'../../pages/diary',file),'utf8'),sandbox);
  const posts=[],gets=[],toasts=[],options=[];
  const ctx={UI:window.UI,who:()=> 'same-user',setWho(){},mergeOptions:value=>options.push(value),toast:(...a)=>toasts.push(a),
    apiGet:async(key,params)=>{gets.push({key,params});return snapshot();},
    apiPost:async(key,body)=>{posts.push({key,body});return {ok:true};}};
  const root=new Element(); const view=window.createCoreView(ctx);view.mount(root);return {ctx,root,view,posts,gets,toasts,options};
}
function nodes(root) {return [root,...(root.children||[]).flatMap(nodes)];}
function field(f,label) { const found=nodes(f.root).find(n=>n.attributes?.['aria-label']===label);assert.ok(found,label);return found;}
async function click(f,label) {const found=nodes(f.root).find(n=>n.tagName==='button'&&n.textContent===label);assert.ok(found,label);await found.emit('click');await new Promise(done=>setImmediate(done));}

test('core page reads selected subject and a missing core does not submit operations',async()=>{
  const f=fixture();f.ctx.apiGet=async()=>({ok:false,error:'core missing'});await f.view.refresh();
  assert.match(f.root.textContent,/core missing/);assert.equal(f.posts.length,0);
});
test('saving one form preserves other unsaved drafts',async()=>{
  const f=fixture();await f.view.refresh();
  await field(f,'人设提示词').emit('input','unsaved persona');
  await field(f,'记忆内容 · 1').emit('input','saved memory');
  await click(f,'保存记忆');
  assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.subject_id,'same-user');
  assert.equal(f.posts[0].body.value,'saved memory');
  assert.equal(field(f,'人设提示词').value,'unsaved persona');
  assert.equal(f.view.hasUnsavedChanges(),true);
});
test('failed writes preserve the draft and prevent a user switch',async()=>{
  const f=fixture();await f.view.refresh();f.ctx.apiPost=async()=>({ok:false,error:'conflict'});
  await field(f,'人设提示词').emit('input','keep this');await click(f,'保存人设');
  assert.equal(field(f,'人设提示词').value,'keep this');assert.equal(f.view.hasUnsavedChanges(),true);
  assert.equal(f.toasts.at(-1)[0],'conflict');
});
test('an old read cannot paint after the core view is unmounted',async()=>{
  const f=fixture(),late=deferred();f.ctx.apiGet=()=>late.promise;
  const pending=f.view.refresh();f.view.unmount();late.resolve(snapshot());await pending;
  assert.equal(f.root.children.length,0);
});
