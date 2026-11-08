/* Shared premium confirm popup: window.dnConfirm(text, okLabel, title) -> Promise<boolean>.
   users.html defines its own copy of the same function; this one is used everywhere else. */
(function(){
  if(window.__dnConfirmShared)return;window.__dnConfirmShared=true;
  var css='.dnc{position:fixed;inset:0;background:rgba(5,8,20,.72);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;z-index:99999;padding:16px}.dnc[hidden]{display:none}.dnc-card{background:linear-gradient(180deg,#1b2238,#121829);border:1px solid #33406a;border-radius:20px;padding:28px;max-width:440px;width:100%;box-shadow:0 30px 80px rgba(0,0,0,.6);color:#eaf1ff;text-align:center;animation:dncpop .18s ease}.dnc-ico{width:54px;height:54px;border-radius:50%;margin:0 auto 14px;display:grid;place-items:center;font-size:26px;font-weight:800;background:rgba(239,68,68,.16);color:#f87171;border:1px solid rgba(239,68,68,.4)}.dnc-card h3{margin:0 0 8px;font-size:20px}.dnc-card p{margin:0 0 20px;color:#b6c0de;font-size:14px;line-height:1.55}.dnc-actions{display:flex;gap:10px;justify-content:center}.dnc-actions button{border-radius:12px;padding:11px 18px;font-weight:700;cursor:pointer;border:1px solid #3a4770;background:transparent;color:#dbe3ff}.dnc-actions .dnc-ok{background:linear-gradient(135deg,#ef4444,#b91c1c);border-color:transparent;color:#fff}@keyframes dncpop{from{transform:scale(.94);opacity:0}to{transform:none;opacity:1}}';
  var st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
  var shared=function(text,okLabel,title){
    return new Promise(function(res){
      var m=document.createElement('div');m.className='dnc';m.setAttribute('role','dialog');m.setAttribute('aria-modal','true');
      m.innerHTML='<div class="dnc-card"><div class="dnc-ico">!</div><h3></h3><p></p><div class="dnc-actions"><button type="button" class="dnc-cancel">Cancel</button><button type="button" class="dnc-ok"></button></div></div>';
      m.querySelector('h3').textContent=title||'Are you sure?';
      m.querySelector('p').textContent=text;
      m.querySelector('.dnc-ok').textContent=okLabel||'Yes, delete';
      document.body.appendChild(m);
      var done=function(v){document.removeEventListener('keydown',key);m.remove();res(v)};
      var key=function(e){if(e.key==='Escape')done(false)};
      document.addEventListener('keydown',key);
      m.querySelector('.dnc-ok').onclick=function(){done(true)};
      m.querySelector('.dnc-cancel').onclick=function(){done(false)};
      m.addEventListener('click',function(e){if(e.target===m)done(false)});
      m.querySelector('.dnc-cancel').focus();
    });
  };
  if(!window.dnConfirm)window.dnConfirm=shared;
  window.dnConfirmShared=shared;
})();
