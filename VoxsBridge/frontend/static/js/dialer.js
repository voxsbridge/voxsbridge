const input = document.getElementById('numberInput');
    const statusEl = document.getElementById('callStatus');
    const rtcStatus = document.getElementById('rtcStatus');
    const cfg = document.getElementById('dialerConfig');
    const FROM = cfg.dataset.from || '';
    const RECORDING_OK = cfg.dataset.recording === '1';
    const EXPIRED = cfg.dataset.expired === '1';
    const PROVIDER = cfg.dataset.provider === 'twilio' ? 'twilio' : 'telnyx';
    let recorder = null;
    let recCtx = null;
    let recCallId = null;
    let recChunks = [];
    let recStarted = 0;
    let activeCall = null;
    let incomingCall = null;
    let incomingGroupId = null;
    let incomingLookup = null;
    let declinedIncomingIds = new Set();
    let incomingSequence = 0;
    let placing = false;
    let heldCall = null;
    let onHold = false;
    let muted = false;
    let client = null;
    let talkStart = 0;
    let timerId = null;
    let logId = null;
    let ended = false;
    let lastState = '';
    let activeConfirmed = false;
    let answerHold = null;
    let rtpPackets = -1;
    let rtpStall = 0;
    let silentTicks = 0;
    let heardAudio = false;
    let sawAlive = false;
    let audioCtx = null;
    let analyser = null;
    let tappedStream = null;
    const presetParams = new URLSearchParams(location.search);
    if (presetParams.get('number')) input.value = presetParams.get('number');

    function clock(sec) {
        const m = String(Math.floor(sec / 60)).padStart(2, '0');
        const s = String(sec % 60).padStart(2, '0');
        return m + ':' + s;
    }
    function setStatus(msg, color) {
        statusEl.textContent = msg;
        statusEl.style.color = color || 'var(--text-secondary)';
    }
    function stopTimer() {
        if (timerId) clearInterval(timerId);
        timerId = null;
    }
    function startTimer() {
        if (talkStart) return;
        talkStart = Date.now();
        // Calling Preferences: record every call automatically.
        if (cfg.dataset.autorec === '1' && RECORDING_OK) {
            setTimeout(() => {
                if (activeCall && !ended && (!recorder || recorder.state === 'inactive')) toggleRec();
            }, 800);
        }
        document.getElementById('activitySub').textContent = '00:00';
        timerId = setInterval(() => {
            const sec = Math.floor((Date.now() - talkStart) / 1000);
            document.getElementById('activitySub').textContent = clock(sec);
        }, 1000);
    }
    function saveDuration(status) {
        if (!logId) return;
        const sec = talkStart ? Math.max(0, Math.round((Date.now() - talkStart) / 1000)) : 0;
        const id = logId;
        logId = null;
        fetch('/api/call-log/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: status || 'completed', duration: sec })
        });
        return sec;
    }
    
