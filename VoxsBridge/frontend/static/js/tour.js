/* "Take the Tour": step-by-step guide to the dialer buttons. */
(function () {
    'use strict';
    const STEPS = [
        { sel: '.dial-rail .your-number', title: 'Your number', text: 'This is the number people see when you call them (your Caller ID).' },
        { sel: '#rtcStatus', title: 'Line status', text: '"Ready" in green means you can make and receive calls. If it says "Connecting", wait a moment.' },
        { sel: '.pn-field', title: 'Number box', text: 'Type or paste the number with its country code, e.g. +44 7700 900123. The country is shown automatically.' },
        { sel: '#dialPasteButton', title: 'Paste', text: 'Pastes a number you copied from anywhere (WhatsApp, email, a website).' },
        { sel: '#dialPad', title: 'Keypad', text: 'Tap the numbers to dial. Press and hold 0 to type +.' },
        { sel: '#callBtn', title: 'Call', text: 'Starts the call. You can also press Enter in the number box.' },
        { sel: '#padBody .call-actions .call-btn.gray', title: 'Backspace', text: 'Deletes the last digit.' },
        { demo: true, sel: '#muteBtn', title: 'Mute', text: 'During a call: turns your microphone off and on. The other person cannot hear you while muted.' },
        { demo: true, sel: '#keypadBtn', title: 'Keypad in call', text: 'Opens the keypad to press options in phone menus ("Press 1 for sales").' },
        { demo: true, sel: '#holdBtn', title: 'Hold', text: 'Puts the caller on hold. Press again to continue the call.' },
        { demo: true, sel: '#recBtn', title: 'Record', text: 'Starts or stops recording. Recordings appear in the Recordings page. Auto-record can be turned on in Calling Preferences.' },
        { demo: true, sel: '.incall-grid .incall-btn:nth-child(4)', title: 'Add Call & Transfer', text: 'Add Call puts this caller on hold and dials someone else. Transfer passes the call to another number.' },
        { demo: true, sel: '#inCallView .hangup-big', title: 'Hang up', text: 'Ends the call. The call is saved in Call History with its duration.' },
        { sel: null, title: "You're all set!", text: 'Find this tour again anytime in your profile menu → Take the Tour.' },
    ];
    let index = 0, layer = null, box = null, demoOn = false, saved = null, active = false;

    function visible(el) {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function busy() {
        const d = window.VoxsBridgeDialer;
        return !!(d && d.busy && d.busy());
    }
    function setDemo(on) {
        const pad = document.getElementById('padBody');
        const call = document.getElementById('inCallView');
        if (!pad || !call || busy()) return;
        if (on && !demoOn) {
            saved = { num: document.getElementById('inCallNumber').textContent, state: document.getElementById('inCallState').textContent };
            call.classList.add('tour-demo'); pad.hidden = true; call.hidden = false;
            document.getElementById('inCallNumber').textContent = '+44 7700 900123';
            document.getElementById('inCallState').textContent = 'Example call · 00:42';
            demoOn = true;
        } else if (!on && demoOn) {
            call.hidden = true; pad.hidden = false; call.classList.remove('tour-demo');
            if (saved) { document.getElementById('inCallNumber').textContent = saved.num; document.getElementById('inCallState').textContent = saved.state; }
            demoOn = false;
        }
    }
    function build() {
        layer = document.createElement('div');
        layer.className = 'tour-layer';
        layer.innerHTML = '<div class="tour-spot"></div>';
        box = document.createElement('div');
        box.className = 'tour-box';
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-modal', 'true');
        box.setAttribute('aria-labelledby', 'tourTitle');
        box.innerHTML = '<button type="button" class="tour-x" aria-label="Cancel tour">&times;</button>' +
            '<small class="tour-count" id="tourCount"></small><h3 id="tourTitle"></h3><p id="tourText"></p>' +
            '<div class="tour-dots" id="tourDots"></div>' +
            '<div class="tour-actions"><button type="button" class="tour-skip">Cancel tour</button>' +
            '<span><button type="button" class="tour-back">Back</button><button type="button" class="tour-next">Next</button></span></div>';
        document.body.append(layer, box);
        box.querySelector('.tour-x').onclick = end;
        box.querySelector('.tour-skip').onclick = end;
        box.querySelector('.tour-back').onclick = () => go(index - 1);
        box.querySelector('.tour-next').onclick = () => (index >= STEPS.length - 1 ? end() : go(index + 1));
        layer.onclick = end;
    }
    function place(target) {
        const spot = layer.querySelector('.tour-spot');
        const pad = 8;
        if (!target) {
            spot.style.cssText = 'top:50%;left:50%;width:0;height:0';
            box.style.cssText = '';
            box.classList.add('center');
            return;
        }
        box.classList.remove('center');
        target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        const r = target.getBoundingClientRect();
        spot.style.cssText = `top:${r.top - pad}px;left:${r.left - pad}px;width:${r.width + pad * 2}px;height:${r.height + pad * 2}px`;
        const bw = Math.min(330, window.innerWidth - 24);
        const bh = box.offsetHeight || 200;
        let left, top;
        const spaceLeft = r.left, spaceRight = window.innerWidth - r.right;
        if (window.innerWidth < 700) {                // phones: above or below the target
            left = (window.innerWidth - bw) / 2;
            top = r.bottom + 14 + bh < window.innerHeight - 70 ? r.bottom + 14 : Math.max(12, r.top - bh - 14);
        } else if (spaceLeft > bw + 24) {             // desktop rail is on the right: show the card to its left
            left = r.left - bw - 18;
            top = Math.min(Math.max(12, r.top + r.height / 2 - bh / 2), window.innerHeight - bh - 12);
        } else if (spaceRight > bw + 24) {
            left = r.right + 18;
            top = Math.min(Math.max(12, r.top + r.height / 2 - bh / 2), window.innerHeight - bh - 12);
        } else {
            left = Math.max(12, Math.min(r.left, window.innerWidth - bw - 12));
            top = r.bottom + 14 + bh < window.innerHeight ? r.bottom + 14 : Math.max(12, r.top - bh - 14);
        }
        box.style.cssText = `left:${left}px;top:${top}px;width:${bw}px`;
    }
    function go(i) {
        let next = Math.max(0, Math.min(STEPS.length - 1, i));
        const dir = i < index ? -1 : 1;
        // skip steps whose element is not available (e.g. demo steps during a real call)
        for (let guard = 0; guard < STEPS.length; guard++) {
            const st = STEPS[next];
            if (st.demo && busy()) { next += dir; if (next < 0 || next >= STEPS.length) { next = STEPS.length - 1; } continue; }
            setDemo(!!st.demo);
            if (!st.sel || visible(document.querySelector(st.sel))) break;
            next += dir;
            if (next < 0 || next >= STEPS.length) { next = STEPS.length - 1; }
        }
        index = next;
        const st = STEPS[index];
        box.querySelector('#tourCount').textContent = `Step ${index + 1} of ${STEPS.length}`;
        box.querySelector('#tourTitle').textContent = st.title;
        box.querySelector('#tourText').textContent = st.text;
        box.querySelector('#tourDots').innerHTML = STEPS.map((_, n) => `<i class="${n === index ? 'on' : ''}"></i>`).join('');
        box.querySelector('.tour-back').disabled = index === 0;
        box.querySelector('.tour-next').textContent = index === STEPS.length - 1 ? 'Finish' : 'Next';
        place(st.sel ? document.querySelector(st.sel) : null);
        box.querySelector('.tour-next').focus();
    }
    function onKey(e) {
        if (!active) return;
        if (e.key === 'Escape') end();
        else if (e.key === 'ArrowRight') go(index + 1);
        else if (e.key === 'ArrowLeft') go(index - 1);
    }
    function onResize() { if (active) place(STEPS[index].sel ? document.querySelector(STEPS[index].sel) : null); }
    function start() {
        if (active) return;
        if (window.matchMedia('(max-width: 1100px)').matches) {
            document.body.classList.add('rail-open');      // phones: show the dialer first
        }
        active = true;
        build();
        document.addEventListener('keydown', onKey);
        window.addEventListener('resize', onResize);
        setTimeout(() => go(0), 280);
    }
    function end() {
        if (!active) return;
        active = false;
        setDemo(false);
        layer && layer.remove();
        box && box.remove();
        document.removeEventListener('keydown', onKey);
        window.removeEventListener('resize', onResize);
        try { localStorage.setItem('voxsbridge-tour-done', '1'); } catch (e) {}
    }
    window.VoxsBridgeTour = { start, end };
})();
