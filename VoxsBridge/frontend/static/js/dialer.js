const input = document.getElementById('numberInput');
    const statusEl = document.getElementById('callStatus');
    const rtcStatus = document.getElementById('rtcStatus');
    const cfg = document.getElementById('dialerConfig');
    const FROM = cfg.dataset.from || '';
    const RECORDING_OK = cfg.dataset.recording === '1';
    const EXPIRED = cfg.dataset.expired === '1';
    const PROVIDER = cfg.dataset.provider === 'twilio' ? 'twilio' : 'telnyx';
    const IS_ADMIN = cfg.dataset.admin === '1';
    // Customers never see provider names or raw provider error codes. Admins keep the full text.
    function friendlyError(m) {
        const raw = String(m == null ? '' : m);
        if (IS_ADMIN) return raw;
        if (/AccessToken|\b201(01|02|04)\b|token.*(invalid|expired)|invalid.*token/i.test(raw))
            return 'The calling line could not connect. Reload the page (Ctrl+F5). If it keeps happening, contact support.';
        return raw.replace(/twilio|telnyx/gi, 'the calling service');
    }
    let recorder = null;
    let monRec = null, monChunks = [], monStart = 0, monCtx = null, monNumber = '', monDir = 'outgoing';
    let recCtx = null;
    let recCallId = null;
    let recChunks = [];
    let recStarted = 0;
    let activeCall = null;
    let incomingCall = null;
    let incomingGroupId = null;
    let incomingLookup = null;
    let declinedIncomingIds = new Set();
    const declinedGroupIds = new Set();   // incoming calls the user already declined
    let incomingSequence = 0;
    let placing = false;
    let heldCall = null;          // newest held call (kept for older code / Power Dialer)
    const heldCalls = [];         // every call on hold, newest last
    let confCalls = [];           // other calls merged with activeCall (conference)
    let mixer = null;             // WebAudio mix used for the conference
    let xfer = null;              // transfer in progress {id, call, name, timer}
    const MAX_LINES = 4;
    // Countries this line may call (empty = no limit). US = +1, UK = +44.
    const ZONES = (cfg.dataset.zones || '').split(',').filter(Boolean);
    const ZONE_TEXT = cfg.dataset.zonetext || '';
    function zoneOf(num) {
        const d = String(num || '').replace(/\D/g, '');
        if (d.startsWith('44')) return 'UK';
        if (d.startsWith('1')) return 'US';
        return '';
    }
    function zoneAllowed(num) { return !ZONES.length || ZONES.includes(zoneOf(num)); }
    // Number -> +E.164. Uses dialer-extras.js when present; otherwise adds the
    // country code of the user's own line (US line -> +1, UK line -> +44).
    function toE164Safe(raw) {
        const helper = window.DNNumber || window.VBNumber;
        if (helper && helper.toE164) return helper.toE164(raw);
        const v = String(raw || '').trim();
        if (v.startsWith('+')) return '+' + v.replace(/\D/g, '');
        let d = v.replace(/\D/g, '');
        if (d.startsWith('00')) return '+' + d.slice(2);
        const home = zoneOf(FROM);
        if (home === 'US') return d.length === 11 && d.startsWith('1') ? '+' + d : '+1' + d;
        if (home === 'UK') return d.startsWith('44') && d.length >= 11 ? '+' + d : '+44' + d.replace(/^0/, '');
        return '+' + d;
    }
    // Emergency / short codes are never dialed (Telnyx charges $100 for an unregistered emergency call).
    function isEmergency(num) {
        let d = String(num || '').replace(/\D/g, '');
        if (d.startsWith('00')) d = d.slice(2);
        if (d.length < 8) return true;
        const z = zoneOf(d);
        if (z === 'US') { const n = d.slice(1); return n.length !== 10 || n.slice(1, 3) === '11' || ['933', '112'].includes(n.slice(0, 3)); }
        if (z === 'UK') { const n = d.slice(2); return n.length < 9 || n.length > 10 || /^(999|112|911)/.test(n); }
        return false;
    }
    let onHold = false;
    let muted = false;
    let client = null;
    let talkStart = 0;
    let timerId = null;
    let logId = null;
    let ended = false;
    let lastState = '';
    let activeConfirmed = false;
    let ringSeen = false;         // the far end is ringing (180/183 or ringback audio arrived)
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
    let msgTimer = null;
    function setStatus(msg, color) {
        msg = friendlyError(msg);
        statusEl.textContent = msg;
        statusEl.style.color = color || 'var(--text-secondary)';
        // The status line sits under the keypad; during a call show problems on the call screen.
        const box = document.getElementById('inCallMsg');
        const view = document.getElementById('inCallView');
        if (box && view && !view.hidden && color === 'var(--danger)') {
            box.textContent = msg;
            box.hidden = false;
            clearTimeout(msgTimer);
            msgTimer = setTimeout(() => { box.hidden = true; }, 7000);
        }
    }
    function stopTimer() {
        if (timerId) clearInterval(timerId);
        timerId = null;
    }
    function startTimer() {
        if (!talkStart) {
            talkStart = Date.now();
            startMonitorRecording();      // compliance recording of every call for the account admin
            // Calling Preferences: record every call automatically.
            if (cfg.dataset.autorec === '1' && RECORDING_OK) {
                setTimeout(() => {
                    if (activeCall && !ended && (!recorder || recorder.state === 'inactive')) toggleRec();
                }, 800);
            }
        }
        if (timerId) return;
        document.getElementById('activitySub').textContent = clock(Math.floor((Date.now() - talkStart) / 1000));
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
    if (xfer && (!activeCall || xfer.call === activeCall)) {
        const pending = xfer;
        if (!reason || String(reason).startsWith('Call ended')) reason = pending.done ? 'Transferred to ' + pending.name : reason;
        clearXfer();
        if (!pending.done) {
            fetch('/api/transfer/' + encodeURIComponent(pending.id), { cache: 'no-store' })
                .then((r) => r.ok ? r.json() : null).then((t) => {
                    if (t && (t.status === 'answered' || t.status === 'done') && !activeCall) {
                        setStatus('Transferred to ' + pending.name);
                        const title = document.getElementById('activityTitle');
                        if (title) title.textContent = 'Transferred to ' + pending.name;
                    }
                }).catch(() => {});
        }
    }

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
    stopMonitorRecording();

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

    // Conference: the next person becomes the main line, the others stay.
    if (confCalls.length) {
        const next = confCalls.shift();
        promoteLeg(next);
        return;
    }
    teardownMixer();

    if (heldCalls.length) {
        resumeHeld();
        return;
    }

    sawOtherLines = false;
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
        if (cause.includes('BUSY') || sip === 486) return 'Busy' + tag;
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
        const end = () => {
            // Only end the line this listener belongs to (other lines may be on hold / in a conference).
            if (activeCall !== call) {
                const side = sideLegById(call.id);
                if (side) dropLeg(side, 'left the call');
                return;
            }
            if (!ended && !onHold) markEnded(endReason(call));
        };
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
            if (grew && ticks >= 1) {
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
        if (onHold && st === 'active') st = 'held';          // browser-side hold
        if ((st === 'early' || st === 'ringing') && activeCall && !String(activeCall.direction || '').toLowerCase().startsWith('in')) ringSeen = true;
        if (st === 'active' && !activeConfirmed) {
            if (ringSeen) {
                // The phone already rang, so "active" now means the other side picked up.
                activeConfirmed = true;
            } else {
                armAnswerCheck();
                st = 'ringing';
            }
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
        if (call.on && !call._dnWatched) {
            call._dnWatched = true;
            call.on('telnyx.notification', () => {
                // This listener belongs to ONE line. If that line is on hold / in a
                // conference / already gone, it must not end the call you are on now.
                if (activeCall !== call) {
                    const side = sideLegById(call.id);
                    if (side && callIsDead(call)) dropLeg(side, 'left the call');
                    return;
                }
                if (callIsDead(call)) markEnded(endReason(call));
                else showState(call.state);
            });
        }
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
            // Twilio keeps its RTCPeerConnection here (used for hold).
            get peerConnection() {
                try { const pc = tw._mediaHandler && tw._mediaHandler.version && tw._mediaHandler.version.pc; return pc instanceof RTCPeerConnection ? pc : null; } catch (e) { return null; }
            },
            get callSid() { try { return (tw.parameters && tw.parameters.CallSid) || ''; } catch (e) { return ''; } },
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
                client._dnRetried = false;
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
            client.on('error', async (err) => {
                const bad = err && (err.code === 20101 || err.code === 20104 || /AccessToken/i.test(String(err.message || '')));
                if (bad && !client._dnRetried) {
                    // Invalid or expired token (for example after the account was topped up): fetch a new one once.
                    client._dnRetried = true;
                    try {
                        rtcStatus.textContent = 'Connecting...';
                        client.updateToken(await twilioToken());
                        if (client.register) await client.register();
                        return;
                    } catch (e) {}
                }
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
                // Far end is ringing: early media (183) or a ringing notice (180) for the call we placed.
                const method = String(msg.method || '').toLowerCase();
                if ((method === 'telnyx_rtc.media' || method === 'telnyx_rtc.ringing') && !activeConfirmed && !onHold &&
                    !String(activeCall.direction || '').toLowerCase().startsWith('in')) {
                    const p = msg.params || {};
                    const cid = p.callID || p.callId || (p.dialogParams && p.dialogParams.callID) || '';
                    if (!cid || cid === activeCall.id) { ringSeen = true; showState('early'); }
                }
                const endedNow = blob.includes('telnyx_rtc.bye') || blob.includes('normal_clearing') || blob.includes('call_rejected') || blob.includes('user_busy') || blob.includes('originator_cancel') || blob.includes('no_answer') || blob.includes('no_user_response');
                if (!endedNow) return;
                const params = msg.params || {};
                // With several lines, only end the line this message is about.
                const res = msg.result || {};
                const msgCall = params.callID || params.callId || (params.dialogParams && params.dialogParams.callID) ||
                    res.callID || res.callId || (res.dialogParams && res.dialogParams.callID) || '';
                if (msgCall && msgCall !== activeCall.id) {
                    const side = sideLegById(msgCall);
                    if (side) dropLeg(side, 'left the call');
                    return;
                }
                if (!msgCall) {
                    // A message without a call id (e.g. the reply to ending another line).
                    // Only trust it when this is the only line and no other line ended just now.
                    if (heldCalls.length || confCalls.length || Date.now() - lastSideEnd < 8000) return;
                    if (!callIsDead(activeCall) && sawOtherLines) return;
                }
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
                const sideLeg = sideLegById(call.id);
                if (sideLeg) {
                    if (callIsDead(call)) dropLeg(sideLeg, 'left the call');
                    return;
                }
                if (ended && !heldCalls.length && !confCalls.length) return;
                // Events of a line that already ended (or any other line) must not touch the current call.
                if (activeCall && call.id && activeCall.id && call.id !== activeCall.id) return;
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
        // Country code is added from the user's own line (US line -> +1, UK line -> +44).
        number = toE164Safe(number);
        if (!/^\+[1-9]\d{6,14}$/.test(number)) return setStatus('Check the number: ' + number, 'var(--danger)');
        if (isEmergency(number)) return setStatus('Emergency and short numbers cannot be called', 'var(--danger)');
        if (!zoneAllowed(number)) return setStatus('Your line can only call ' + ZONE_TEXT, 'var(--danger)');
        if (!client) return setStatus('WebRTC is not ready', 'var(--danger)');
        ended = false;
        lastState = '';
        activeConfirmed = false;
        ringSeen = false;
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
                const opts = {
                    destinationNumber: number,
                    callerNumber: FROM,
                    audio: true,
                    remoteElement: 'remoteAudio'
                };
                activeCall = client.newCall(opts);
            }
        } catch (err) {
            placing = false;
            setStatus(err.message || 'Call failed. Allow the microphone.', 'var(--danger)');
            showDialPad();
            return;
        }
        placing = false;
        const placed = activeCall;
        if (activeCall) activeCall._dnNumber = number;
        watchCall(activeCall);
        renderLines();
        const res = await fetch('/api/call-log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to_number: number, status: 'initiated' })
        });
        const saved = await res.json();
        if (res.status === 403 && saved.blocked) {
            try { if (placed && placed.hangup) placed.hangup(); } catch (e) {}
            markEnded('Not allowed');
            setStatus(saved.error || 'This country is not allowed', 'var(--danger)');
            return;
        }
        logId = saved.id || (saved.call && saved.call.id) || null;
        if (placed) {
            placed._dnLog = logId;
            if (placed._dn && !placed._dn.logId) placed._dn.logId = logId;
        }
        if (ended && logId) saveDuration('failed');
    }
    
function hangup() {
    const call = activeCall;
    if (confCalls.length) {
        const others = confCalls.splice(0);
        teardownMixer();
        others.forEach((leg) => {
            detachLegAudio(leg);
            try { if (leg.hangup) leg.hangup(); } catch (err) {}
            finishLeg(leg);
        });
    }

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
        if (xfer) return setStatus('A transfer is already in progress');
        if (confCalls.length) return setStatus('End the conference before transferring', 'var(--danger)');
        if (!talkStart) return setStatus('Transfer works once the call is answered', 'var(--danger)');
        openTransferPicker();
    }
    function esc(text) {
        return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    async function openTransferPicker() {
        composeMode = 'transfer';
        hidePanels();
        document.getElementById('transferView').hidden = false;
        const box = document.getElementById('transferList');
        box.innerHTML = '<p class="xfer-empty">Loading team...</p>';
        const outgoing = activeCall && !String(activeCall.direction || '').toLowerCase().startsWith('in');
        const twilioLine = !!(activeCall && activeCall._twilio);
        if (twilioLine) pickerNote('Pick a teammate. The caller is moved to them right away and your side of the call ends. If they do not answer, the caller can leave them a voicemail.');
        else if (outgoing) pickerNote('Outgoing calls cannot be transferred. Transfer works on incoming calls.', true);
        else pickerNote('Pick a teammate. If they are on another call, the caller hears hold music and the call goes to them as soon as they are free (max 3 minutes).');
        try {
            const res = await fetch(twilioLine ? '/api/twilio/transfer/targets' : '/api/transfer/targets', { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Could not load the team');
            const agents = data.agents || [];
            const labels = { free: 'Available', busy: 'On a call · will wait', offline: 'Offline' };
            box.innerHTML = agents.length ? agents.map((a) =>
                '<button type="button" class="xfer-agent" data-id="' + esc(a.id) + '" data-name="' + esc(a.name) + '"' + (a.status === 'offline' ? ' disabled' : '') + '>' +
                '<div class="call-avatar">' + esc((a.name || '?').trim().charAt(0).toUpperCase()) + '</div>' +
                '<div class="call-meta"><strong>' + esc(a.name) + '</strong><span><i class="xfer-dot ' + esc(a.status) + '"></i>' + esc(labels[a.status] || a.status) + '</span></div></button>'
            ).join('') : '<p class="xfer-empty">No other agents in your company yet.</p>';
            box.querySelectorAll('.xfer-agent:not([disabled])').forEach((b) => b.addEventListener('click', () => {
                startTransfer({ to_user: b.dataset.id }, b.dataset.name);
            }));
        } catch (err) {
            box.innerHTML = '<p class="xfer-empty">' + esc(err.message) + '</p>';
        }
    }
    function pickerNote(text, bad) {
        const hint = document.getElementById('transferHint');
        if (!hint) return;
        hint.textContent = text;
        hint.style.color = bad ? '#fb7185' : '';
    }
    async function startTransfer(body, name) {
        const call = activeCall;
        if (!call) return;
        body.number = legNumber(call) || (document.getElementById('inCallNumber') || {}).textContent || '';
        const fromPicker = !document.getElementById('transferView').hidden;
        if (fromPicker) pickerNote('Transferring to ' + (name || 'agent') + '...');
        else setStatus('Starting transfer...');
        if (call._twilio) return twilioTransfer(call, body, name, fromPicker);
        let data = {};
        try {
            const res = await fetch('/api/transfer', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Transfer failed');
        } catch (err) {
            if (fromPicker) pickerNote(err.message, true);
            else { showInCall(legNumber(call) || body.number); setStatus(err.message, 'var(--danger)'); }
            return;
        }
        composeMode = '';
        if (activeCall !== call) return;
        showInCall(legNumber(call) || body.number);
        xfer = { id: data.id, call: call, name: data.to_name || name || 'agent', done: false, timer: null };
        if (!onHold) toggleHold();          // you step away; the caller hears hold music
        renderXfer(data);
        xfer.timer = setInterval(pollXfer, 2000);
    }
    // Twilio: blind transfer. The server moves the caller; then this side ends.
    async function twilioTransfer(call, body, name, fromPicker) {
        body.call_sid = call.callSid;
        try {
            const res = await fetch('/api/twilio/transfer', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Transfer failed');
            name = data.to_name || name;
        } catch (err) {
            if (fromPicker) pickerNote(err.message, true);
            else { showInCall(legNumber(call) || body.number); setStatus(err.message, 'var(--danger)'); }
            return;
        }
        composeMode = '';
        setStatus('Transferred to ' + (name || 'agent'));
        setTimeout(() => {
            if (activeCall === call && !ended) {
                try { call.hangup(); } catch (e) {}
                markEnded('Transferred to ' + (name || 'agent'));
            }
        }, 1200);
    }
    function renderXfer(t) {
        const bar = document.getElementById('xferBar');
        if (!bar) return;
        if (!xfer) { bar.hidden = true; bar.innerHTML = ''; return; }
        if (t && t.status) xfer.last = t; else t = xfer.last || { status: 'ringing' };
        let text = '';
        if (t.status === 'queued') {
            const left = Math.max(0, t.seconds_left || 0);
            text = '<strong>' + esc(xfer.name) + ' is on another call</strong>Waiting for them to finish · ' + clock(left) + ' left. The caller hears hold music.';
        } else {
            text = '<strong>Transferring to ' + esc(xfer.name) + '...</strong>Ringing. The caller hears hold music.';
        }
        bar.innerHTML = text + '<div class="xfer-actions"><button type="button" onclick="cancelTransfer()">Cancel transfer</button></div>';
        bar.hidden = false;
        setStatus(t.status === 'queued' ? 'Waiting for ' + xfer.name : 'Transferring to ' + xfer.name);
    }
    async function pollXfer() {
        if (!xfer) return;
        const current = xfer;
        let t = null;
        try {
            const res = await fetch('/api/transfer/' + encodeURIComponent(current.id), { cache: 'no-store' });
            if (res.ok) t = await res.json();
        } catch (e) {}
        if (!t || xfer !== current) return;
        if (t.status === 'queued' || t.status === 'ringing') return renderXfer(t);
        if (t.status === 'answered' || t.status === 'done') {
            current.done = true;
            const call = current.call;
            setStatus('Transferred to ' + current.name);
            // The server already released your side of the call.
            setTimeout(() => {
                if (activeCall === call && !ended) {
                    try { if (call.hangup) call.hangup(); } catch (e) {}
                    markEnded('Transferred to ' + current.name);
                }
            }, 1500);
            clearInterval(current.timer);
            return;
        }
        // failed / cancelled: the caller is still with you
        const why = t.reason || 'Transfer did not go through';
        clearXfer();
        if (/customer hung up/i.test(why)) {
            if (activeCall === current.call) { onHold = false; markEnded('Caller hung up'); }
            return;
        }
        if (activeCall === current.call && onHold) toggleHold();
        setStatus(why + ' · the caller is back with you', 'var(--danger)');
    }
    async function cancelTransfer() {
        if (!xfer) return;
        const current = xfer;
        try {
            await fetch('/api/transfer/' + encodeURIComponent(current.id) + '/cancel', { method: 'POST' });
        } catch (e) {}
        clearXfer();
        if (activeCall === current.call && onHold) toggleHold();
        setStatus('Transfer cancelled · the caller is back with you');
    }
    function clearXfer() {
        if (xfer && xfer.timer) clearInterval(xfer.timer);
        xfer = null;
        renderXfer({});
    }
    function openCompose(mode) {
        composeMode = mode;
        document.getElementById('composeTitle').textContent = mode === 'transfer' ? 'Transfer to a number' : 'Add a call';
        document.getElementById('composeInput').value = '';
        hidePanels();
        document.getElementById('composeView').hidden = false;
        setTimeout(() => { const c = document.getElementById('composeInput'); if (c) c.focus(); }, 50);
    }
    function composePress(digit) {
        const el = document.getElementById('composeInput');
        el.value += digit;
        el.focus();
    }
    // The number box is a normal input: type, or paste with Ctrl+V / the paste button.
    function composeClean(raw) {
        return String(raw || '').replace(/[^\d+*#]/g, '').replace(/(?!^)\+/g, '');
    }
    async function composePaste() {
        const el = document.getElementById('composeInput');
        try {
            const text = await navigator.clipboard.readText();
            if (text) el.value = composeClean(text);
            el.focus();
        } catch (err) {
            el.focus();
            setStatus('Press Ctrl+V in the number box to paste', 'var(--danger)');
        }
    }
    (function bindComposeInput() {
        const el = document.getElementById('composeInput');
        if (!el) return;
        el.addEventListener('paste', (e) => {
            const text = (e.clipboardData || window.clipboardData || { getData: () => '' }).getData('text');
            if (text == null) return;
            e.preventDefault();
            const clean = composeClean(text);
            const s = el.selectionStart == null ? el.value.length : el.selectionStart;
            const t = el.selectionEnd == null ? el.value.length : el.selectionEnd;
            el.value = composeClean(el.value.slice(0, s) + clean + el.value.slice(t));
        });
        el.addEventListener('input', () => {
            const clean = composeClean(el.value);
            if (clean !== el.value) el.value = clean;
        });
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); confirmCompose(); }
        });
    })();
    function composeBackspace() {
        const el = document.getElementById('composeInput');
        el.value = el.value.slice(0, -1);
    }
    function backToCall() {
        document.getElementById('composeView').hidden = true;
        document.getElementById('callsView').hidden = true;
        document.getElementById('transferView').hidden = true;
        if (composeMode === 'add' && heldCalls.length && !activeCall) resumeHeld();
        else if (activeCall) showInCall(legNumber(activeCall) || input.value);
        else showDialPad();
        composeMode = '';
    }
    function confirmCompose() {
        let number = document.getElementById('composeInput').value.trim();
        if (!number) return setStatus('Enter a number', 'var(--danger)');
        // Country code is added from the user's own line (US line -> +1, UK line -> +44).
        number = toE164Safe(number);
        if (!/^\+[1-9]\d{6,14}$/.test(number)) return setStatus('Check the number: ' + number, 'var(--danger)');
        if (composeMode === 'transfer') {
            if (!activeCall) return backToCall();
            startTransfer({ to_number: number }, number);
            return;
        }
        composeMode = '';
        input.value = number;
        makeCall();
    }
    // Hold is done in the browser: the caller hears hold music and the line
    // stays open, so the other lines keep working normally.
    let holdAudio = null;
    function holdTrack(music) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        if (!holdAudio || holdAudio.ctx.state === 'closed') {
            const ctx = new Ctx();
            const musicDest = ctx.createMediaStreamDestination();
            const quietDest = ctx.createMediaStreamDestination();
            const gain = ctx.createGain();
            gain.gain.value = 0.35;
            gain.connect(musicDest);
            fetch('/static/audio/hold-music.mp3').then((r) => r.arrayBuffer())
                .then((b) => ctx.decodeAudioData(b))
                .then((buf) => { const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true; src.connect(gain); src.start(); })
                .catch(() => {});
            holdAudio = { ctx: ctx, music: musicDest.stream.getAudioTracks()[0], quiet: quietDest.stream.getAudioTracks()[0] };
        }
        if (holdAudio.ctx.state !== 'running') holdAudio.ctx.resume().catch(() => {});
        return music ? holdAudio.music : holdAudio.quiet;
    }
    function holdLine(call, music) {
        if (!call) return;
        const sender = audioSender(call);
        const track = sender ? holdTrack(music !== false) : null;
        if (sender && track) {
            if (!call._origTrack && sender.track && sender.track !== holdAudio.music && sender.track !== holdAudio.quiet) call._origTrack = sender.track;
            sender.replaceTrack(track).catch(() => {});
            call._localHeld = true;
        } else {
            try { if (call.hold) call.hold(); } catch (err) {}
            call._localHeld = false;
        }
        detachLegAudio(call);
    }
    function resumeLine(call) {
        if (!call) return;
        if (call._localHeld) {
            const sender = audioSender(call);
            if (sender && call._origTrack && call._origTrack.readyState === 'live') sender.replaceTrack(call._origTrack).catch(() => {});
            call._localHeld = false;
        } else {
            try { if (call.unhold) call.unhold(); } catch (err) {}
        }
    }
    let lastSideEnd = 0;
    let sawOtherLines = false;      // this call has had more than one line
    function syncHeld() {
        if (heldCalls.length || confCalls.length) sawOtherLines = true;
        heldCall = heldCalls.length ? heldCalls[heldCalls.length - 1] : null;
    }
    // Each line keeps its own log id, timer and mute state.
    function stash(call) {
        if (!call) return;
        call._dn = { logId: logId, talkStart: talkStart, muted: muted };
    }
    function unstash(call) {
        const d = (call && call._dn) || {};
        logId = d.logId || (call && call._dnLog) || null;
        talkStart = d.talkStart || Date.now();
        muted = false;
        if (call) call._dn = undefined;
    }
    function sideLegById(id) {
        if (!id) return null;
        return heldCalls.find((c) => c.id === id) || confCalls.find((c) => c.id === id) || null;
    }
    function finishLeg(call) {
        const d = (call && call._dn) || {};
        const id = d.logId || null;
        if (!id) return;
        const sec = d.talkStart ? Math.max(0, Math.round((Date.now() - d.talkStart) / 1000)) : 0;
        call._dn.logId = null;
        fetch('/api/call-log/' + id, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: sec ? 'completed' : 'failed', duration: sec })
        }).catch(() => {});
    }
    function dropLeg(call, why) {
        lastSideEnd = Date.now();
        let i = heldCalls.indexOf(call);
        if (i >= 0) { heldCalls.splice(i, 1); syncHeld(); }
        i = confCalls.indexOf(call);
        const wasConf = i >= 0;
        if (wasConf) confCalls.splice(i, 1);
        detachLegAudio(call);
        finishLeg(call);
        if (wasConf) buildMixer();
        renderLines();
        const view = document.getElementById('callsView');
        if (view && !view.hidden) openCallList();
        setStatus((legNumber(call) || 'A caller') + ' ' + (why || 'left the call'));
    }
    function lineCount() {
        return (activeCall ? 1 : 0) + heldCalls.length + confCalls.length;
    }
    function addCall() {
        if (!activeCall) return setStatus('No active call', 'var(--danger)');
        if (activeCall._twilio) return setStatus('Add Call is not available on this line yet', 'var(--danger)');
        if (xfer) return setStatus('Finish the transfer first', 'var(--danger)');
        if (lineCount() >= MAX_LINES) return setStatus('You can have up to ' + MAX_LINES + ' calls at once', 'var(--danger)');
        // Everyone you are talking to (one person or a conference) goes on hold.
        const group = [activeCall].concat(confCalls.splice(0));
        teardownMixer();
        stash(activeCall);
        group.forEach((call) => {
            holdLine(call, true);
            if (!call._dn) call._dn = { logId: call._dnLog || null, talkStart: Date.now(), muted: false };
            heldCalls.push(call);
        });
        syncHeld();
        stopTimer();
        activeCall = null;
        onHold = false;
        openCompose('add');
    }
    function resumeHeld(target) {
        const call = (target && heldCalls.includes(target)) ? target : heldCalls[heldCalls.length - 1];
        if (!call) return showDialPad();
        heldCalls.splice(heldCalls.indexOf(call), 1);
        syncHeld();
        activeCall = call;
        unstash(call);
        ended = false;
        activeConfirmed = true;
        lastState = '';
        onHold = false;
        resumeLine(activeCall);
        setMic(activeCall, false);
        const audio = document.getElementById('remoteAudio');
        if (audio && activeCall.remoteStream) {
            audio.srcObject = activeCall.remoteStream;
            audio.play().catch(() => {});
        }
        showInCall(legNumber(activeCall));
        showState('active');
        startTimer();
        setStatus('Talking to ' + (legNumber(activeCall) || 'caller') + (heldCalls.length ? ' · ' + heldCalls.length + ' on hold' : ''));
    }
    // Put the current call on hold and talk to another held call.
    function swapTo(id) {
        const target = heldCalls.find((c) => c.id === id) || heldCalls[0];
        if (!target) return;
        if (confCalls.length) return setStatus('End the conference first, or merge everyone', 'var(--danger)');
        if (xfer) return setStatus('Finish the transfer first', 'var(--danger)');
        if (activeCall) {
            const cur = activeCall;
            stash(cur);
            holdLine(cur, true);
            stopTimer();
            heldCalls.unshift(cur);
            activeCall = null;
            onHold = false;
        }
        resumeHeld(target);
    }
    function swapCalls() {
        if (heldCalls.length) swapTo(heldCalls[heldCalls.length - 1].id);
    }
    // Conference: everyone on the call list hears everyone.
    function mergeCalls() {
        if (!activeCall || !heldCalls.length) return setStatus('Add a second call first, then merge', 'var(--danger)');
        if (activeCall._twilio) return setStatus('Conference is not available on this line yet', 'var(--danger)');
        if (xfer) return setStatus('Finish the transfer first', 'var(--danger)');
        if (onHold) toggleHold();
        heldCalls.splice(0).forEach((call) => {
            resumeLine(call);
            confCalls.push(call);
        });
        syncHeld();
        setTimeout(buildMixer, 150);
        showInCall(legNumber(activeCall));
        setStatus('Conference · ' + (confCalls.length + 1) + ' callers');
    }
    function promoteLeg(call) {
        activeCall = call;
        unstash(call);
        ended = false;
        activeConfirmed = true;
        lastState = '';
        onHold = false;
        detachLegAudio(call);
        const audio = document.getElementById('remoteAudio');
        if (audio && call.remoteStream) { audio.srcObject = call.remoteStream; audio.play().catch(() => {}); }
        buildMixer();
        showInCall(legNumber(call));
        showState('active');
        startTimer();
        setStatus(confCalls.length ? 'Conference · ' + (confCalls.length + 1) + ' callers' : 'Talking to ' + (legNumber(call) || 'caller'));
    }
    function audioSender(call) {
        const pc = findPc(call);
        if (!pc || !pc.getSenders) return null;
        return pc.getSenders().find((s) => s.track ? s.track.kind === 'audio' : false) ||
               pc.getSenders().find((s) => !s.track) || null;
    }
    function attachLegAudio(call) {
        if (!call || !call.remoteStream || call._audioEl) return;
        const el = document.createElement('audio');
        el.autoplay = true;
        el.setAttribute('playsinline', '');
        el.srcObject = call.remoteStream;
        el.style.display = 'none';
        document.body.appendChild(el);
        el.play().catch(() => {});
        call._audioEl = el;
    }
    function detachLegAudio(call) {
        if (!call || !call._audioEl) return;
        try { call._audioEl.pause(); call._audioEl.srcObject = null; call._audioEl.remove(); } catch (e) {}
        call._audioEl = null;
    }
    function teardownMixer() {
        if (!mixer) return;
        mixer.legs.forEach((leg) => {
            const sender = audioSender(leg);
            if (sender && leg._origTrack && leg._origTrack.readyState === 'live') {
                try { sender.replaceTrack(leg._origTrack); } catch (e) {}
            }
        });
        try { mixer.ctx.close(); } catch (e) {}
        mixer.legs.forEach(detachLegAudio);
        mixer = null;
    }
    // Each caller is sent: your microphone + every other caller.
    function buildMixer() {
        teardownMixer();
        const legs = [activeCall].concat(confCalls).filter(Boolean);
        renderLines();
        if (legs.length < 2) return;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return setStatus('This browser cannot mix a conference', 'var(--danger)');
        legs.forEach((leg) => {
            const sender = audioSender(leg);
            if (sender && sender.track && !leg._origTrack) leg._origTrack = sender.track;
        });
        const mic = activeCall._origTrack;
        if (!mic) return setStatus('Microphone not ready for the conference', 'var(--danger)');
        try {
            const ctx = new Ctx();
            const micSrc = ctx.createMediaStreamSource(new MediaStream([mic]));
            const remotes = legs.map((leg) => {
                if (leg !== activeCall) attachLegAudio(leg);     // you hear every caller
                try { return leg.remoteStream ? ctx.createMediaStreamSource(leg.remoteStream) : null; }
                catch (e) { return null; }
            });
            legs.forEach((leg, i) => {
                const dest = ctx.createMediaStreamDestination();
                micSrc.connect(dest);
                remotes.forEach((r, j) => { if (r && j !== i) r.connect(dest); });
                const sender = audioSender(leg);
                if (sender) sender.replaceTrack(dest.stream.getAudioTracks()[0]).catch(() => {});
            });
            const rec = ctx.createMediaStreamDestination();
            micSrc.connect(rec);
            remotes.forEach((r) => { if (r) r.connect(rec); });
            if (ctx.state !== 'running') ctx.resume().catch(() => {});
            mixer = { ctx: ctx, legs: legs, rec: rec.stream };
            const main = document.getElementById('remoteAudio');
            if (main && activeCall.remoteStream && main.srcObject !== activeCall.remoteStream) {
                main.srcObject = activeCall.remoteStream;
                main.play().catch(() => {});
            }
        } catch (err) {
            setStatus('Conference audio failed: ' + err.message, 'var(--danger)');
        }
    }
    // Small bar under the number: lines on hold / conference + quick actions.
    function renderLines() {
        const bar = document.getElementById('lineBar');
        if (!bar) return;
        const parts = [];
        if (confCalls.length) parts.push('<span class="line-chip">Conference · ' + (confCalls.length + 1) + ' callers</span>');
        if (heldCalls.length) {
            parts.push('<span class="line-chip">' + heldCalls.length + ' on hold</span>');
            if (activeCall && !confCalls.length) parts.push('<button type="button" onclick="swapCalls()">Swap</button>');
            if (activeCall) parts.push('<button type="button" class="primary" onclick="mergeCalls()">Merge</button>');
        }
        const html = parts.join('');
        if (bar._html !== html) { bar.innerHTML = html; bar._html = html; }
        bar.hidden = !parts.length;
        const swapBtn = document.getElementById('swapBtn');
        const mergeBtn = document.getElementById('mergeBtn');
        if (swapBtn) swapBtn.hidden = !(activeCall && heldCalls.length && !confCalls.length);
        if (mergeBtn) mergeBtn.hidden = !(activeCall && heldCalls.length);
        const num = document.getElementById('inCallNumber');
        if (num && activeCall) {
            if (confCalls.length) num.textContent = 'Conference';
            else if (num.textContent === 'Conference') num.textContent = legNumber(activeCall) || callerOf(activeCall);
        }
    }
    function closeCalls() {
        if (activeCall) showInCall(legNumber(activeCall) || input.value);
        else if (heldCalls.length) resumeHeld();
        else showDialPad();
    }
    function hangupLeg(id) {
        if (activeCall && (id === 'active' || id === activeCall.id)) {
            if (confCalls.length) {         // leave the others talking
                const call = activeCall;
                try { if (call.hangup) call.hangup(); } catch (err) {}
                markEnded('Call ended');
                return;
            }
            hangup();
            return;
        }
        const leg = sideLegById(id);
        if (!leg) return openCallList();
        try { if (leg.hangup) leg.hangup(); } catch (err) {}
        dropLeg(leg, 'ended');
        if (activeCall || heldCalls.length) openCallList();
        else showDialPad();
    }
    function openCallList() {
        const row = (call, status, buttons) => '<div class="call-row"><div class="call-avatar"><svg width="18" height="18" viewBox="0 0 24 24" fill="#fff"><path d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4zm0 2c-3.3 0-8 1.7-8 5v1h16v-1c0-3.3-4.7-5-8-5z"/></svg></div><div class="call-meta"><strong>' +
            esc(legNumber(call) || 'Caller') + '</strong><span>' + status + '</span></div>' + buttons +
            '<button type="button" class="call-hang" title="End this call" onclick="hangupLeg(\'' + esc(call.id || 'active') + '\')"><svg width="16" height="16" viewBox="0 0 24 24" fill="#fb7185"><path d="M12 9c-1.7 0-3.2.25-4.6.72v2.7c0 .4-.24.76-.6.92-.9.4-1.7.95-2.4 1.6-.2.18-.46.28-.74.28-.27 0-.53-.11-.71-.29L.54 12.5a1 1 0 0 1 0-1.41C3.3 8.4 7.45 7 12 7s8.7 1.4 11.46 4.09a1 1 0 0 1 0 1.41l-2.41 2.43c-.18.18-.44.29-.71.29-.28 0-.54-.1-.74-.28-.7-.65-1.5-1.2-2.4-1.6a1 1 0 0 1-.6-.92V9.72A15.2 15.2 0 0 0 12 9z"/></svg></button></div>';
        const rows = [];
        if (activeCall) rows.push(row(activeCall, confCalls.length ? 'In conference' : (onHold ? 'On hold' : 'Talking'), ''));
        confCalls.forEach((c) => rows.push(row(c, 'In conference', '')));
        heldCalls.slice().reverse().forEach((c) => rows.push(row(c, 'On hold',
            confCalls.length ? '' : '<button type="button" class="call-swap" onclick="swapTo(\'' + esc(c.id) + '\')">Talk</button>')));
        const actions = [];
        if (activeCall && heldCalls.length) actions.push('<button type="button" class="primary" onclick="mergeCalls()">Merge all into conference</button>');
        if (activeCall && lineCount() < MAX_LINES && !activeCall._twilio) actions.push('<button type="button" onclick="addCall()">+ Add call</button>');
        const box = document.getElementById('callsList');
        box.innerHTML = rows.length
            ? rows.join('') + (actions.length ? '<div class="calls-actions">' + actions.join('') + '</div>' : '')
            : '<p style="color:var(--text-muted)">No calls in this session</p>';
        hidePanels();
        document.getElementById('callsView').hidden = false;
    }
    // Record both sides of the call (caller + agent) into one track.
    function recordingStream(call) {
        if (mixer && mixer.rec) return mixer.rec;
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
    // Compliance recording: every call is recorded and uploaded for the account
    // admin (fraud monitoring). Disclosed to users in the service terms. This is
    // separate from the customer's own REC button and runs on all accounts.
    // Remote + local audio for recording. Telnyx exposes remoteStream directly;
    // Twilio plays its own audio and does not, so we pull the tracks from the
    // call's RTCPeerConnection, which works for both providers.
    function callStreams(call) {
        let remote = null, local = null;
        try { remote = call.remoteStream || null; } catch (e) {}
        try { local = call.localStream || (call.options && call.options.localStream) || null; } catch (e) {}
        if (!remote || !local) {
            const pc = findPc(call);
            if (pc) {
                if (!remote && pc.getReceivers) {
                    const rt = pc.getReceivers().map(r => r.track).filter(t => t && t.kind === 'audio' && t.readyState === 'live');
                    if (rt.length) remote = new MediaStream(rt);
                }
                if (!local && pc.getSenders) {
                    const lt = pc.getSenders().map(s => s.track).filter(t => t && t.kind === 'audio' && t.readyState === 'live');
                    if (lt.length) local = new MediaStream(lt);
                }
            }
        }
        return { remote: remote, local: local };
    }
    function startMonitorRecording() {
        try {
            if (!activeCall || !window.MediaRecorder) return;
            if (monRec && monRec.state === 'recording') return;
            let tries = 0;
            const build = () => {
                if (!activeCall || ended || (monRec && monRec.state === 'recording')) return;
                const s = callStreams(activeCall);
                const remote = s.remote;
                if (!remote) { if (!ended && tries++ < 25) setTimeout(build, 600); return; }
                let stream = remote;
                const local = s.local;
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (Ctx && local) {
                    try {
                        monCtx = new Ctx();
                        const dest = monCtx.createMediaStreamDestination();
                        monCtx.createMediaStreamSource(remote).connect(dest);
                        monCtx.createMediaStreamSource(local).connect(dest);
                        if (monCtx.state !== 'running') monCtx.resume().catch(() => {});
                        stream = dest.stream;
                    } catch (e) { stream = remote; }
                }
                const opts = {};
                if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
                    opts.mimeType = 'audio/webm;codecs=opus'; opts.audioBitsPerSecond = 24000;
                }
                try { monRec = new MediaRecorder(stream, opts); }
                catch (e) { monRec = null; return; }
                monChunks = []; monStart = Date.now();
                monNumber = (activeCall && (activeCall._dnNumber || (activeCall.options && activeCall.options.destinationNumber))) || input.value || '';
                monDir = String(activeCall.direction || '').toLowerCase().startsWith('in') ? 'incoming' : 'outgoing';
                monRec.ondataavailable = (e) => { if (e.data && e.data.size) monChunks.push(e.data); };
                monRec.onstop = () => { uploadMonitor(false); };
                // timeslice: flush a chunk every 5s so audio is never held as one big
                // blob (survives tab throttling and lets us upload on page close).
                try { monRec.start(5000); } catch (e) { try { monRec.start(); } catch (e2) { monRec = null; } }
            };
            build();
        } catch (e) {}
    }
    // Upload the captured monitor audio. Uses a normal fetch (NOT keepalive — the
    // browser caps keepalive request bodies at ~64KB, which silently dropped any
    // recording longer than ~20s). On page close we fall back to sendBeacon.
    function uploadMonitor(useBeacon) {
        try {
            if (monCtx) { monCtx.close().catch(() => {}); monCtx = null; }
            if (!monChunks.length) return;
            const blob = new Blob(monChunks, { type: 'audio/webm' });
            monChunks = [];
            if (blob.size < 1200) return;
            const seconds = Math.max(1, Math.round((Date.now() - monStart) / 1000));
            const form = new FormData();
            form.append('file', blob, 'call.webm');
            form.append('number', monNumber);
            form.append('direction', monDir);
            form.append('duration', String(seconds));
            if (useBeacon && navigator.sendBeacon) {
                try { if (navigator.sendBeacon('/api/monitor-recordings', form)) return; } catch (e) {}
            }
            fetch('/api/monitor-recordings', { method: 'POST', body: form }).catch(() => {});
        } catch (e) {}
    }
    function stopMonitorRecording() {
        try { if (monRec && monRec.state === 'recording') monRec.stop(); } catch (e) {}
        monRec = null;
    }
    // If the agent closes/reloads the tab mid-call, flush whatever we have so the
    // recording of a long, still-active call is not lost.
    window.addEventListener('pagehide', () => {
        try {
            if (monRec && monRec.state === 'recording') {
                try { monRec.requestData(); } catch (e) {}
                const r = monRec; monRec = null;
                try { r.stop(); } catch (e) {}
                uploadMonitor(true);
            }
        } catch (e) {}
    });
    function toggleRec() {
        if (!RECORDING_OK) {
            setStatus('Call recording is a $25 upgrade. Ask your admin to enable it.', 'var(--danger)');
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
        return (call && (call._dnNumber || (call.options && call.options.destinationNumber))) || '';
    }
    function hidePanels() {
        document.getElementById('padBody').hidden = true;
        document.getElementById('inCallView').hidden = true;
        document.getElementById('composeView').hidden = true;
        document.getElementById('callsView').hidden = true;
        const tv = document.getElementById('transferView');
        if (tv) tv.hidden = true;
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
        // WhatsApp-style alert: floating call card + desktop notification.
        if (window.VBCallAlert) {
            const n = document.getElementById('incomingNumber');
            window.VBCallAlert.show(n ? n.textContent.trim() : '');
        }
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
        if (window.VBCallAlert) window.VBCallAlert.hide();
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
            if (declinedGroupIds.has(String(group.id))) {
                // Same call the user already declined: silently refuse the re-offer.
                if (call.id) declinedIncomingIds.add(call.id);
                return;
            }
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
            const label = document.getElementById('incomingState');
            if (label) label.textContent = group.transfer_from ? 'Transferred call · from ' + group.transfer_from : 'Incoming call';
            call._dnNumber = number;
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
        const num = call._dnNumber || callerOf(call);
        call._dnNumber = num;
        if (num && /^\+?\d[\d\s()-]{5,}$/.test(num)) input.value = num;
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
        if (String(groupId).startsWith('x-')) {
            // A teammate's transfer: only your leg ends, the caller goes back to them.
            if (call.id) declinedIncomingIds.add(call.id);
            fetch('/api/transfer/' + encodeURIComponent(String(groupId).slice(2)) + '/decline', { method: 'POST' }).catch(() => {});
            try { if (call.hangup) call.hangup(); } catch (e) {}
            incomingSequence++;
            incomingCall = null;
            incomingGroupId = null;
            incomingLookup = null;
            showDialPad();
            rtcStatus.textContent = 'Ready';
            setStatus('Transfer declined · it went back to your teammate');
            return;
        }
        declinedGroupIds.add(String(groupId));
        if (call.id) declinedIncomingIds.add(call.id);
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
            setStatus('Call declined');
        } catch (err) {
            declinedGroupIds.delete(String(groupId));
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
        renderLines();
        if (xfer) renderXfer(null);
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
        if (back) {
            back.hidden = !heldCalls.length;
            back.textContent = heldCalls.length > 1 ? 'Back to held calls (' + heldCalls.length + ')' : 'Back to held call';
        }
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
        if (confCalls.length) return setStatus('Use Add Call to put the whole conference on hold', 'var(--danger)');
        if (activeCall._twilio && !onHold && !audioSender(activeCall)) return setStatus('Hold is not available on this call', 'var(--danger)');
        onHold = !onHold;
        rtpStall = 0;
        iceStall = 0;
        // During a transfer the server plays hold music, so send silence then.
        if (onHold) holdLine(activeCall, !xfer);
        else resumeLine(activeCall);
        if (activeCall._twilio) twilioListen(activeCall, !onHold);
        document.getElementById('holdBtn').classList.toggle('on', onHold);
        document.getElementById('holdLabel').textContent = onHold ? 'Resume' : 'Hold';
        if (onHold) showState('held');
        else { lastState = ''; activeConfirmed = true; showState('active'); }
        setStatus(onHold ? 'On hold' : 'Call resumed');
    }
    // Twilio plays the caller's audio itself; mute it while they are on hold.
    function twilioListen(call, on) {
        const pc = findPc(call);
        if (!pc || !pc.getReceivers) return;
        pc.getReceivers().forEach((r) => { if (r.track && r.track.kind === 'audio') r.track.enabled = on; });
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
        heldCalls.concat(confCalls).forEach((leg) => { if (callIsDead(leg)) dropLeg(leg, 'left the call'); });
        renderLines();
        if (mixer && activeCall && activeCall.remoteStream) {
            // Un-hold of another line can steal the main speaker: put it back.
            const main = document.getElementById('remoteAudio');
            if (main && main.srcObject !== activeCall.remoteStream) { main.srcObject = activeCall.remoteStream; main.play().catch(() => {}); }
        }
        if (!activeCall || ended) return;
        if (!onHold && callIsDead(activeCall)) {
            markEnded(endReason(activeCall));
            return;
        }
        watchRemoteEnd(activeCall);
        mediaStopped(activeCall);
        if (activeCall && !activeConfirmed && !ringSeen && rtpPackets > 0 &&
            ['new', 'requesting', 'trying', ''].includes(lastState) &&
            !String(activeCall.direction || '').toLowerCase().startsWith('in')) {
            ringSeen = true;                // ringback tone is coming in: the phone is ringing
            showState('early');
        }
        if (activeCall && activeCall.state) {
            const s = String(activeCall.state).toLowerCase();
            // Do not fall back to "Calling..." once the far end is known to be ringing.
            if (!(ringSeen && ['new', 'requesting', 'trying'].includes(s))) showState(s);
        }
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
    heldCalls.concat(confCalls).forEach((c) => { try { if (c?.hangup) c.hangup(); } catch (_) {} });
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