function markEnded(reason) {
    if (ended) return;

    ended = true;
    placing = false;
    activeConfirmed = false;
    lastState = 'hangup';

    stopRing();
    stopTimer();

    if (answerHold) {
        clearInterval(answerHold);
        answerHold = null;
    }

    const wasRecording =
        recorder && recorder.state === 'recording';

    const sec = saveDuration(
        talkStart ? 'completed' : 'failed'
    );

    talkStart = 0;

    const message = reason || 'Call ended';

    const title = document.getElementById('activityTitle');
    const sub = document.getElementById('activitySub');
    const inState = document.getElementById('inCallState');

    if (title) title.textContent = message;
    if (sub) sub.textContent = sec ? clock(sec) : message;
    if (inState) inState.textContent = message;

    setStatus(message);
    if (rtcStatus) rtcStatus.textContent = client ? 'Ready' : 'Not connected';

    if (wasRecording) toggleRec();

    const audio = document.getElementById('remoteAudio');
    if (audio) {
        audio.pause();
        audio.srcObject = null;
    }

    activeCall = null;
    muted = false;
    onHold = false;
    window.dispatchEvent(new CustomEvent('voxsbridge:call-ended', {
        detail: { reason: message, seconds: sec || 0 }
    }));

    if (heldCall) {
        resumeHeld();
        return;
    }

    showDialPad();
}

    function endReason(call) {
        const cause = ((call && call.cause) || '').toUpperCase();
        const sip = Number((call && call.sipCode) || 0);
        const reason = (call && (call.sipReason || call.sip_reason)) || '';
        // Show the provider's own code so failed calls can be diagnosed.
        const detail = [sip || '', cause, reason && String(reason) !== cause ? reason : '']
            .filter(Boolean).join(' ');
        if (sip || cause) console.warn('[VoxsBridge] call ended:', { cause, sipCode: sip, sipReason: reason, causeCode: call && call.causeCode });
        const tag = detail ? ' (' + detail + ')' : '';
        if (cause.includes('BUSY') || sip === 486) return 'Busy';
        if (cause.includes('REJECT') || sip === 603) return 'Call declined' + tag;
        if (cause.includes('NO_ANSWER') || sip === 408) return 'No answer';
        if (sip === 480 || sip === 487) return 'No answer';
        return 'Call ended' + (sip >= 400 ? tag : '');
    }
    function callIsDead(call) {
        if (!call) return false;
        const st = String(call.state || '').toLowerCase();
        if (['hangup', 'destroy', 'purge', 'done'].includes(st)) return true;
        const sip = Number(call.sipCode || 0);
        if ([486, 603, 480, 487, 404, 408].includes(sip)) return true;
        const pc = findPc(call);
        if (pc && (pc.connectionState === 'closed' || pc.connectionState === 'failed')) return true;
        return false;
    }
    function watchRemoteEnd(call) {
        if (!call || call._endWatch) return;
        call._endWatch = true;
        const pc = findPc(call);
        const end = () => { if (!ended && !onHold) markEnded(endReason(call)); };
        if (pc) {
            pc.addEventListener('connectionstatechange', () => {
                if (pc.connectionState === 'closed' || pc.connectionState === 'failed') end();
            });
            if (pc.getReceivers) {
                pc.getReceivers().forEach((r) => {
                    if (r.track) r.track.addEventListener('ended', end);
                });
            }
        }
        if (call.remoteStream) {
            call.remoteStream.getTracks().forEach((t) => t.addEventListener('ended', end));
        }
    }
    function armAnswerCheck() {
        if (answerHold || activeConfirmed || ended) return;
        const base = rtpPackets;
        let ticks = 0;
        answerHold = setInterval(() => {
            if (ended || !activeCall) {
                clearInterval(answerHold);
                answerHold = null;
                return;
            }
            ticks += 1;
            const grew = rtpPackets > base;
            if (grew && ticks >= 2) {
                clearInterval(answerHold);
                answerHold = null;
                activeConfirmed = true;
                lastState = '';
                showState('active');
                return;
            }
            if (ticks >= 5 && !grew && mediaGrew >= 2) {
                clearInterval(answerHold);
                answerHold = null;
                markEnded('Call ended');
            }
        }, 700);
    }
    function showState(st) {
        if (ended) return;
        st = String(st || '').toLowerCase();
        if (st === 'active' && !activeConfirmed) {
            armAnswerCheck();
            st = 'ringing';
        }
        if (!st || st === lastState) return;
        lastState = st;
        const labels = {
            new: 'Calling...', requesting: 'Calling...', trying: 'Calling...',
            early: 'Ringing', ringing: 'Ringing', answering: 'Connecting',
            active: 'Active', held: 'On hold',
            hangup: 'Call ended', destroy: 'Call ended', purge: 'Call ended', done: 'Call ended'
        };
        const label = labels[st] || st;
        if (st === 'hangup' || st === 'destroy' || st === 'purge' || st === 'done') {
            markEnded(endReason(activeCall));
            return;
        }
        document.getElementById('activityTitle').textContent = label;
        const who = (activeCall && activeCall.options && activeCall.options.destinationNumber) || input.value || '';
        document.getElementById('activitySub').textContent = st === 'active' ? '00:00' : who;
        const inState = document.getElementById('inCallState');
        if (inState) inState.textContent = label;
        setStatus(label + (who ? ' · ' + who : ''));
        if (st === 'active') {
            if (activeCall && activeCall.remoteStream && !activeCall._twilio) {
                document.getElementById('remoteAudio').srcObject = activeCall.remoteStream;
            }
            watchRemoteEnd(activeCall);
            startTimer();
        }
    }
    function watchCall(call) {
        if (!call) return;
        activeCall = call;
        if (call.on) call.on('telnyx.notification', () => {
            if (callIsDead(call)) markEnded(endReason(call));
            else showState(call.state);
        });
        showState(call.state);
    }
    function RTC() {
        if (window.TelnyxRTC) return window.TelnyxRTC;
        if (window.TelnyxWebRTC && window.TelnyxWebRTC.TelnyxRTC) return window.TelnyxWebRTC.TelnyxRTC;
        return null;
    }
    async function pasteNumber() {
        try {
            const text = await navigator.clipboard.readText();
            if (text) input.value = text.trim();
        } catch (err) {
            setStatus('Allow clipboard to paste a number', 'var(--danger)');
        }
    }
    function press(digit) {
        const inCall = document.getElementById('inCallView');
        if (inCall && !inCall.hidden) {
            if (activeCall && activeCall.dtmf) activeCall.dtmf(digit);
            const box = document.getElementById('dtmfDisplay');
            if (box) box.textContent += digit;
            return;
        }
        input.value += digit;
    }
    (function bindZero() {
        const btn = document.getElementById('zeroBtn');
        let timer = null;
        let long = false;
        const start = (e) => {
            e.preventDefault();
            long = false;
            timer = setTimeout(() => { long = true; press('+'); }, 600);
        };
        const end = () => {
            if (timer) clearTimeout(timer);
            timer = null;
            if (!long) press('0');
            long = false;
        };
        btn.addEventListener('pointerdown', start);
        btn.addEventListener('pointerup', end);
        btn.addEventListener('pointerleave', () => { if (timer) { clearTimeout(timer); timer = null; } });
        btn.addEventListener('pointercancel', () => { if (timer) { clearTimeout(timer); timer = null; } });
    })();
    function backspace() { input.value = input.value.slice(0, -1); }

    // ---------- Twilio adapter: gives a Twilio Call the same shape the
    // dialer already uses for Telnyx calls (state, hangup, dtmf, mute...).
    function twilioShim(tw, number, incoming) {
        const shim = {
            _twilio: true,
            _accepted: false,
            direction: incoming ? 'inbound' : 'outbound',
            id: 'tw-' + Math.random().toString(36).slice(2),
            state: incoming ? 'ringing' : 'new',
            cause: '',
            sipCode: 0,
            options: { destinationNumber: number, remoteCallerNumber: number },
            get remoteStream() { try { return tw.getRemoteStream ? tw.getRemoteStream() : null; } catch (e) { return null; } },
            get localStream() { try { return tw.getLocalStream ? tw.getLocalStream() : null; } catch (e) { return null; } },
            answer() { tw.accept(); },
            reject() { tw.reject(); },
            hangup() {
                try {
                    if (incoming && !shim._accepted) tw.reject();
                    else tw.disconnect();
                } catch (e) {}
            },
            dtmf(d) { try { tw.sendDigits(d); } catch (e) {} },
            muteAudio() { tw.mute(true); },
            unmuteAudio() { tw.mute(false); },
            hold() {},
            unhold() {}
        };
        tw.on('ringing', () => {
            if (shim.state === 'new') shim.state = 'ringing';
            if (activeCall === shim) showState('ringing');
        });
        tw.on('accept', () => {
            shim._accepted = true;
            shim.state = 'active';
            if (activeCall === shim) {
                activeConfirmed = true;
                lastState = '';
                showState('active');
            }
        });
        const finish = (reason) => {
            shim.state = 'hangup';
            if (incomingCall === shim) {
                stopRing();
                incomingCall = null;
                showDialPad();
                setStatus('Missed call');
                return;
            }
            if (activeCall === shim) markEnded(reason || 'Call ended');
            if (heldCall === shim) heldCall = null;
        };
        tw.on('disconnect', () => finish('Call ended'));
        tw.on('cancel', () => finish('Call ended'));
        tw.on('reject', () => finish('Call declined'));
        tw.on('error', (err) => {
            setStatus((err && err.message) || 'Call error', 'var(--danger)');
            finish('Call failed');
        });
        return shim;
    }

    async function twilioToken() {
        const res = await fetch('/api/twilio-token', { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Token failed');
        return data.token;
    }

    async function bootTwilio() {
        const Device = window.Twilio && window.Twilio.Device;
        if (!Device) {
            rtcStatus.textContent = 'SDK missing';
            setStatus('Calling service failed to load. Check your internet or ad blocker.', 'var(--danger)');
            return;
        }
        try {
            const token = await twilioToken();
            client = new Device(token, { codecPreferences: ['opus', 'pcmu'], logLevel: 'warn' });
            try {
                if (client.audio) {
                    client.audio.incoming(false);   // the rail plays its own ring
                    client.audio.disconnect(false);
                }
            } catch (e) {}
            client.on('registered', () => {
                rtcStatus.textContent = 'Ready';
                rtcStatus.style.color = 'var(--success)';
                const params = new URLSearchParams(location.search);
                if (params.get('call') === '1' && input.value && !window._autoDialed) {
                    window._autoDialed = true;
                    makeCall();
                }
            });
            client.on('unregistered', () => {
                if (rtcStatus.textContent === 'Ready') rtcStatus.textContent = 'Not connected';
            });
            client.on('error', (err) => {
                rtcStatus.textContent = 'RTC error';
                setStatus((err && err.message) || 'Line error', 'var(--danger)');
            });
            client.on('tokenWillExpire', async () => {
                try { client.updateToken(await twilioToken()); } catch (e) {}
            });
            client.on('incoming', (tw) => {
                if (activeCall || incomingCall || heldCall || placing ||
                    localStorage.getItem('voxsbridge-dnd') === 'true') {
                    try { tw.reject(); } catch (e) {}
                    return;
                }
                const from = (tw.parameters && tw.parameters.From) || 'Unknown caller';
                const shim = twilioShim(tw, from, true);
                ended = false;
                incomingCall = shim;
                hidePanels();
                const view = document.getElementById('incomingView');
                if (view) view.hidden = false;
                const num = document.getElementById('incomingNumber');
                if (num) num.textContent = from;
                rtcStatus.textContent = 'Incoming';
                rtcStatus.style.color = 'var(--success)';
                setStatus('Incoming call');
                startRing();
            });
            rtcStatus.textContent = 'Connecting...';
            await client.register();
        } catch (err) {
            rtcStatus.textContent = 'Not connected';
            setStatus(err.message, 'var(--danger)');
        }
    }

    async function boot() {
        if (PROVIDER === 'twilio') return bootTwilio();
        const Klass = RTC();
        if (!Klass) {
            rtcStatus.textContent = 'SDK missing';
            setStatus('Calling service failed to load. Check your internet or ad blocker.', 'var(--danger)');
            return;
        }
        try {
            const res = await fetch('/api/webrtc-token');
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Token failed');
            client = new Klass({ login_token: data.token });
            client.on('telnyx.ready', () => {
                rtcStatus.textContent = 'Ready';
                rtcStatus.style.color = 'var(--success)';
                const params = new URLSearchParams(location.search);
                if (params.get('call') === '1' && input.value && !window._autoDialed) {
                    window._autoDialed = true;
                    makeCall();
                }
            });
            client.on('telnyx.error', (e) => {
                const details = e && (e.error || e);
                const code = details && (details.code || details.error?.code);
                const message = (details && (details.message || details.error?.message)) || 'Line error';
                if (code === -32002 || /CALL DOES NOT EXIST/i.test(String(message))) return;
                rtcStatus.textContent = 'RTC error';
                setStatus(typeof message === 'string' ? message : JSON.stringify(message), 'var(--danger');
            });
            client.on('telnyx.socket.message', (msg) => {
                if (!activeCall || ended || !msg) return;
                const blob = JSON.stringify(msg).toLowerCase();
                const endedNow = blob.includes('telnyx_rtc.bye') || blob.includes('normal_clearing') || blob.includes('call_rejected') || blob.includes('user_busy') || blob.includes('originator_cancel') || blob.includes('no_answer') || blob.includes('no_user_response');
                if (!endedNow) return;
                const params = msg.params || {};
                console.warn('[VoxsBridge] provider hangup message:', msg);
                markEnded(endReason({
                    cause: params.cause || params.sipReason || params.sip_reason || '',
                    sipCode: params.sipCode || params.sip_code || 0,
                    sipReason: params.sipReason || params.sip_reason || ''
                }));
            });
            client.on('telnyx.notification', (n) => {
                const call = n && (n.call || n);
                if (!call || !call.state) return;
                if (call.id && declinedIncomingIds.has(call.id)) return;
                const st = String(call.state).toLowerCase();
                if (incomingCall && call.id === incomingCall.id) {
                    if (callIsDead(call)) {
                        stopRing();
                        incomingCall = null;
                        showDialPad();
                        setStatus('Missed call');
                    }
                    return;
                }
                if (!placing && !activeCall && (st === 'ringing' || st === 'new' || st === 'trying' || st === 'requesting')) {
                    ended = false;
                    showIncoming(call);
                    return;
                }
                if (ended && !heldCall) return;
                if (heldCall && call.id === heldCall.id) {
                    if (callIsDead(call)) heldCall = null;
                    return;
                }
                if (!activeCall || activeCall.id === call.id) activeCall = call;
                if (!onHold && callIsDead(call)) markEnded(endReason(call));
                else showState(st);
            });
            rtcStatus.textContent = 'Connecting...';
            client.connect();
        } catch (err) {
            rtcStatus.textContent = 'Not connected';
            setStatus(err.message, 'var(--danger)');
        }
    }

    async function makeCall() {
        let number = input.value.trim();
        if (!number) return setStatus('Enter a number', 'var(--danger)');
        if (!number.startsWith('+')) number = '+' + number.replace(/\D/g, '');
        if (!client) return setStatus('WebRTC is not ready', 'var(--danger)');
        ended = false;
        lastState = '';
        activeConfirmed = false;
        if (answerHold) { clearInterval(answerHold); answerHold = null; }
        talkStart = 0;
        stopTimer();
        muted = false;
        rtpPackets = -1;
        rtpStall = 0;
        mediaGrew = 0;
        iceStall = 0;
        sawAlive = false;
        silentTicks = 0;
        heardAudio = false;
        tappedStream = null;
        setStatus('Calling · ' + number);
        const title = document.getElementById('activityTitle');
        const sub = document.getElementById('activitySub');
        if (title) title.textContent = 'Calling...';
        if (sub) sub.textContent = number;
        showInCall(number);
        placing = true;
        try {
            if (PROVIDER === 'twilio') {
                const tw = await client.connect({ params: { To: number } });
                if (ended) {            // hung up while the call was being set up
                    try { tw.disconnect(); } catch (e) {}
                    placing = false;
                    return;
                }
                activeCall = twilioShim(tw, number, false);
            } else {
                activeCall = client.newCall({
                    destinationNumber: number,
                    callerNumber: FROM,
                    audio: true,
                    remoteElement: 'remoteAudio'
                });
            }
        } catch (err) {
            placing = false;
            setStatus(err.message || 'Call failed. Allow the microphone.', 'var(--danger)');
            showDialPad();
            return;
        }
        placing = false;
        watchCall(activeCall);
        const res = await fetch('/api/call-log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to_number: number, status: 'initiated' })
        });
        const saved = await res.json();
        logId = saved.id || (saved.call && saved.call.id) || null;
        if (ended && logId) saveDuration('failed');
    }
    
function hangup() {
    const call = activeCall;

    if (call && typeof call.hangup === 'function') {
        try {
            call.hangup();
        } catch (err) {
            console.error('Hangup error:', err);
        }
    }

    markEnded('Call ended');
}

    function transferCall() {
        if (!activeCall) return setStatus('Transfer works only during a call', 'var(--danger)');
        if (activeCall._twilio) return setStatus('Transfer is not available on this line yet', 'var(--danger)');
        const st = String(activeCall.state || '').toLowerCase();
        if (st && st !== 'active' && st !== 'held' && st !== 'ringing' && st !== 'early') {
            setStatus('Transfer works only when the call is active', 'var(--danger)');
            return;
        }
        openCompose('transfer');
    }
    function openCompose(mode) {
        composeMode = mode;
        document.getElementById('composeTitle').textContent = mode === 'transfer' ? 'Transfer call' : 'New call';
        document.getElementById('composeInput').value = '';
        hidePanels();
        document.getElementById('composeView').hidden = false;
    }
    function composePress(digit) {
        document.getElementById('composeInput').value += digit;
    }
    function composeBackspace() {
        const el = document.getElementById('composeInput');
        el.value = el.value.slice(0, -1);
    }
    function backToCall() {
        document.getElementById('composeView').hidden = true;
        document.getElementById('callsView').hidden = true;
        if (composeMode === 'add' && heldCall && !activeCall) resumeHeld();
        else if (activeCall) showInCall(legNumber(activeCall) || input.value);
        else showDialPad();
        composeMode = '';
    }
    function confirmCompose() {
        let number = document.getElementById('composeInput').value.trim();
        if (!number) return setStatus('Enter a number', 'var(--danger)');
        if (!number.startsWith('+')) number = '+' + number.replace(/\D/g, '');
        if (composeMode === 'transfer') {
            if (activeCall && activeCall.transfer) activeCall.transfer(number);
            composeMode = '';
            showInCall(legNumber(activeCall) || input.value);
            setStatus('Transferring to ' + number);
            return;
        }
        composeMode = '';
        input.value = number;
        makeCall();
    }
    function addCall() {
        if (!activeCall) return setStatus('No active call', 'var(--danger)');
        if (activeCall._twilio) return setStatus('Add Call is not available on this line yet', 'var(--danger)');
        if (heldCall) return setStatus('A call is already on hold', 'var(--danger)');
        try { if (activeCall.hold) activeCall.hold(); } catch (err) {}
        heldCall = activeCall;
        activeCall = null;
        onHold = false;
        openCompose('add');
    }
    function resumeHeld() {
        if (!heldCall) return showDialPad();
        activeCall = heldCall;
        heldCall = null;
        ended = false;
        activeConfirmed = true;
        lastState = '';
        onHold = false;
        muted = false;
        try { if (activeCall.unhold) activeCall.unhold(); } catch (err) {}
        const audio = document.getElementById('remoteAudio');
        if (audio && activeCall.remoteStream) {
            audio.srcObject = activeCall.remoteStream;
            audio.play().catch(() => {});
        }
        showInCall(legNumber(activeCall));
        showState('active');
        setStatus('Resumed held call');
    }
    function closeCalls() {
        if (activeCall) showInCall(legNumber(activeCall) || input.value);
        else if (heldCall) resumeHeld();
        else showDialPad();
    }
    function hangupLeg(role) {
        if (role === 'held' && heldCall) {
            try { if (heldCall.hangup) heldCall.hangup(); } catch (err) {}
            heldCall = null;
            openCallList();
            return;
        }
        hangup();
    }
    function openCallList() {
        const legs = [];
        if (activeCall) legs.push({ role: 'active', number: legNumber(activeCall) || input.value, status: onHold ? 'On hold' : 'Active' });
        if (heldCall) legs.push({ role: 'held', number: legNumber(heldCall), status: 'On hold' });
        const box = document.getElementById('callsList');
        box.innerHTML = legs.length ? legs.map((leg) =>
            '<div class="call-row"><div class="call-avatar"><svg width="18" height="18" viewBox="0 0 24 24" fill="#fff"><path d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4zm0 2c-3.3 0-8 1.7-8 5v1h16v-1c0-3.3-4.7-5-8-5z"/></svg></div><div class="call-meta"><strong>' +
            leg.number + '</strong><span>' + leg.status + '</span></div><button type="button" class="call-hang" onclick="hangupLeg(\'' + leg.role + '\')"><svg width="16" height="16" viewBox="0 0 24 24" fill="#fb7185"><path d="M12 9c-1.7 0-3.2.25-4.6.72v2.7c0 .4-.24.76-.6.92-.9.4-1.7.95-2.4 1.6-.2.18-.46.28-.74.28-.27 0-.53-.11-.71-.29L.54 12.5a1 1 0 0 1 0-1.41C3.3 8.4 7.45 7 12 7s8.7 1.4 11.46 4.09a1 1 0 0 1 0 1.41l-2.41 2.43c-.18.18-.44.29-.71.29-.28 0-.54-.1-.74-.28-.7-.65-1.5-1.2-2.4-1.6a1 1 0 0 1-.6-.92V9.72A15.2 15.2 0 0 0 12 9z"/></svg></button></div>'
        ).join('') : '<p style="color:var(--text-muted)">No calls in this session</p>';
        hidePanels();
        document.getElementById('callsView').hidden = false;
    }
    // Record both sides of the call (caller + agent) into one track.
    function recordingStream(call) {
        const remote = call.remoteStream;
        const local = call.localStream || (call.options && call.options.localStream) || null;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!remote) return null;
        if (!Ctx || !local) return remote;
        try {
            recCtx = new Ctx();
            const dest = recCtx.createMediaStreamDestination();
            recCtx.createMediaStreamSource(remote).connect(dest);
            recCtx.createMediaStreamSource(local).connect(dest);
            if (recCtx.state !== 'running') recCtx.resume().catch(() => {});
            return dest.stream;
        } catch (err) {
            return remote;
        }
    }
    function toggleRec() {
        if (!RECORDING_OK) {
            setStatus('Recording and campaigns are $25. Ask your admin to upgrade this account.', 'var(--danger)');
            return;
        }
        if (!recorder || recorder.state === 'inactive') {
            if (!activeCall || !activeCall.remoteStream) {
                setStatus('Start a call before recording', 'var(--danger)');
                return;
            }
            recChunks = [];
            recStarted = Date.now();
            recCallId = logId;
            const stream = recordingStream(activeCall);
            const opts = {};
            if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
                opts.mimeType = 'audio/webm;codecs=opus';
                opts.audioBitsPerSecond = 32000;
            }
            try { recorder = new MediaRecorder(stream, opts); }
            catch (err) { setStatus('Recording unavailable: ' + err.message, 'var(--danger)'); return; }
            recorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
            recorder.onstop = () => {
                if (recCtx) { recCtx.close().catch(() => {}); recCtx = null; }
                const blob = new Blob(recChunks, { type: 'audio/webm' });
                const seconds = Math.max(1, Math.round((Date.now() - recStarted) / 1000));
                const form = new FormData();
                form.append('file', blob, 'call.webm');
                form.append('to_number', input.value || '');
                form.append('duration', String(seconds));
                form.append('kind', 'call');
                if (recCallId) form.append('call_id', recCallId);
                recCallId = null;
                fetch('/api/recordings', { method: 'POST', body: form })
                    .then(r => { if (!r.ok) throw new Error('Upload failed (' + r.status + ')'); setStatus('Recording saved. Open Recordings to play it.'); })
                    .catch(() => setStatus('Could not save recording', 'var(--danger)'));
            };
            recorder.start();
            const recBtn = document.getElementById('recBtn');
            const recLabel = document.getElementById('recLabel');
            if (recBtn) recBtn.classList.add('recording');
            if (recLabel) recLabel.textContent = 'STOP';
            setStatus('Recording...');
        } else {
            recorder.stop();
            const recBtn = document.getElementById('recBtn');
            const recLabel = document.getElementById('recLabel');
            if (recBtn) recBtn.classList.remove('recording');
            if (recLabel) recLabel.textContent = 'REC';
            setStatus('Recording saved');
        }
    }
    function setMic(call, off) {
        if (!call) return;
        const stream = call.options && call.options.localStream;
        if (stream && stream.getAudioTracks) {
            stream.getAudioTracks().forEach((track) => { track.enabled = !off; });
        }
        if (off) {
            if (call.muteAudio) call.muteAudio();
        } else if (call.unmuteAudio) {
            call.unmuteAudio();
        }
    }
    function toggleMute() {
        if (!activeCall) return;
        muted = !muted;
        setMic(activeCall, muted);
        const icon = document.getElementById('muteIcon');
        document.getElementById('muteLabel').textContent = muted ? 'Muted' : 'Mute';
        document.getElementById('muteBtn').classList.toggle('on', muted);
        setStatus(muted ? 'Muted' : 'Unmuted');
    }
    let composeMode = '';
    function legNumber(call) {
        return (call && call.options && call.options.destinationNumber) || '';
    }
    function hidePanels() {
        document.getElementById('padBody').hidden = true;
        document.getElementById('inCallView').hidden = true;
        document.getElementById('composeView').hidden = true;
        document.getElementById('callsView').hidden = true;
        const incoming = document.getElementById('incomingView');
        if (incoming) incoming.hidden = true;
    }
    function callerOf(call) {
        const o = (call && call.options) || {};
        const value = o.remoteCallerNumber || o.remoteCallerName || o.callerNumber || '';
        if (!value || value === '10000000000' || /^anonymous/i.test(value)) return 'Unknown caller';
        return value;
    }

    async function lookupIncomingGroup() {
        const res = await fetch('/api/incoming/pending', {credentials: 'same-origin', cache: 'no-store'});
        if (!res.ok) throw new Error('Incoming group lookup failed');
        const data = await res.json();
        const groups = data.pending || [];
        if (!groups.length) throw new Error('No pending incoming call');
        // One active incoming group per user is expected; reject ambiguous matches.
        if (groups.length !== 1) throw new Error('Multiple pending calls; cannot safely decline');
        return groups[0];
    }
    let ringTimer = null;
    function startRing() {
        stopRing();
        if (window.VoxsBridgeRing) {           // ringtone chosen in Calling Preferences
            window._ringStop = window.VoxsBridgeRing.start(cfg.dataset.ringtone, Number(cfg.dataset.ringvol || 70));
            return;
        }
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        const ctx = new Ctx();
        const beep = () => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.frequency.value = 440;
            gain.gain.value = 0.04;
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            setTimeout(() => { try { osc.stop(); } catch (err) {} }, 350);
        };
        beep();
        ringTimer = setInterval(beep, 1600);
        window._ringCtx = ctx;
    }
    function stopRing() {
        if (window._ringStop) { try { window._ringStop(); } catch (e) {} window._ringStop = null; }
        if (ringTimer) clearInterval(ringTimer);
        ringTimer = null;
        if (window._ringCtx) {
            window._ringCtx.close().catch(() => {});
            window._ringCtx = null;
        }
    }
    function showIncoming(call) {
        // A new SIP leg can be offered again while the original PSTN leg is
        // still parked. Do not display it until the backend confirms there is
        // a genuinely pending, user-owned incoming group.
        if (incomingCall || incomingLookup || (call.id && declinedIncomingIds.has(call.id))) return;
        const sequence = ++incomingSequence;
        incomingLookup = lookupIncomingGroup();
        incomingLookup.then(group => {
            if (sequence !== incomingSequence || !group) return;
            if (localStorage.getItem('voxsbridge-dnd') === 'true') {
                // Do not create a repeated browser SIP BYE loop here.
                return;
            }
            incomingGroupId = group.id;
            incomingCall = call;
            hidePanels();
            const view = document.getElementById('incomingView');
            if (view) view.hidden = false;
            const number = group.from && !/^anonymous/i.test(group.from)
                ? group.from : callerOf(call);
            const num = document.getElementById('incomingNumber');
            if (num) num.textContent = number;
            rtcStatus.textContent = 'Incoming';
            rtcStatus.style.color = 'var(--success)';
            setStatus('Incoming call');
            startRing();
        }).catch(err => {
            // A declined/ended group is no longer pending. Ignore re-offers
            // rather than popping up a call that cannot be answered.
            console.debug('No active incoming group for SIP offer:', err.message);
        }).finally(() => {
            if (sequence === incomingSequence) incomingLookup = null;
        });
    }
    function answerIncoming() {
        if (!incomingCall) return;
        stopRing();
        const call = incomingCall;
        incomingCall = null;
        ended = false;
        activeConfirmed = true;
        lastState = '';
        onHold = false;
        muted = false;
        activeCall = call;
        const num = callerOf(call);
        if (num && num !== 'Unknown') input.value = num;
        try { call.answer(); } catch (err) {
            setStatus(err.message || 'Could not answer', 'var(--danger)');
        }
        showInCall(num);
        const inState = document.getElementById('inCallState');
        if (inState) inState.textContent = 'Connecting';
        setStatus('Answering');
        watchCall(call);
    }
    async function declineIncoming() {
        const call = incomingCall;
        const groupId = incomingGroupId;
        if (call && call._twilio) {
            stopRing();
            incomingCall = null;
            try { call.reject(); } catch (e) {}
            showDialPad();
            rtcStatus.textContent = 'Ready';
            setStatus('Call declined. The caller can leave a voicemail.');
            return;
        }
        if (!call || !groupId) return;
        stopRing();
        setStatus('Declining incoming call...');
        try {
            const res = await fetch('/api/incoming-groups/' + encodeURIComponent(groupId) + '/decline', {
                method: 'POST', credentials: 'same-origin'
            });
            const result = await res.json();
            if (!res.ok || !result.carrier_hangup_requested) {
                throw new Error(result.detail || result.status || 'Carrier decline failed');
            }
            // Telnyx will send the SIP BYE when the original PSTN leg ends.
            // Calling call.hangup() again can produce -32002 and re-offers.
            if (call.id) declinedIncomingIds.add(call.id);
            incomingSequence++;
            incomingCall = null;
            incomingGroupId = null;
            incomingLookup = null;
            showDialPad();
            rtcStatus.textContent = 'Ready';
            setStatus('Decline requested; awaiting carrier termination');
        } catch (err) {
            console.error('Incoming decline error:', err);
            setStatus('Decline failed: ' + err.message, 'var(--danger)');
            // Keep the incoming call visible so the user can retry.
            const view = document.getElementById('incomingView');
            if (view) view.hidden = false;
        }
    }
    function showInCall(number) {
        hidePanels();
        const view = document.getElementById('inCallView');
        view.hidden = false;
        view.classList.remove('keypad-open');
        document.getElementById('inCallNumber').textContent = number || input.value || '';
        document.getElementById('dtmfDisplay').textContent = '';
        document.getElementById('dtmfPad').hidden = true;
        document.getElementById('keypadBtn').classList.remove('on');
    }
    function showDialPad() {
        hidePanels();
        document.getElementById('padBody').hidden = false;
        document.getElementById('inCallView').classList.remove('keypad-open');
        document.getElementById('dtmfPad').hidden = true;
        const holdBtn = document.getElementById('holdBtn');
        if (holdBtn) holdBtn.classList.remove('on');
        const holdLabel = document.getElementById('holdLabel');
        if (holdLabel) holdLabel.textContent = 'Hold';
        const muteBtn = document.getElementById('muteBtn');
        if (muteBtn) muteBtn.classList.remove('on');
        const muteLabel = document.getElementById('muteLabel');
        if (muteLabel) muteLabel.textContent = 'Mute';
        const back = document.getElementById('backHeld');
        if (back) back.hidden = !heldCall;
        onHold = false;
        muted = false;
    }
    function toggleKeypad() {
        const pad = document.getElementById('dtmfPad');
        const view = document.getElementById('inCallView');
        pad.hidden = !pad.hidden;
        view.classList.toggle('keypad-open', !pad.hidden);
        document.getElementById('keypadBtn').classList.toggle('on', !pad.hidden);
    }
    function toggleHold() {
        if (!activeCall) return;
        if (activeCall._twilio) return setStatus('Hold is not available on this line yet', 'var(--danger)');
        onHold = !onHold;
        rtpStall = 0;
        iceStall = 0;
        try {
            if (onHold && activeCall.hold) activeCall.hold();
            if (!onHold && activeCall.unhold) activeCall.unhold();
        } catch (err) {}
        document.getElementById('holdBtn').classList.toggle('on', onHold);
        document.getElementById('holdLabel').textContent = onHold ? 'Resume' : 'Hold';
        if (onHold) showState('held');
        else { lastState = ''; activeConfirmed = true; showState('active'); }
        setStatus(onHold ? 'On hold' : 'Call resumed');
    }
    function findPc(call) {
        if (!call) return null;
        if (call.peerConnection instanceof RTCPeerConnection) return call.peerConnection;
        const peer = call.peer;
        if (peer) {
            if (peer.instance instanceof RTCPeerConnection) return peer.instance;
            if (peer.pc instanceof RTCPeerConnection) return peer.pc;
        }
        return null;
    }
    let mediaGrew = 0;
    let iceStall = 0;
    function sampleLevel(call) {
        const el = document.getElementById('remoteAudio');
        const stream = (el && el.srcObject) || call.remoteStream;
        if (!stream || !stream.getAudioTracks || !stream.getAudioTracks().length) return;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        if (tappedStream !== stream) {
            if (audioCtx) audioCtx.close().catch(() => {});
            audioCtx = new Ctx();
            const src = audioCtx.createMediaStreamSource(stream);
            analyser = audioCtx.createAnalyser();
            analyser.fftSize = 512;
            src.connect(analyser);
            tappedStream = stream;
            heardAudio = false;
            silentTicks = 0;
        }
        if (audioCtx.state !== 'running') {
            audioCtx.resume().catch(() => {});
            return;
        }
        const buf = new Uint8Array(analyser.fftSize);
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
            const v = (buf[i] - 128) / 128;
            sum += v * v;
        }
        const rms = Math.sqrt(sum / buf.length);
        if (rms > 0.02) {
            heardAudio = true;
            silentTicks = 0;
        } else if (heardAudio) {
            silentTicks += 1;
        }
    }
    async function mediaStopped(call) {
        const pc = findPc(call);
        let ice = 'no-pc';
        if (pc && pc.getStats) {
            ice = pc.iceConnectionState || pc.connectionState || 'pc';
            if (!onHold && (['disconnected', 'failed', 'closed'].includes(pc.iceConnectionState) || ['failed', 'closed'].includes(pc.connectionState))) {
                iceStall += 1;
                if (iceStall >= 3 && !ended) markEnded('Call ended');
            } else {
                iceStall = 0;
            }
            try {
                const stats = await pc.getStats();
                let bytes = 0;
                stats.forEach((row) => {
                    if (row.type === 'inbound-rtp' && (row.kind === 'audio' || row.mediaType === 'audio')) bytes += row.bytesReceived || 0;
                });
                if (bytes > rtpPackets) {
                    mediaGrew += 1;
                    rtpPackets = bytes;
                    rtpStall = 0;
                } else if (!onHold && mediaGrew >= 2 && bytes > 0 && bytes === rtpPackets) {
                    rtpStall += 1;
                    if (rtpStall >= 3 && !ended) markEnded('Call ended');
                }
            } catch (err) {}
        }
        sampleLevel(call);
        const stream = call.remoteStream || (document.getElementById('remoteAudio') && document.getElementById('remoteAudio').srcObject);
        if (!onHold && stream && stream.getAudioTracks && stream.getAudioTracks().some((t) => t.readyState === 'ended') && !ended) {
            markEnded('Call ended');
        }
        if (!ended) rtcStatus.textContent = document.getElementById('activityTitle').textContent;
    }
    setInterval(() => {
        if (!activeCall || ended) return;
        if (!onHold && callIsDead(activeCall)) {
            markEnded(endReason(activeCall));
            return;
        }
        watchRemoteEnd(activeCall);
        mediaStopped(activeCall);
        if (activeCall && activeCall.state) showState(String(activeCall.state).toLowerCase());
    }, 700);
    document.addEventListener('keydown', (e) => {
        if (document.activeElement === input && e.key === 'Enter') makeCall();
    });
    const NO_LINE = cfg.dataset.nocall === '1';
    if (NO_LINE) {
        rtcStatus.textContent = 'No calling number';
    } else if (!EXPIRED) boot();
    else {
        rtcStatus.textContent = 'Subscription expired';
        setStatus('Subscription expired. Ask your admin to renew it.', 'var(--danger)');
    }

