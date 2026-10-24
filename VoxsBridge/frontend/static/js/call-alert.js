/* VoxsBridge incoming-call alert (WhatsApp-style).
 *
 * While a call is ringing:
 *  - a floating call card appears on top of whatever page the user is on,
 *    with Answer / Decline buttons;
 *  - if the user is in another tab or another app, a desktop notification
 *    pops up (click it to jump back to VoxsBridge);
 *  - the tab title flashes and phones vibrate.
 * dialer.js calls VBCallAlert.show() when ringing starts and hide() when it stops.
 */
(function () {
    'use strict';
    const ICON = '/static/img/voxsbridge-favlogo.png';
    const PERM_KEY = 'voxsbridge-alert-banner-hidden';
    const canNotify = 'Notification' in window;
    let card = null, note = null, flashTimer = null, vibrateTimer = null, baseTitle = document.title;

    function el(tag, cls, html) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (html != null) n.innerHTML = html;
        return n;
    }
    const PHONE = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.6a2 2 0 0 1-.5 2.1L8 9.6a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.8.3 1.7.5 2.6.6a2 2 0 0 1 1.7 2z"/></svg>';
    const HANG = '<svg width="24" height="24" viewBox="0 0 24 24" fill="#fff"><path d="M12 9c-1.7 0-3.2.25-4.6.72v2.7c0 .4-.24.76-.6.92-.9.4-1.7.95-2.4 1.6-.2.18-.46.28-.74.28-.27 0-.53-.11-.71-.29L.54 12.5a1 1 0 0 1 0-1.41C3.3 8.4 7.45 7 12 7s8.7 1.4 11.46 4.09a1 1 0 0 1 0 1.41l-2.41 2.43c-.18.18-.44.29-.71.29-.28 0-.54-.1-.74-.28-.7-.65-1.5-1.2-2.4-1.6a1 1 0 0 1-.6-.92V9.72A15.2 15.2 0 0 0 12 9z"/></svg>';

    function buildCard() {
        card = el('div', 'vb-callcard');
        card.setAttribute('role', 'alertdialog');
        card.setAttribute('aria-live', 'assertive');
        card.setAttribute('aria-label', 'Incoming call');
        card.hidden = true;
        card.innerHTML =
            '<div class="vb-callcard-pulse" aria-hidden="true"><span>' + PHONE + '</span></div>' +
            '<div class="vb-callcard-info"><small>Incoming call · VoxsBridge</small><strong id="vbCallcardNum">Unknown caller</strong></div>' +
            '<div class="vb-callcard-actions">' +
            '<button type="button" class="vb-cc-btn vb-cc-decline" title="Decline" aria-label="Decline">' + HANG + '</button>' +
            '<button type="button" class="vb-cc-btn vb-cc-answer" title="Answer" aria-label="Answer">' + PHONE + '</button>' +
            '</div>';
        card.querySelector('.vb-cc-answer').addEventListener('click', () => {
            hide();
            try { window.dispatchEvent(new CustomEvent('voxsbridge:show-dialer')); } catch (e) {}
            if (typeof window.answerIncoming === 'function') window.answerIncoming();
        });
        card.querySelector('.vb-cc-decline').addEventListener('click', () => {
            hide();
            if (typeof window.declineIncoming === 'function') window.declineIncoming();
        });
        document.body.appendChild(card);
    }

    function systemNotify(number) {
        if (!canNotify || Notification.permission !== 'granted') return;
        // Only needed when the user cannot already see the page.
        if (!document.hidden && document.hasFocus()) return;
        try {
            note = new Notification('📞 Incoming call', {
                body: number + '\nClick to open VoxsBridge and answer',
                icon: ICON, badge: ICON, tag: 'voxsbridge-incoming',
                renotify: true, requireInteraction: true, silent: false
            });
            note.onclick = () => {
                try { window.focus(); } catch (e) {}
                try { window.dispatchEvent(new CustomEvent('voxsbridge:show-dialer')); } catch (e) {}
                if (note) note.close();
            };
        } catch (e) {
            // Some mobile browsers only allow notifications from a service worker.
        }
    }

    function flashTitle(number) {
        stopFlash();
        baseTitle = document.title.replace(/^📞 .*? · /, '');
        let on = false;
        flashTimer = setInterval(() => {
            on = !on;
            document.title = on ? '📞 ' + number + ' · Incoming call' : baseTitle;
        }, 900);
    }
    function stopFlash() {
        if (flashTimer) clearInterval(flashTimer);
        flashTimer = null;
        if (/^📞 /.test(document.title)) document.title = baseTitle;
    }

    function show(number) {
        number = number || 'Unknown caller';
        if (!card) buildCard();
        card.querySelector('#vbCallcardNum').textContent = number;
        card.hidden = false;
        requestAnimationFrame(() => card.classList.add('open'));
        systemNotify(number);
        flashTitle(number);
        if (navigator.vibrate) {
            navigator.vibrate([400, 200, 400]);
            vibrateTimer = setInterval(() => navigator.vibrate([400, 200, 400]), 1800);
        }
    }

    function hide() {
        if (card) { card.classList.remove('open'); card.hidden = true; }
        if (note) { try { note.close(); } catch (e) {} note = null; }
        stopFlash();
        if (vibrateTimer) clearInterval(vibrateTimer);
        vibrateTimer = null;
        if (navigator.vibrate) navigator.vibrate(0);
    }

    // One-time banner asking the user to allow desktop call alerts.
    function permissionBanner() {
        if (!canNotify || Notification.permission !== 'default') return;
        try { if (localStorage.getItem(PERM_KEY) === '1') return; } catch (e) {}
        const bar = el('div', 'vb-alertbar');
        bar.innerHTML =
            '<span class="vb-alertbar-ico" aria-hidden="true">🔔</span>' +
            '<span class="vb-alertbar-text"><b>Turn on call alerts</b><small>See incoming calls even when you are in another tab or app.</small></span>' +
            '<button type="button" class="vb-alertbar-on">Allow</button>' +
            '<button type="button" class="vb-alertbar-x" aria-label="Not now">×</button>';
        const close = (remember) => {
            bar.remove();
            if (remember) { try { localStorage.setItem(PERM_KEY, '1'); } catch (e) {} }
        };
        bar.querySelector('.vb-alertbar-on').addEventListener('click', () => {
            Notification.requestPermission().then(() => close(false)).catch(() => close(false));
        });
        bar.querySelector('.vb-alertbar-x').addEventListener('click', () => close(true));
        document.body.appendChild(bar);
    }

    window.VBCallAlert = { show, hide };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', permissionBanner);
    else permissionBanner();
})();
