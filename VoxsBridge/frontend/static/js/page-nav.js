/* VoxsBridge in-app navigation.
 *
 * Sidebar links, Today/Week/Month buttons and other in-app page links load
 * only the page content (#pageMain + #pageScripts) instead of reloading the
 * whole document, so the dialer rail, the Telnyx/Twilio connection and any
 * active call keep running. "Call" links (/dialer?number=...) dial straight
 * from the rail. If anything goes wrong we fall back to a normal page load.
 */
(function () {
    'use strict';

    // ---- Page-scoped listener/interval tracking ---------------------------
    // Listeners that page scripts add to window/document are removed when the
    // user leaves that page, so old pages cannot react to new events.
    let capturing = 0;
    let tracked = [];
    let intervals = [];
    const nativeAdd = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, listener, options) {
        if (capturing > 0 && (this === window || this === document)) {
            tracked.push([this, type, listener, options]);
        }
        return nativeAdd.call(this, type, listener, options);
    };
    const nativeSetInterval = window.setInterval;
    window.setInterval = function () {
        const id = nativeSetInterval.apply(window, arguments);
        if (capturing > 0) intervals.push(id);
        return id;
    };

    window.VoxsBridgeNav = {
        capture(on) { capturing = Math.max(0, capturing + (on ? 1 : -1)); },
    };

    const PAGE_PREFIXES = ['/dashboard', '/call-logs', '/messages', '/voicemails', '/recordings',
        '/contacts', '/crm', '/power-dialer', '/campaigns', '/follow-ups', '/scripts', '/billing',
        '/customers', '/invoices', '/users', '/settings'];

    function isAppPage(url) {
        if (url.origin !== location.origin) return false;
        const path = url.pathname.replace(/\/+$/, '') || '/';
        if (/\/(pdf|audio)$/.test(path) || url.searchParams.has('download')) return false;
        return PAGE_PREFIXES.some((p) => path === p || path.startsWith(p + '/'));
    }

    function hardNavigate(url) {
        location.assign(url.href || url);
    }

    function leavePage() {
        try { window.dispatchEvent(new CustomEvent('voxsbridge:page-leave')); } catch (e) {}
        tracked.forEach(([target, type, listener, options]) => {
            try { target.removeEventListener(type, listener, options); } catch (e) {}
        });
        intervals.forEach((id) => clearInterval(id));
        tracked = [];
        intervals = [];
        document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch (e) {} });
        document.body.style.overflow = '';
        const menu = document.getElementById('profileMenu');
        if (menu) menu.hidden = true;
    }

    const scriptCache = new Map();   // static page scripts only change on deploy

    // Recreate <script> elements so the browser executes them, in order.
    // Same-origin files are fetched and run inline so that listener capture
    // covers exactly the page script and nothing else (e.g. the call SDK).
    function execInline(old, code, label) {
        const script = document.createElement('script');
        for (const attr of old.attributes) if (attr.name !== 'src') script.setAttribute(attr.name, attr.value);
        script.textContent = code + (label ? '\n//# sourceURL=' + label : '');
        capturing += 1;
        try { old.replaceWith(script); } finally { capturing = Math.max(0, capturing - 1); }
    }

    async function runScript(old) {
        if (old.hasAttribute('data-nav-marker')) { old.remove(); return; }
        const type = (old.getAttribute('type') || '').trim().toLowerCase();
        if (type && type !== 'text/javascript') return;  // JSON data blocks stay as they are
        if (!old.src) return execInline(old, old.textContent, '');
        const src = new URL(old.src, location.href);
        if (src.origin === location.origin) {
            if (!scriptCache.has(src.href)) {
                const res = await fetch(src.href, { credentials: 'same-origin' });
                if (!res.ok) throw new Error('Could not load ' + src.pathname);
                scriptCache.set(src.href, await res.text());
            }
            return execInline(old, scriptCache.get(src.href), src.href);
        }
        await new Promise((resolve) => {   // third-party file: load normally
            const script = document.createElement('script');
            for (const attr of old.attributes) script.setAttribute(attr.name, attr.value);
            script.async = false;
            script.onload = script.onerror = () => resolve();
            old.replaceWith(script);
        });
    }

    async function runScriptsIn(container) {
        for (const s of Array.from(container.querySelectorAll('script'))) {
            try { await runScript(s); } catch (e) { console.error('Page script failed', e); }
        }
    }

    let navSeq = 0;
    let currentKey = location.pathname + location.search;

    // ---- Fetching (with hover prefetch) ------------------------------------
    // Hovering or pressing a link starts loading the page, so by the time the
    // click lands most of the server time is already done.
    const PREFETCH_MAX_AGE = 10000;
    const prefetched = new Map();   // href -> {time, promise}

    function fetchPage(href) {
        return fetch(href, { credentials: 'same-origin', cache: 'no-store',
            headers: { 'X-Requested-With': 'voxsbridge-nav' } })
            .then(async (res) => ({
                ok: res.ok,
                finalUrl: new URL(res.url || href),
                type: res.headers.get('content-type') || '',
                html: res.ok ? await res.text() : '',
            }));
    }

    function prefetch(url) {
        const href = url.href;
        const hit = prefetched.get(href);
        if (hit && Date.now() - hit.time < PREFETCH_MAX_AGE) return;
        const entry = { time: Date.now(), promise: fetchPage(href) };
        entry.promise.catch(() => prefetched.delete(href));
        prefetched.set(href, entry);
    }

    function takePage(url) {
        const hit = prefetched.get(url.href);
        prefetched.delete(url.href);
        if (hit && Date.now() - hit.time < PREFETCH_MAX_AGE) return hit.promise;
        return fetchPage(url.href);
    }

    function markActive(url) {
        // Instant feedback: highlight the clicked menu item before content arrives.
        const links = document.querySelectorAll('.shell-nav a[href]');
        let match = null;
        links.forEach((a) => {
            const p = new URL(a.href, location.href).pathname;
            if (url.pathname === p || (p !== '/' && url.pathname.startsWith(p + '/'))) match = a;
        });
        if (!match) return;
        links.forEach((a) => a.classList.toggle('active', a === match));
    }

    async function navigate(url, push) {
        const main = document.getElementById('pageMain');
        const scriptsBox = document.getElementById('pageScripts');
        if (!main || !scriptsBox) return hardNavigate(url);
        const seq = ++navSeq;
        document.body.classList.add('nav-loading');
        main.setAttribute('aria-busy', 'true');
        markActive(url);
        let doc, finalUrl;
        try {
            const page = await takePage(url);
            finalUrl = page.finalUrl;
            if (!page.ok || !page.type.includes('text/html') || !isAppPage(finalUrl)) {
                // Login expired, error page or not an app page: load it normally.
                if (seq === navSeq) hardNavigate(page.ok ? finalUrl : url);
                return;
            }
            doc = new DOMParser().parseFromString(page.html, 'text/html');
        } catch (err) {
            if (seq === navSeq) hardNavigate(url);
            return;
        }
        if (seq !== navSeq) return;  // a newer click won
        const newMain = doc.getElementById('pageMain');
        const newScripts = doc.getElementById('pageScripts');
        if (!newMain || !newScripts) return hardNavigate(finalUrl);

        leavePage();
        document.title = doc.title || document.title;
        const nav = document.querySelector('.shell-nav nav');
        const newNav = doc.querySelector('.shell-nav nav');
        if (nav && newNav) nav.innerHTML = newNav.innerHTML;
        const settings = document.querySelector('.shell-nav .nav-settings');
        const newSettings = doc.querySelector('.shell-nav .nav-settings');
        if (settings && newSettings) settings.className = newSettings.className;

        const target = finalUrl.href.split('#')[0] + (url.hash || '');   // keep #anchors
        if (push) history.pushState({ dnNav: true }, '', target);
        else if (target !== location.href) history.replaceState({ dnNav: true }, '', target);
        currentKey = location.pathname + location.search;

        main.innerHTML = newMain.innerHTML;
        scriptsBox.innerHTML = newScripts.innerHTML;
        main.removeAttribute('aria-busy');
        document.body.classList.remove('nav-loading');
        main.scrollTop = 0;
        window.scrollTo(0, 0);

        await runScriptsIn(main);
        await runScriptsIn(scriptsBox);
        try { window.dispatchEvent(new CustomEvent('voxsbridge:page-load')); } catch (e) {}
        const heading = main.querySelector('h1');
        if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); }
    }

    // "Call" links: dial from the rail without leaving the page.
    function dialFromLink(url) {
        const number = (url.searchParams.get('number') || '').replace(/[\s().-]/g, '');
        if (!/^\+[1-9]\d{6,14}$/.test(number)) return false;
        const dialer = window.VoxsBridgeDialer;
        const input = document.getElementById('numberInput');
        const say = (msg, bad) => {
            if (typeof window.setStatus === 'function') window.setStatus(msg, bad ? 'var(--danger)' : undefined);
        };
        if (!dialer || !input) return false;
        try { window.dispatchEvent(new CustomEvent('voxsbridge:show-dialer')); } catch (e) {}
        if (dialer.busy()) { say('Another call is active. Finish it before calling ' + number + '.', true); return true; }
        input.value = number;
        if (!dialer.ready()) { say('Dialer is still connecting. Press the green button when it shows Ready.', true); return true; }
        dialer.dial(number).catch((err) => say(err.message || 'Call could not be started', true));
        return true;
    }

    document.addEventListener('click', (event) => {
        if (event.defaultPrevented || event.button !== 0 ||
            event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest && event.target.closest('a[href]');
        if (!link || link.hasAttribute('download')) return;
        const target = link.getAttribute('target');
        if (target && target !== '_self') return;
        let url;
        try { url = new URL(link.href, location.href); } catch (e) { return; }
        if (url.origin !== location.origin) return;
        if (url.pathname === '/dialer' && url.searchParams.has('number')) {
            if (dialFromLink(url)) event.preventDefault();
            return;
        }
        if (!isAppPage(url)) return;
        if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
        event.preventDefault();
        navigate(url, true);
    });

    let hoverTimer = null;
    function prefetchTarget(event) {
        const link = event.target.closest && event.target.closest('a[href]');
        if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return null;
        let url;
        try { url = new URL(link.href, location.href); } catch (e) { return null; }
        if (!isAppPage(url) || (url.pathname === location.pathname && url.search === location.search)) return null;
        return url;
    }
    document.addEventListener('mouseover', (event) => {
        const url = prefetchTarget(event);
        clearTimeout(hoverTimer);
        if (url) hoverTimer = setTimeout(() => prefetch(url), 70);
    });
    document.addEventListener('pointerdown', (event) => {
        const url = event.button === 0 && prefetchTarget(event);
        if (url) prefetch(url);
    });
    document.addEventListener('touchstart', (event) => {
        const url = prefetchTarget(event);
        if (url) prefetch(url);
    }, { passive: true });

    window.addEventListener('popstate', () => {
        const key = location.pathname + location.search;
        if (key === currentKey) return;  // only the #hash changed
        const url = new URL(location.href);
        if (isAppPage(url)) navigate(url, false);
        else hardNavigate(url);
    });

    try { history.replaceState({ dnNav: true }, '', location.href); } catch (e) {}
})();
