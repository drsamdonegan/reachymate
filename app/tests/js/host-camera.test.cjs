const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/host-camera.js', 'utf8');

function fixture({label = 'Reachy Mini Camera', known = true, permission} = {}) {
  const nodes = new Map(), calls = [], previewAssignments = [], timers = new Map();
  const track = {label, readyState:'live', muted:false, stopped:false, listeners:{},
    stop() {this.stopped=true;}, addEventListener(name, fn) {this.listeners[name]=fn;}};
  const stream = {getTracks:()=>[track],getVideoTracks:()=>[track]};
  const element = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {listeners:{}, disabled:false, hidden:true,
      addEventListener(name, fn) {this.listeners[name]=fn;}});
    return nodes.get(selector);
  };
  const video=element('#host-camera-preview');
  Object.assign(video,{videoWidth:1920,videoHeight:1080,readyState:4,play:async()=>{}});
  Object.defineProperty(video,'srcObject',{set(value){previewAssignments.push(value);}});
  const state={pending:null};
  const fetch=async(url,options={})=>{
    calls.push({url,options});
    return {ok:true,json:async()=>url.endsWith('/start') ? {token:'test-token'} : url.endsWith('/request') ? {request_id:state.pending} : {status:'received'}};
  };
  const window={addEventListener(){}};
  const context=vm.createContext({window,fetch,AbortController,
    setTimeout:fn=>{const id=Symbol();timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
    navigator:{mediaDevices:{enumerateDevices:async()=>[{kind:'videoinput',label:known?'Reachy Mini Camera':'',deviceId:'reachy-usb'}],
      getUserMedia:async options=>{state.constraints=options;return permission ? permission : stream;}}},
    document:{querySelector:element,createElement:()=>({getContext:()=>({drawImage(){}}),toBlob:fn=>fn(new Blob(['jpeg'],{type:'image/jpeg'}))})}
  });
  vm.runInContext(source,context);
  window.ReachyHostCamera.update({conversation:{mode:'event_host'},voice:{running:true,vision:{enabled:true}}});
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
  return {nodes,calls,track,stream,state,window,timers,previewAssignments,settle,
    enable:()=>element('#host-camera-start').listeners.click(),stop:()=>element('#host-camera-stop').listeners.click()};
}

test('selects Reachy exactly, captures only requested stills, and closes its own track',async()=>{
  const f=fixture();
  await f.enable(); await f.settle();
  assert.equal(f.state.constraints.video.deviceId.exact,'reachy-usb');
  assert.equal(f.state.constraints.audio,false);
  assert.equal(f.calls.filter(c=>c.url.includes('/frame/')).length,0);
  f.state.pending='requested-frame';
  for(const [id,fn] of [...f.timers]){f.timers.delete(id);fn();}
  await f.settle();
  const upload=f.calls.find(c=>c.url.endsWith('/frame/requested-frame'));
  assert.ok(upload);
  assert.equal(upload.options.headers.Authorization,'Bearer test-token');
  f.stop();
  assert.equal(f.track.stopped,true);
  assert.equal(f.nodes.get('#host-camera-preview').hidden,true);
  assert.ok(f.calls.some(c=>c.url.endsWith('/stop')));
});

test('rejects a MacBook track before preview or any camera API registration',async()=>{
  const f=fixture({known:false,label:'FaceTime HD Camera'});
  await f.enable();
  assert.equal(f.track.stopped,true);
  assert.ok(!f.previewAssignments.some(Boolean));
  assert.equal(f.calls.length,0);
  assert.match(f.nodes.get('#host-camera-status').textContent,/different camera/);
});

test('stop while permission is pending discards a late stream',async()=>{
  let resolve;
  const permission=new Promise(r=>{resolve=r;});
  const f=fixture({permission});
  const enabling=f.enable(); await f.settle();
  f.stop(); resolve(f.stream); await enabling;
  assert.equal(f.track.stopped,true);
  assert.equal(f.calls.length,0);
});

test('host end and a disconnected camera both close sharing',async()=>{
  for(const ending of ['host','usb']) {
    const f=fixture(); await f.enable(); await f.settle();
    if(ending==='host') f.window.ReachyHostCamera.update({conversation:{mode:'idle'},voice:{running:false}});
    else f.track.listeners.ended();
    assert.equal(f.track.stopped,true);
    assert.equal(f.nodes.get('#host-camera-stop').disabled,true);
  }
});
