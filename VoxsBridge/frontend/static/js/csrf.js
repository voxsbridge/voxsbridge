// CSRF protection: every same-origin POST/PUT/PATCH/DELETE carries the token
// from the dn_csrf cookie (header for fetch, hidden field for HTML forms).
(function () {
    function token() {
        const match = document.cookie.match(/(?:^|;\s*)dn_csrf=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }
    function sameOrigin(url) {
        try { return new URL(url, location.href).origin === location.origin; }
        catch (err) { return false; }
    }
    const SAFE = ['GET', 'HEAD', 'OPTIONS', 'TRACE'];
    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        init = init || {};
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
        if (!SAFE.includes(method) && sameOrigin(url)) {
            const headers = new Headers(init.headers || (input && input.headers) || undefined);
            if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token());
            init = Object.assign({}, init, { headers: headers, credentials: init.credentials || 'same-origin' });
        }
        return originalFetch(input, init);
    };
    function stamp(form) {
        if (!form || !form.getAttribute) return;
        // Read attributes, not form.method / form.action: an input named "method" or
        // "action" (e.g. Payment Method) replaces those properties on the form element.
        if (String(form.getAttribute('method') || '').toLowerCase() !== 'post') return;
        if (!sameOrigin(form.getAttribute('action') || location.href)) return;
        let field = form.querySelector('input[name="csrf_token"]');
        if (!field) {
            field = document.createElement('input');
            field.type = 'hidden';
            field.name = 'csrf_token';
            form.appendChild(field);
        }
        field.value = token();
    }
    document.addEventListener('submit', (event) => stamp(event.target), true);
    const nativeSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () { stamp(this); return nativeSubmit.call(this); };
    function stampAll() { document.querySelectorAll('form').forEach(stamp); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', stampAll);
    else stampAll();
})();
