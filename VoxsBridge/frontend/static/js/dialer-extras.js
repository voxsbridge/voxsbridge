/* Dialer extras: premium number box (country hint, clear button) and the
 * ringtones offered in Calling Preferences. */
(function () {
    'use strict';

    // ---- Number box -------------------------------------------------------
    const COUNTRIES = [
        ['1', 'US', 'United States / Canada'], ['44', 'GB', 'United Kingdom'], ['61', 'AU', 'Australia'],
        ['81', 'JP', 'Japan'], ['92', 'PK', 'Pakistan'], ['971', 'AE', 'United Arab Emirates'],
        ['966', 'SA', 'Saudi Arabia'], ['974', 'QA', 'Qatar'], ['965', 'KW', 'Kuwait'], ['968', 'OM', 'Oman'],
        ['973', 'BH', 'Bahrain'], ['91', 'IN', 'India'], ['880', 'BD', 'Bangladesh'], ['353', 'IE', 'Ireland'],
        ['49', 'DE', 'Germany'], ['33', 'FR', 'France'], ['34', 'ES', 'Spain'], ['39', 'IT', 'Italy'],
        ['31', 'NL', 'Netherlands'], ['32', 'BE', 'Belgium'], ['41', 'CH', 'Switzerland'], ['46', 'SE', 'Sweden'],
        ['47', 'NO', 'Norway'], ['45', 'DK', 'Denmark'], ['64', 'NZ', 'New Zealand'], ['65', 'SG', 'Singapore'],
        ['60', 'MY', 'Malaysia'], ['27', 'ZA', 'South Africa'], ['90', 'TR', 'Turkey'], ['86', 'CN', 'China'],
        ['852', 'HK', 'Hong Kong'], ['52', 'MX', 'Mexico'], ['55', 'BR', 'Brazil'],
    ].sort((a, b) => b[0].length - a[0].length);   // longest code first

    const input = document.getElementById('numberInput');
    const cc = document.getElementById('pnCc');
    const hint = document.getElementById('pnHint');
    const clear = document.getElementById('pnClear');
    const field = input && input.closest('.pn-field');

    function detect(value) {
        const digits = String(value || '').replace(/[^\d+]/g, '');
        if (!digits.startsWith('+')) return null;
        const body = digits.slice(1);
        return COUNTRIES.find(([code]) => body.startsWith(code)) || null;
    }
    let last = null;
    function refresh() {
        if (!input) return;
        const value = input.value;
        if (value === last) return;
        last = value;
        const found = detect(value);
        if (cc) cc.textContent = found ? found[1] : '+';
        if (field) field.classList.toggle('has-country', !!found);
        if (hint) {
            if (!value) hint.textContent = 'Add the country code, e.g. +44';
            else if (found) hint.textContent = found[2] + ' · +' + found[0];
            else if (!value.trim().startsWith('+')) hint.textContent = 'Tip: start with + and the country code';
            else hint.textContent = 'Country code not recognised — check the number';
        }
        if (clear) clear.hidden = !value;
        const paste = document.getElementById('dialPasteButton');
        if (paste) paste.hidden = !!value;
        const len = value.replace(/\s/g, '').length;      // shrink long numbers so they fit
        if (field) { field.classList.toggle('pn-long', len > 9); field.classList.toggle('pn-xlong', len > 12); }
    }
    if (input) {
        input.addEventListener('input', refresh);
        // The keypad, paste and "Call" links set the value from code, so watch it too.
        setInterval(refresh, 250);
        refresh();
    }
    clear && clear.addEventListener('click', () => { input.value = ''; refresh(); input.focus(); });

    // ---- Ringtones (generated in the browser, no audio files needed) -------
    const TONES = {
        classic: { label: 'Classic', pattern: [[440, 480, 2000]], gap: 2000 },            // US style ring
        uk:      { label: 'UK double ring', pattern: [[400, 450, 400], [0, 0, 200], [400, 450, 400]], gap: 2000 },
        digital: { label: 'Digital', pattern: [[1200, 0, 120], [0, 0, 80], [1200, 0, 120], [0, 0, 80], [1200, 0, 120]], gap: 1200 },
        chime:   { label: 'Soft chime', pattern: [[659, 0, 260], [784, 0, 260], [988, 0, 520]], gap: 1600, soft: true },
        pulse:   { label: 'Pulse', pattern: [[520, 0, 180], [0, 0, 120], [620, 0, 180]], gap: 1400 },
        silent:  { label: 'Silent (visual only)', pattern: [], gap: 0 },
    };

    function start(name, volume) {
        const tone = TONES[name] || TONES.classic;
        if (!tone.pattern.length) return () => {};
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return () => {};
        const ctx = new Ctx();
        const master = ctx.createGain();
        master.gain.value = Math.max(0, Math.min(100, volume || 70)) / 100 * 0.12;
        master.connect(ctx.destination);
        let stopped = false;
        let timer = null;
        function cycle() {
            if (stopped) return;
            let t = ctx.currentTime + 0.02;
            tone.pattern.forEach(([f1, f2, ms]) => {
                const dur = ms / 1000;
                [f1, f2].filter(Boolean).forEach((freq) => {
                    const osc = ctx.createOscillator();
                    const env = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = freq;
                    env.gain.setValueAtTime(0, t);
                    env.gain.linearRampToValueAtTime(1, t + 0.015);
                    if (tone.soft) env.gain.exponentialRampToValueAtTime(0.001, t + dur);
                    else env.gain.setValueAtTime(1, t + dur - 0.02);
                    env.gain.linearRampToValueAtTime(0, t + dur);
                    osc.connect(env); env.connect(master);
                    osc.start(t); osc.stop(t + dur + 0.02);
                });
                t += dur;
            });
            const total = tone.pattern.reduce((s, p) => s + p[2], 0) + tone.gap;
            timer = setTimeout(cycle, total);
        }
        if (ctx.state !== 'running') ctx.resume().catch(() => {});
        cycle();
        return () => { stopped = true; clearTimeout(timer); ctx.close().catch(() => {}); };
    }

    window.VoxsBridgeRing = { start, tones: TONES };
})();

