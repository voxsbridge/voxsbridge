/* Phone / tablet layout: bottom tab bar, slide-up dialer and "More" sheet. */
(function () {
    'use strict';
    const body = document.body;
    const mq = window.matchMedia('(max-width: 1100px)');
    const dialBtn = document.getElementById('mobDial');
    const dialLabel = document.getElementById('mobDialLabel');
    const railClose = document.getElementById('railClose');
    const moreBtn = document.getElementById('mobMore');
    const sheet = document.getElementById('mobMoreSheet');
    const backdrop = document.getElementById('mobMoreBackdrop');
    const grid = document.getElementById('mobMoreGrid');
    const inCall = document.getElementById('inCallView');
    const incoming = document.getElementById('incomingView');
    if (!sheet || !moreBtn) return;

    function setRail(open) {
        body.classList.toggle('rail-open', open);
        if (dialBtn) dialBtn.setAttribute('aria-expanded', String(open));
        if (open) closeMore();
    }
    dialBtn && dialBtn.addEventListener('click', () => setRail(!body.classList.contains('rail-open')));
    railClose && railClose.addEventListener('click', () => setRail(false));
    window.addEventListener('voxsbridge:show-dialer', () => { if (mq.matches) setRail(true); });

    // Follow the call: open the dialer for incoming/new calls, show "On call" on the tab.
    let wasCalling = false;
    function syncCall() {
        const calling = !!(inCall && !inCall.hidden && !inCall.classList.contains('tour-demo'));
        const ringing = !!(incoming && !incoming.hidden);
        body.classList.toggle('dn-in-call', calling);
        body.classList.toggle('dn-ringing', ringing);
        if (dialLabel) dialLabel.textContent = ringing ? 'Incoming' : calling ? 'On call' : 'Dial';
        if (mq.matches && (ringing || (calling && !wasCalling))) setRail(true);
        wasCalling = calling;
    }
    [inCall, incoming].forEach((el) => el && new MutationObserver(syncCall)
        .observe(el, { attributes: true, attributeFilter: ['hidden'] }));
    syncCall();

    // "More" sheet lists every sidebar page that is not already a tab.
    const TAB_PATHS = document.body.classList.contains('is-admin')
        ? ['/dashboard', '/customers', '/companies', '/invoices']
        : ['/dashboard', '/call-logs', '/contacts', '/clients'];
    function buildMore() {
        grid.innerHTML = '';
        document.querySelectorAll('.shell-nav nav a[href], .shell-nav .nav-settings').forEach((a) => {
            const path = new URL(a.href, location.href).pathname;
            if (TAB_PATHS.includes(path)) return;
            const item = a.cloneNode(true);
            item.className = 'mob-sheet-item' + (a.classList.contains('active') ? ' on' : '');
            item.removeAttribute('onclick');
            grid.appendChild(item);
        });
    }
    function openMore() {
        buildMore();
        sheet.hidden = false;
        backdrop.hidden = false;
        moreBtn.setAttribute('aria-expanded', 'true');
        requestAnimationFrame(() => sheet.classList.add('open'));
    }
    function closeMore() {
        sheet.classList.remove('open');
        sheet.hidden = true;
        backdrop.hidden = true;
        moreBtn.setAttribute('aria-expanded', 'false');
    }
    moreBtn.addEventListener('click', () => (sheet.hidden ? openMore() : closeMore()));
    backdrop.addEventListener('click', closeMore);
    sheet.addEventListener('click', (e) => { if (e.target.closest('a')) { closeMore(); setRail(false); } });
    document.getElementById('mobTabbar').addEventListener('click', (e) => {
        if (e.target.closest('a.mob-tab')) { closeMore(); setRail(false); }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (!sheet.hidden) closeMore();
        else if (body.classList.contains('rail-open')) setRail(false);
    });

    function markTab(forPath) {
        const path = typeof forPath === 'string' ? forPath : location.pathname;
        let matched = false;
        document.querySelectorAll('.mob-tab[data-tab]').forEach((t) => {
            const on = path === t.dataset.tab || path.startsWith(t.dataset.tab + '/');
            t.classList.toggle('on', on);
            if (on) matched = true;
        });
        moreBtn.classList.toggle('on', !matched);
    }
    window.addEventListener('voxsbridge:page-load', markTab);
    window.addEventListener('popstate', () => setTimeout(markTab, 0));
    document.addEventListener('click', (e) => {
        const a = e.target.closest && e.target.closest('a[href]');
        if (!a || a.target === '_blank') return;
        try {
            const url = new URL(a.href, location.href);
            if (url.origin === location.origin && url.pathname !== '/dialer') markTab(url.pathname);
        } catch (err) {}
    });
    markTab();
    const onSize = () => { if (!mq.matches) { setRail(false); closeMore(); } };
    if (mq.addEventListener) mq.addEventListener('change', onSize);
    else if (mq.addListener) mq.addListener(onSize);   // older Safari
})();