// Local WebRTC teardown on server-side session revocation.
window.addEventListener('voxsbridge:session-revoked', () => {
    try { if (incomingCall?.hangup) incomingCall.hangup(); } catch (_) {}
    try { if (activeCall?.hangup) activeCall.hangup(); } catch (_) {}
    try { if (heldCall?.hangup) heldCall.hangup(); } catch (_) {}
    try { if (client?.disconnect) client.disconnect(); } catch (_) {}
    try { if (client?.destroy) client.destroy(); } catch (_) {}
});


// Power Dialer -> existing Telnyx WebRTC dialer bridge.
// Power Dialer must authorize each opted-in lead server-side before calling dial().
window.VoxsBridgeDialer = {
    ready: () => Boolean(client && rtcStatus && rtcStatus.textContent === 'Ready' &&
        !activeCall && !incomingCall && !heldCall && !placing && !EXPIRED),
    busy: () => Boolean(activeCall || incomingCall || heldCall || placing),
    dial: async (number) => {
        if (!window.VoxsBridgeDialer.ready()) {
            throw new Error('Dialer is not ready or another call is active');
        }
        if (!/^\+[1-9]\d{6,14}$/.test(String(number))) {
            throw new Error('Invalid E.164 phone number');
        }
        input.value = number;
        await makeCall();
        if (!activeCall || ended) throw new Error('Call could not be started');
        return true;
    }
};