/* Live presence for the company owner's Team dashboard:
 * online / dialing / on_call / ringing, sent on every change and every 15 s. */
(function () {
    'use strict';
    const cfg = document.getElementById('dialerConfig');
    if (!cfg) return;
    function snapshot() {
        const s = { status: 'online', number: '', direction: '', started_at: 0 };
        try {
            /* global activeCall, incomingCall, ended, talkStart */
            if (typeof incomingCall !== 'undefined' && incomingCall) {
                s.status = 'ringing';
                s.direction = 'incoming';
                s.number = (document.getElementById('incomingNumber') || {}).textContent || '';
            } else if (typeof activeCall !== 'undefined' && activeCall && !(typeof ended !== 'undefined' && ended)) {
                const started = typeof talkStart !== 'undefined' ? talkStart : 0;
                s.status = started ? 'on_call' : 'dialing';
                s.started_at = started || 0;
                s.number = (document.getElementById('inCallNumber') || {}).textContent || '';
                const dir = String(activeCall.direction || '').toLowerCase();
                s.direction = dir.startsWith('in') ? 'incoming' : 'outgoing';
            }
        } catch (e) {}
        return s;
    }
    let lastKey = '';
    let lastSent = 0;
    function send(force) {
        const s = snapshot();
        const key = s.status + '|' + s.number + '|' + s.started_at;
        const now = Date.now();
        if (!force && key === lastKey && now - lastSent < 15000) return;
        lastKey = key;
        lastSent = now;
        fetch('/api/presence', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(s), keepalive: true }).catch(() => {});
    }
    setInterval(() => send(false), 1000);
    send(true);
    window.addEventListener('pagehide', () => {
        try {
            fetch('/api/presence', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: 'offline' }), keepalive: true });
        } catch (e) {}
    });
})();
