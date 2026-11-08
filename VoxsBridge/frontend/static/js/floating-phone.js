/* Floating phone: a small always-on-top window (Document Picture-in-Picture, Chrome and Edge)
   that mirrors the active call and controls it. The call itself stays in this tab; the mini
   window only reads its state and calls the same functions the main buttons use. */
(function () {
  'use strict';
  var rail = document.getElementById('dialRail');
  if (!rail || window.__dnFloating) return;
  window.__dnFloating = true;
  var supported = 'documentPictureInPicture' in window;

  // Button in the dialer rail (hidden where the browser cannot do it).
  var css = document.createElement('style');
  css.textContent = '.rail-pop{display:inline-flex;align-items:center;gap:7px;align-self:flex-end;margin:-4px 0 8px;padding:6px 12px;border-radius:999px;border:1px solid var(--border,#31384b);background:var(--bg-secondary,#1b2233);color:var(--text-primary,#e8edff);font-size:12px;font-weight:600;cursor:pointer}.rail-pop:hover{border-color:#3de0ff;color:#3de0ff}.rail-pop[hidden]{display:none}.rail-pop svg{width:14px;height:14px}.rail-pop.on{border-color:#22c55e;color:#22c55e}';
  document.head.appendChild(css);
  var btn = document.createElement('button');
  btn.type = 'button'; btn.id = 'pipBtn'; btn.className = 'rail-pop'; btn.hidden = !supported;
  btn.title = 'Keep a small phone on top of your other tabs and apps (Chrome and Edge)';
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2"/><rect x="12" y="11" width="7" height="5" rx="1" fill="currentColor"/></svg><span>Floating phone</span>';
  var anchor = rail.querySelector('.your-number');
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
  if (!supported) return;

  var pip = null, timer = null, keypadOpen = false;

  var PIP_CSS = '*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:14px;background:linear-gradient(180deg,#0f1a3d,#070c1e);color:#eef3ff;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;text-align:center;overflow:hidden}' +
    '.top{position:absolute;top:10px;left:14px;right:14px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#8fa3d0;letter-spacing:.04em}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#64748b;margin-right:6px}.dot.ok{background:#22c55e}' +
    '.av{width:64px;height:64px;border-radius:50%;background:#22c55e;display:grid;place-items:center;margin-top:14px;box-shadow:0 0 0 6px rgba(34,197,94,.15)}.av.idle{background:#334155;box-shadow:none}.av.ring{animation:pulse 1.2s ease-out infinite}@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.55)}100%{box-shadow:0 0 0 16px rgba(34,197,94,0)}}' +
    '.num{font-size:20px;font-weight:700;word-break:break-all;max-width:100%}.st{font-size:13px;color:#9fb0d2;min-height:18px}.row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;width:100%}' +
    'button{font-family:inherit;cursor:pointer;border:1px solid #2c3a66;background:#16224a;color:#eef3ff;border-radius:14px;min-height:46px;padding:0 16px;font-size:13px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;gap:7px;transition:transform .12s ease,filter .15s ease,background .2s ease}button:hover{filter:brightness(1.2);transform:translateY(-1px)}button:active{transform:scale(.96)}' +
    'button.on{background:#f59e0b;border-color:#f59e0b;color:#111}button.red{background:#e11d48;border-color:#e11d48}button.green{background:#22c55e;border-color:#22c55e;color:#06210f}.hang{width:100%;max-width:220px;min-height:50px;border-radius:999px}' +
    '.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;width:100%;max-width:230px}.pad button{min-height:38px;padding:0;font-size:16px}[hidden]{display:none!important}svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}';

  function q(id) { return document.getElementById(id); }
  function text(id) { var e = q(id); return e ? (e.textContent || '').trim() : ''; }
  function shown(id) { var e = q(id); return !!e && !e.hidden; }
  function on(id) { var e = q(id); return !!e && e.classList.contains('on'); }
  function call(name) { try { if (typeof window[name] === 'function') window[name].apply(window, Array.prototype.slice.call(arguments, 1)); } catch (e) {} }

  function build(doc) {
    var s = doc.createElement('style'); s.textContent = PIP_CSS; doc.head.appendChild(s);
    doc.title = 'VoxsBridge';
    doc.body.innerHTML =
      '<div class="top"><span><i class="dot" id="d"></i><span id="rtc">VoxsBridge</span></span><span id="who"></span></div>' +
      '<div class="av idle" id="av"><svg viewBox="0 0 24 24" style="stroke:#fff;fill:#fff;width:30px;height:30px"><path d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4zm0 2c-3.3 0-8 1.7-8 5v1h16v-1c0-3.3-4.7-5-8-5z"/></svg></div>' +
      '<div class="num" id="n">Ready to call</div><div class="st" id="s"></div>' +
      '<div class="row" id="incoming" hidden><button class="green" id="ans">Answer</button><button class="red" id="dec">Decline</button></div>' +
      '<div id="incall" style="width:100%;display:flex;flex-direction:column;align-items:center;gap:10px" hidden>' +
        '<div class="row"><button id="mute">Mute</button><button id="hold">Hold</button><button id="kp">Keypad</button></div>' +
        '<div class="pad" id="pad" hidden>' + '123456789*0#'.split('').map(function (d) { return '<button data-d="' + d + '">' + d + '</button>'; }).join('') + '</div>' +
        '<button class="red hang" id="hang">Hang up</button></div>' +
      '<div class="st" id="idle" style="max-width:230px">Start calls from the VoxsBridge tab. Incoming calls show up here.</div>';
    var $ = function (id) { return doc.getElementById(id); };
    $('ans').onclick = function () { call('answerIncoming'); };
    $('dec').onclick = function () { call('declineIncoming'); };
    $('mute').onclick = function () { call('toggleMute'); };
    $('hold').onclick = function () { call('toggleHold'); };
    $('hang').onclick = function () { call('hangup'); };
    $('kp').onclick = function () { keypadOpen = !keypadOpen; $('pad').hidden = !keypadOpen; $('kp').classList.toggle('on', keypadOpen); };
    $('pad').onclick = function (e) { var b = e.target.closest('[data-d]'); if (b) call('press', b.getAttribute('data-d')); };
  }

  function update() {
    if (!pip || pip.closed) return close();
    var doc = pip.document, $ = function (id) { return doc.getElementById(id); };
    if (!$('n')) return;
    var inc = shown('incomingView'), inc2 = shown('inCallView');
    var rtc = text('rtcStatus');
    $('rtc').textContent = rtc || 'VoxsBridge';
    $('d').className = 'dot' + (rtc === 'Ready' ? ' ok' : '');
    $('incoming').hidden = !inc;
    $('incall').hidden = inc || !inc2;
    $('idle').hidden = inc || inc2;
    var av = $('av');
    av.className = 'av' + (inc ? ' ring' : inc2 ? '' : ' idle');
    if (inc) {
      $('n').textContent = text('incomingNumber') || 'Unknown';
      $('s').textContent = text('incomingState') || 'Incoming call';
    } else if (inc2) {
      $('n').textContent = text('inCallNumber') || '';
      var t = text('activitySub'), st = text('inCallState');
      $('s').textContent = (st && !/^\d+:\d+/.test(st) ? st : '') + (t && /\d+:\d+/.test(t) ? (st ? ' · ' : '') + t : '');
      var m = on('muteBtn'), h = on('holdBtn');
      $('mute').textContent = m ? 'Muted' : 'Mute'; $('mute').classList.toggle('on', m);
      $('hold').textContent = h ? 'On hold' : 'Hold'; $('hold').classList.toggle('on', h);
      if (!keypadOpen) { $('pad').hidden = true; }
    } else {
      $('n').textContent = 'Ready to call'; $('s').textContent = '';
      keypadOpen = false;
    }
  }

  function close() {
    if (timer) clearInterval(timer);
    timer = null;
    try { if (pip && !pip.closed) pip.close(); } catch (e) {}
    pip = null; keypadOpen = false;
    btn.classList.remove('on');
    btn.querySelector('span').textContent = 'Floating phone';
  }

  async function open() {
    if (pip && !pip.closed) { pip.focus(); return; }
    try {
      pip = await window.documentPictureInPicture.requestWindow({ width: 320, height: 450 });
    } catch (e) {
      var s = q('callStatus');
      if (s) { s.textContent = 'Could not open the floating phone: ' + (e && e.message ? e.message : e); s.style.color = 'var(--danger)'; }
      return;
    }
    build(pip.document);
    pip.addEventListener('pagehide', close);
    timer = setInterval(update, 400);
    update();
    btn.classList.add('on');
    btn.querySelector('span').textContent = 'Floating phone on';
  }

  btn.addEventListener('click', function () { (pip && !pip.closed) ? close() : open(); });
  window.openFloatingPhone = open;
  window.addEventListener('beforeunload', close);
})();
