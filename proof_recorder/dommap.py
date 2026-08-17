from __future__ import annotations

import math
import random
import time


from .capture import _cursor_click_pulse, _ghost_move, _human_move_to, _scroll_to_element_human

_DOM_JS = r"""(args) => {
    const W = window.innerWidth, H = window.innerHeight;
    const REG = (window.__pr_targets = window.__pr_targets || {});
    const CLICKABLE = 'a,button,[role="button"],[role="link"],[onclick],input[type="submit"],input[type="button"]';

    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const textOf = (el) => {
        let t = el.innerText || el.textContent || '';
        if (!t.trim()) t = el.value || el.getAttribute('aria-label') || el.getAttribute('title') ||
                           el.getAttribute('placeholder') || el.getAttribute('name') || '';
        return norm(t).slice(0, 80);
    };
    const attrBlob = (el) => norm([el.id, el.className && el.className.baseVal !== undefined
        ? el.className.baseVal : el.className, el.getAttribute('aria-label'),
        el.getAttribute('data-testid'), el.getAttribute('title')].join(' '));

    function rectOf(el) {
        let r;
        try { r = el.getBoundingClientRect(); } catch (e) { return null; }
        if (!r || r.width < 4 || r.height < 4) return null;
        let s;
        try { s = getComputedStyle(el); } catch (e) { return null; }
        if (s.visibility === 'hidden' || s.display === 'none') return null;
        if (parseFloat(s.opacity || '1') < 0.05) return null;
        return r;
    }
    const inViewport = (r) => r.bottom > 0 && r.right > 0 && r.top < H && r.left < W;

    function visFrac(r) {
        const iw = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0));
        const ih = Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
        const area = r.width * r.height;
        return area > 0 ? (iw * ih) / area : 0;
    }

    function clipState(r) {
        if (visFrac(r) >= 0.5) return 'ok';
        const fitsHoriz = r.left >= -2 && r.right <= W + 2;
        if (fitsHoriz && (r.bottom <= 0 || r.top >= H)) return 'scrollable';
        return 'clipped';
    }

    function deepElementFromPoint(x, y) {
        let el;
        try { el = document.elementFromPoint(x, y); } catch (e) { return null; }
        for (let i = 0; i < 10 && el && el.shadowRoot; i++) {
            let inner;
            try { inner = el.shadowRoot.elementFromPoint(x, y); } catch (e) { break; }
            if (!inner || inner === el) break;
            el = inner;
        }
        return el;
    }

    function pointInfo(el) {
        const r = rectOf(el);
        if (!r) return {pt: null, why: 'нет размера', blocker: ''};
        if (visFrac(r) < 0.5) return {pt: null, why: 'вне экрана', blocker: ''};
        const L = Math.max(r.left, 0), T = Math.max(r.top, 0);
        const R = Math.min(r.right, W), B = Math.min(r.bottom, H);
        if (R - L < 2 || B - T < 2) return {pt: null, why: 'вне экрана', blocker: ''};
        const cx = (L + R) / 2, cy = (T + B) / 2;
        const tries = [[cx, cy], [cx, T + (B - T) * 0.25], [cx, B - (B - T) * 0.25],
                       [L + (R - L) * 0.25, cy], [R - (R - L) * 0.25, cy]];
        for (const [px0, py0] of tries) {
            const px = Math.round(Math.min(W - 1, Math.max(0, px0)));
            const py = Math.round(Math.min(H - 1, Math.max(0, py0)));
            const top = deepElementFromPoint(px, py);
            if (top && (top === el || containsDeep(el, top) || containsDeep(top, el)))
                return {pt: {x: px, y: py}, why: '', blocker: ''};
        }
        const px = Math.round(Math.min(W - 1, Math.max(0, cx)));
        const py = Math.round(Math.min(H - 1, Math.max(0, cy)));
        const top = deepElementFromPoint(px, py);
        let blocker = 'неизвестно чем';
        if (top) {
            const cls = typeof top.className === 'string' ? top.className.trim().slice(0, 40) : '';
            const tr = rectOf(top);
            const full = tr && tr.width * tr.height > W * H * 0.8 ? ', во весь экран' : '';
            blocker = '<' + top.tagName.toLowerCase() + '>' +
                      (cls ? ' .' + cls : '') + ' ' + textOf(top).slice(0, 30) + full;
        }
        return {pt: {x: px, y: py}, why: 'перекрыт', blocker: blocker};
    }

    function pointOf(el) {
        const i = pointInfo(el);
        return i.why ? null : i.pt;
    }

    function describe(el, extra) {
        const r = rectOf(el);
        const info = pointInfo(el);
        const p = info.pt;
        const out = Object.assign({
            found: true,
            offscreen: !p,
            covered: info.why === 'перекрыт',
            blocker: info.blocker,
            x: p ? p.x : null, y: p ? p.y : null,
            tag: el.tagName ? el.tagName.toLowerCase() : '?',
            text: textOf(el).slice(0, 50),
            top: r ? Math.round(r.top) : null,
        }, extra || {});
        return out;
    }

    function allElements() {
        const flat = document.querySelectorAll('*');
        const out = Array.prototype.slice.call(flat);
        if (out.length > 20000) return out;  // очень тяжёлая страница — обходимся без shadow
        const walk = (root) => {
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) { out.push.apply(out, el.shadowRoot.querySelectorAll('*')); walk(el.shadowRoot); }
            }
        };
        try { walk(document); } catch (e) {}
        return out;
    }

    const hostOf = (u) => { try { return new URL(u, location.href).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
    const selfHost = location.hostname.replace(/^www\./, '');
    const isAnchorOnly = (el) => {
        const href = el.getAttribute && el.getAttribute('href');
        return !!href && href.trim().startsWith('#');
    };
    function isExternal(el) {
        if (!selfHost) return false;
        const href = el.getAttribute && el.getAttribute('href');
        if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) return false;
        const h = hostOf(href);
        if (!h || h === selfHost) return false;
        return !(h.endsWith('.' + selfHost) || selfHost.endsWith('.' + h));
    }

    function innermost(list) {
        return list.filter(a => !list.some(b => b !== a && a.el.contains(b.el)));
    }

    const CTA_STRONG = ['перейти', 'официальный сайт', 'на сайт', 'go to', 'official site'];
    const CTA_WORDS = ['играть', 'играй', 'вход', 'войти', 'регистр', 'бонус',
                       'начать', 'получить', 'забрать', 'сайт', 'зеркал',
                       'play', 'join', 'sign up', 'enter', 'claim', 'bonus'];
    const CAROUSEL_SEL = '[class*="slider"],[class*="slide"],[class*="carousel"],[class*="swiper"]';
    const CTA_BAD = ['политик', 'cookie', 'куки', 'соглаш', 'условия', 'конфиденц', '18+',
                     'telegram', 'поддержк', 'support', 'скачать', 'download', 'vpn',
                     'ответственн', 'лиценз',
                     'помощь', 'помощ', 'help', 'faq', 'вопрос', 'контакт', 'contact',
                     'правил', 'о нас', 'about', 'жалоб', 'блог', 'blog', 'новост',
                     'партн', 'affiliate', 'реклам', 'карта сайта', 'sitemap'];

    const squash = (s) => (s || '').replace(/[^0-9a-zа-яё]/gi, '').toLowerCase();
    const BRAND_KEY = squash(selfHost.split('.')[0]);
    const hasBrandWord = (t) => {
        const b = squash(t);
        if (BRAND_KEY.length < 4 || b.length < 4) return false;
        return BRAND_KEY.indexOf(b) >= 0 || b.indexOf(BRAND_KEY) >= 0;
    };
    const SLOTS_WORDS = ['слоты', 'слот', 'slots', 'игровые автоматы', 'автоматы', 'games', 'игры', 'казино', 'casino'];
    const REG_WORDS = ['регистрация', 'зарегистрироваться', 'регистрируйся', 'создать аккаунт',
                       'register', 'sign up', 'signup', 'join now'];
    const CLOSE_WORDS = ['close', 'закрыть', 'dismiss', 'модальн', 'modal-close'];

    const INPUT_TYPES = ['', 'text', 'tel', 'email', 'password', 'number', 'search', 'date'];
    const MODAL_HINT = /modal|popup|pop-up|dialog|overlay|lightbox|fancybox|drawer/;

    function parentOf(n) {
        if (!n) return null;
        if (n.parentElement) return n.parentElement;
        const p = n.parentNode;
        return (p && p.host) ? p.host : null;
    }
    function containsDeep(a, b) {
        for (let n = b; n; n = parentOf(n)) if (n === a) return true;
        return false;
    }

    function isFieldLike(el) {
        const tag = el.tagName;
        if (tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') return false;
        if (tag === 'INPUT' &&
            INPUT_TYPES.indexOf((el.getAttribute('type') || '').toLowerCase()) === -1) return false;
        return !(el.disabled || el.readOnly);
    }

    function overlayAncestor(el) {
        let strong = null, loose = null;
        for (let n = parentOf(el); n && n !== document.body && n !== document.documentElement;
             n = parentOf(n)) {
            const role = n.getAttribute ? (n.getAttribute('role') || '') : '';
            if (!strong) {
                if (n.tagName === 'DIALOG' && n.open) strong = {el: n, why: '<dialog open>', strong: true, z: 0};
                else if (role === 'dialog' || role === 'alertdialog')
                    strong = {el: n, why: 'role=' + role, strong: true, z: 0};
                else if (n.getAttribute && n.getAttribute('aria-modal') === 'true')
                    strong = {el: n, why: 'aria-modal', strong: true, z: 0};
            }
            let s;
            try { s = getComputedStyle(n); } catch (e) { continue; }
            const z = parseInt(s.zIndex) || 0;
            if (s.position === 'fixed')
                loose = {el: n, why: 'position:fixed', strong: false, z: z};
            else if (s.position === 'absolute' && z >= 100)
                loose = {el: n, why: 'absolute, z-index ' + z, strong: false, z: z};
            else if (MODAL_HINT.test(attrBlob(n)))
                loose = {el: n, why: 'класс/id всплывающего окна', strong: false, z: z};
        }
        return strong || loose;
    }

    function boxOf(els, overlay) {
        let a = els[0];
        for (const e of els) { while (a && !containsDeep(a, e)) a = parentOf(a); }
        if (!a) return overlay;
        for (let n = parentOf(a); n && n.nodeType === 1; n = parentOf(n)) {
            const r = rectOf(n);
            if (!r || r.width * r.height > W * H * 0.9) break;
            a = n;
            if (n === overlay) break;
        }
        return a;
    }

    function inlineGroupKey(el) {
        const f = el.closest ? el.closest('form') : null;
        if (f) return f;
        const areaLimit = (W < 700 ? 0.97 : 0.7);
        for (let n = parentOf(el); n && n !== document.body && n !== document.documentElement;
             n = parentOf(n)) {
            const r = rectOf(n);
            if (!r) continue;
            if (r.width * r.height > W * H * areaLimit) break;
            let cnt = 0;
            try {
                for (const c of n.querySelectorAll('input,select,textarea'))
                    if (isFieldLike(c)) cnt++;
            } catch (e) {}
            if (cnt >= 2) return n;
        }
        return null;
    }

    const REG_FORM_WORDS = ['регистрац', 'зарегистр', 'создать аккаунт', 'создать счёт',
                            'создать счет', 'register', 'sign up', 'signup', 'join now'];

    function looksLikeRegistration(container) {
        const parts = [];
        let n = container;
        for (let i = 0; i < 3 && n && n !== document.body && n !== document.documentElement;
             i++, n = parentOf(n)) {
            const r = rectOf(n);
            if (r && r.width * r.height > W * H * 0.85) break;
            try { parts.push(((n.innerText || n.textContent || '') + '').slice(0, 600)); } catch (e) {}
        }
        try {
            for (const f of container.querySelectorAll('input,button,select'))
                parts.push([f.value, f.placeholder, f.name, f.getAttribute('aria-label')].join(' '));
        } catch (e) {}
        parts.push(document.title || '');
        try { parts.push(decodeURIComponent(location.href)); } catch (e) { parts.push(location.href); }
        const blob = norm(parts.join(' '));
        return REG_FORM_WORDS.some(w => blob.includes(w));
    }

    function findModal() {
        const groups = new Map();
        const inline = new Map();
        const NARROW = W < 700;
        for (const el of allElements()) {
            if (!isFieldLike(el)) continue;
            const r = rectOf(el);
            if (!r) continue;
            const cs = clipState(r);
            let live = false;
            if (cs === 'ok') {
                live = !!pointOf(el);
                if (!live) continue;
            } else if (NARROW && cs === 'scrollable') {
            } else continue;
            const ov = overlayAncestor(el);
            if (!ov) {
                const key = inlineGroupKey(el);
                if (!key) continue;
                let ig = inline.get(key);
                if (!ig) {
                    ig = {ov: {el: key, why: 'форма в самой странице', strong: false, z: 0},
                          fields: []};
                    inline.set(key, ig);
                }
                ig.fields.push({el: el, top: r.top, left: r.left, live: live});
                continue;
            }
            let g = groups.get(ov.el);
            if (!g) { g = {ov: ov, fields: []}; groups.set(ov.el, g); }
            g.fields.push({el: el, top: r.top, left: r.left, live: live});
        }
        function pickBest(pool, needWords) {
            let best = null;
            for (const g of pool.values()) {
                if (needWords && !looksLikeRegistration(g.ov.el)) continue;
                if (!g.fields.some(f => f.live)) continue;
                if (g.fields.length < 2 && !g.ov.strong &&
                    !(NARROW && looksLikeRegistration(g.ov.el))) continue;
                const ro = rectOf(g.ov.el);
                if (!g.ov.strong && ro && ro.height < H * 0.25 && ro.width > W * 0.9 &&
                    (ro.top <= 2 || ro.bottom >= H - 2)) continue;
                const score = g.fields.length * 20 + (g.ov.strong ? 200 : 0) +
                              Math.min(g.ov.z, 1000) / 10;
                if (!best || score > best.score) best = {g: g, score: score};
            }
            return best;
        }
        const best = pickBest(groups, false) || pickBest(inline, true);
        if (!best) return null;
        const fields = best.g.fields.slice().sort(
            (a, b) => (Math.round(a.top / 24) - Math.round(b.top / 24)) || (a.left - b.left));
        const els = fields.map(f => f.el);
        const box = boxOf(els, best.g.ov.el);
        return {el: box, overlay: best.g.ov.el, inputs: els, why: best.g.ov.why,
                rect: rectOf(box) || best.g.ov.el.getBoundingClientRect()};
    }

    function closeIn(root, r0) {
        let best = null, bestScore = -1e9;
        let list;
        try { list = root.querySelectorAll('button,a,span,i,svg,div,[role="button"]'); } catch (e) { return null; }
        for (const el of list) {
            const r = rectOf(el);
            if (!r || clipState(r) !== 'ok') continue;
            if (r.width > 90 || r.height > 90) continue;   // крестик — мелкий элемент
            const t = textOf(el), blob = attrBlob(el);
            let score = 0;
            if (/^[×✕✖✗✘xX]$/.test(t.trim())) score += 100;
            else if (CLOSE_WORDS.some(w => blob.includes(w))) score += 80;
            else continue;
            score -= Math.hypot(r.left + r.width / 2 - (r0.right - 18),
                                r.top + r.height / 2 - (r0.top + 18)) / 10;
            if (score > bestScore) { bestScore = score; best = el; }
        }
        return best;
    }

    function findClose(modal) {
        return closeIn(modal.el, modal.rect) ||
               (modal.overlay !== modal.el ? closeIn(modal.overlay, modal.rect) : null);
    }

    function inStickyTopBar(el) {
        for (let n = el; n && n !== document.body && n !== document.documentElement;
             n = parentOf(n)) {
            let s;
            try { s = getComputedStyle(n); } catch (e) { continue; }
            if (s.position !== 'fixed' && s.position !== 'sticky') continue;
            const r = rectOf(n);
            if (r && r.top <= 8 && r.width > W * 0.6 && r.height > 0 && r.height < H * 0.35)
                return true;
        }
        return false;
    }

    function scoreCta(el) {
        const r = rectOf(el);
        if (!r || clipState(r) === 'clipped') return null;
        const t = textOf(el);
        if (CTA_BAD.some(w => t.includes(w))) return null;
        const area = r.width * r.height;
        if (area > W * H * 0.5) return null;             // обёртка во весь экран, а не кнопка

        const strong = CTA_STRONG.some(w => t.includes(w));
        const wordy = CTA_WORDS.some(w => t.includes(w));
        const brandy = hasBrandWord(t);
        if (!strong && !wordy && !brandy) return null;

        let score = 0, why = [];
        const sticky = inStickyTopBar(el);
        if (sticky) { score += 40; why.push('в закреплённой верхней панели'); }
        if (isExternal(el)) { score += 45; why.push('внешняя ссылка'); }
        if (el.getAttribute && el.getAttribute('target') === '_blank') { score += 12; why.push('новая вкладка'); }
        if (strong) { score += 55; why.push('прямой призыв перейти'); }
        else if (wordy) { score += 25; why.push('текст призыва'); }
        if (brandy) { score += 30; why.push('на кнопке имя бренда'); }
        if (el.tagName === 'BUTTON' || (el.getAttribute && el.getAttribute('role') === 'button')) score += 8;
        score += Math.min(area / (W * H), 0.08) * 200;   // крупная кнопка вероятнее главная
        if (clipState(r) === 'ok') score += 12;          // видна прямо сейчас целиком
        if (r.top >= 0 && r.top <= H) score += 8;        // на первом экране
        else score -= Math.min(Math.abs(r.top) / H, 4) * 1.5;
        if (el.closest(CAROUSEL_SEL)) { score -= 20; why.push('в карусели, цель нестабильна'); }
        if (el.closest('footer')) score -= 25;
        if (!sticky && (el.closest('nav') || el.closest('header'))) score -= 10;
        if (!t) score -= 10;
        if (area < 3000 || r.height < 22) score -= 25;
        if (isAnchorOnly(el)) score -= 30;   // ссылка внутрь этой же страницы
        return {score: score, why: why.join(', ')};
    }

    const BRAND_BAD = ['вход', 'войти', 'login', 'sign in', 'регистрация', 'register',
                       'sign up', 'выход', 'logout', 'помощь', 'support'];
    const BRAND_WORD = selfHost.split('.')[0].replace(/[0-9]+$/, '');

    function scoreBrand(el) {
        const r = rectOf(el);
        if (!r || clipState(r) === 'clipped') return null;
        const t = textOf(el);
        if (BRAND_BAD.some(w => t.includes(w))) return null;
        if (isAnchorOnly(el)) return null;
        if (r.width * r.height > W * H * 0.25) return null;   // это обёртка, а не логотип
        let score = 0, why = [];
        const href = el.getAttribute && el.getAttribute('href');
        if (href && !/^(javascript:|mailto:|tel:)/i.test(href)) {
            let path = null;
            try { path = new URL(href, location.href); } catch (e) {}
            if (path && (!selfHost || path.hostname.replace(/^www\./, '') === selfHost) &&
                (path.pathname === '/' || path.pathname === '')) {
                score += 50; why.push('ссылка на главную');
            }
        }
        if (el.querySelector && el.querySelector('img,svg,picture')) { score += 25; why.push('картинка-логотип'); }
        if (/logo|brand/.test(attrBlob(el))) { score += 30; why.push('класс логотипа'); }
        if (BRAND_WORD.length >= 3 && t.includes(BRAND_WORD)) { score += 35; why.push('название бренда'); }
        if (el.closest('header,[class*="header"],[class*="navbar"],nav')) { score += 20; why.push('в шапке'); }
        if (r.top <= 140) score += 15;
        if (r.left <= W * 0.35) score += 10;                  // логотип почти всегда слева
        if (clipState(r) === 'ok') score += 10;
        return score > 0 ? {score: score, why: why.join(', ')} : null;
    }

    function bestOf(cands) {
        cands = innermost(cands);
        cands.sort((a, b) => b.score - a.score);
        if (!cands.length) return null;
        const head = cands.slice(0, 5);
        const clear = head.find(c => pointInfo(c.el).why !== 'перекрыт');
        return clear || cands[0];
    }

    switch (args.what) {

    case 'cta': {
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches || !el.matches(CLICKABLE)) continue;
            const s = scoreCta(el);
            if (s && s.score > 25) cands.push({el: el, score: s.score, why: s.why});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.cta = top.el;
        return describe(top.el, {score: Math.round(top.score), why: top.why,
                                 href: top.el.getAttribute ? (top.el.getAttribute('href') || '') : ''});
    }

    case 'slots': {
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches || !el.matches(CLICKABLE + ',li')) continue;
            const t = textOf(el);
            if (!t || t.length > 24) continue;
            const hit = SLOTS_WORDS.find(w => t === w || t.startsWith(w + ' ') || t === w + 'ы');
            if (!hit) continue;
            const r = rectOf(el);
            if (!r || clipState(r) === 'clipped') continue;
            let score = 40 - SLOTS_WORDS.indexOf(hit) * 3;   // "слоты" точнее, чем "казино"
            if (t === hit) score += 20;
            if (clipState(r) === 'ok') score += 15;
            if (el.closest('nav') || el.closest('header') || el.closest('aside') || el.closest('menu')) score += 15;
            const href = el.getAttribute && el.getAttribute('href');
            if (href && /slot|avtomat|игров|games/i.test(href)) score += 15;
            if (el.closest('footer')) score -= 30;
            cands.push({el: el, score: score});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.slots = top.el;
        return describe(top.el, {score: Math.round(top.score)});
    }

    case 'slots_again': {
        const prev = REG.slots;
        const prevRect = prev ? rectOf(prev) : null;
        const pcx = prevRect ? (prevRect.left + prevRect.right) / 2 : null;
        const pcy = prevRect ? (prevRect.top + prevRect.bottom) / 2 : null;
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches) continue;
            const r = rectOf(el);
            if (!r || clipState(r) === 'clipped') continue;
            if (r.right - r.left > 420 || r.bottom - r.top > 220) continue;
            const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
            if (el === prev) continue;
            if (prevRect && Math.abs(cx - pcx) < 12 && Math.abs(cy - pcy) < 12) continue;
            const t = textOf(el);
            if (!t || t.length > 24) continue;
            const hit = SLOTS_WORDS.find(w => t === w || t.startsWith(w + ' ') || t === w + 'ы');
            if (!hit) continue;
            let sameTextChild = false;
            for (const c of el.children) {
                if (textOf(c) === t) { sameTextChild = true; break; }
            }
            if (sameTextChild) continue;
            let score = 40 - SLOTS_WORDS.indexOf(hit) * 3;
            if (t === hit) score += 20;
            if (el.matches(CLICKABLE)) score += 10;
            if (prevRect && r.left < prevRect.right && r.right > prevRect.left) {
                const gap = (r.top > prevRect.bottom) ? r.top - prevRect.bottom
                          : (prevRect.top > r.bottom) ? prevRect.top - r.bottom : 0;
                if (gap <= 600) score += 30 - Math.round(gap / 40);
            }
            const href = el.getAttribute && el.getAttribute('href');
            if (href && /slot|avtomat|игров|games|casino/i.test(href)) score += 20;
            if (el.closest('footer')) score -= 30;
            cands.push({el: el, score: score});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.slots_again = top.el;
        return describe(top.el, {score: Math.round(top.score)});
    }

    case 'brand': {
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches || !el.matches(CLICKABLE)) continue;
            const s = scoreBrand(el);
            if (s && s.score > 45) cands.push({el: el, score: s.score, why: s.why});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.brand = top.el;
        return describe(top.el, {score: Math.round(top.score), why: top.why});
    }

    case 'register': {
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches || !el.matches(CLICKABLE)) continue;
            const t = textOf(el);
            if (!t || t.length > 30) continue;
            if (!REG_WORDS.some(w => t.includes(w))) continue;
            const r = rectOf(el);
            if (!r || clipState(r) === 'clipped') continue;
            let s;
            try { s = getComputedStyle(el); } catch (e) { s = {}; }
            const sticky = !!el.closest('header,[class*="header"],[class*="navbar"]');
            const nearTop = r.top <= 160;
            if (!sticky && !nearTop) continue;
            let score = 40;
            if (nearTop) score += 20;
            if (sticky) score += 20;
            if (clipState(r) === 'ok') score += 15;
            if (el.tagName === 'BUTTON') score += 10;
            if (r.width < 260 && r.height < 80) score += 10;   // компактная кнопка, а не баннер
            score += (r.left / W) * 10;                        // в шапке она обычно справа
            cands.push({el: el, score: score});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.register = top.el;
        return describe(top.el, {score: Math.round(top.score)});
    }

    case 'leave_confirm': {
        const ASK = ['покинуть регистрацию', 'покинуть страницу', 'выйти из регистрации',
                     'хотите покинуть', 'хотите выйти', 'прервать регистрацию',
                     'leave registration', 'leave this page'];
        const GO = ['все равно выйти', 'всё равно выйти', 'выйти все равно',
                    'выйти всё равно', 'все равно уйти', 'всё равно уйти',
                    'да, выйти', 'покинуть', 'выйти', 'leave anyway', 'exit anyway'];
        const STAY = ['продолжить регистрацию', 'продолжить', 'остаться', 'вернуться',
                      'отмена', 'закрыть', 'continue', 'stay', 'cancel'];

        let box = null, boxArea = Infinity;
        for (const el of allElements()) {
            const r = rectOf(el);
            if (!r || r.width < 180 || r.height < 80) continue;
            if (!inViewport(r)) continue;
            const t = norm(el.innerText || '');
            if (!t || t.length > 400) continue;
            if (!ASK.some(w => t.includes(w))) continue;
            const area = r.width * r.height;
            if (area < boxArea) { box = el; boxArea = area; }
        }
        if (!box) return null;

        let btn = null;
        const inside = box.querySelectorAll(
            CLICKABLE + ',[class*="button"],[class*="Button"],[class*="btn"]');
        for (const el of inside) {
            const t = textOf(el);
            if (!t || t.length > 40) continue;
            if (STAY.some(w => t === w || t.indexOf(w) === 0)) continue;   // остаться — мимо
            if (!GO.some(w => t.indexOf(w) >= 0)) continue;
            if (!rectOf(el)) continue;
            btn = el;
            break;
        }
        if (!btn) return null;
        REG.leave_confirm = btn;
        return describe(btn, {why: 'кнопка выхода в окне подтверждения'});
    }

    case 'form': {
        const modal = findModal();
        if (!modal) return {open: false, fields: [], close: null};
        REG.fields = modal.inputs.slice(0, 5);
        const closeEl = findClose(modal);
        REG.close = closeEl || null;
        return {
            open: true,
            why: modal.why,
            fields: REG.fields.map(el => describe(el)),
            close: closeEl ? describe(closeEl) : null,
            rect: {w: Math.round(modal.rect.width), h: Math.round(modal.rect.height)},
        };
    }

    case 'search_box': {
        const cands = [];
        for (const el of allElements()) {
            if (!el.matches) continue;
            if (!el.matches('input[type="text"],input[type="search"],input:not([type]),textarea')) continue;
            const r = rectOf(el);
            if (!r || clipState(r) !== 'ok') continue;
            if (r.width < 120 || r.height < 16) continue;   // не строка поиска, а мелкое поле фильтра
            let score = 20;
            const blob = attrBlob(el) + ' ' + norm(el.getAttribute('name') || '') + ' ' +
                         norm(el.getAttribute('placeholder') || '');
            if (/(^|\s)text($|\s)/.test(norm(el.getAttribute('name') || ''))) score += 50;
            if (blob.indexOf('search') >= 0 || blob.indexOf('запрос') >= 0 ||
                blob.indexOf('найти') >= 0 || blob.indexOf('поиск') >= 0) score += 30;
            if (el.closest('form')) score += 15;
            score += (r.width / W) * 20;          // строка поиска — самое широкое поле на странице
            score -= (r.top / Math.max(1, H)) * 10;
            cands.push({el: el, score: score});
        }
        const top = bestOf(cands);
        if (!top) return null;
        REG.search_box = top.el;
        return describe(top.el, {score: Math.round(top.score)});
    }

    case 'serp': {
        const want = norm(args.domain || '');
        if (!want) return null;
        const hostHit = (h) => {
            h = (h || '').replace(/^www\./, '');
            return !!h && (h === want || h.endsWith('.' + want) || want.endsWith('.' + h));
        };
        const wantRe = new RegExp('(^|[^a-z0-9.-])' +
            want.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '([^a-z0-9-]|$)');

        const cands = [];
        for (const el of allElements()) {
            if (!el.matches || !el.matches('a[href]')) continue;
            const href = el.getAttribute('href') || '';
            if (/^(#|javascript:|mailto:|tel:)/i.test(href)) continue;
            const h = hostOf(href);
            let why = '';
            if (hostHit(h)) {
                why = 'прямая ссылка на домен';
            } else if (h === selfHost || h.endsWith('.yandex.ru') || h === 'yandex.ru') {
                const block = el.closest('li,.serp-item,.organic,[data-fast-name],[class*="OrganicTitle"]');
                if (!block) continue;
                if (!wantRe.test(norm(block.innerText || ''))) continue;
                why = 'домен в подписи результата';
            } else {
                continue;
            }
            const r = rectOf(el);
            if (!r || clipState(r) === 'clipped') continue;
            if (r.width < 40 || r.height < 10) continue;   // фавиконка/иконка меню, а не сам результат
            const t = textOf(el);
            const block = el.closest('li,.serp-item,.organic') || el;
            const blockText = norm(block.innerText || '');

            let score = 40;
            if (why === 'прямая ссылка на домен') score += 15;
            if (el.closest('h1,h2,h3')) score += 35;
            if (t.length >= 15) score += 20;
            if (el.closest('.organic,[class*="OrganicTitle"]')) score += 25;
            if (blockText.indexOf('реклама') >= 0) score -= 25;
            if (el.closest('footer,[class*="footer"],[class*="Footer"]')) score -= 60;
            score -= (r.top / Math.max(1, H)) * 15;        // чем выше в выдаче, тем лучше
            cands.push({el: el, score: score, why: why});
        }
        const top = bestOf(innermost(cands));
        if (!top) return null;
        REG.serp = top.el;
        return describe(top.el, {score: Math.round(top.score), why: top.why,
                                 href: top.el.getAttribute('href') || ''});
    }


    case 'point': {
        let el = REG[args.key];
        if (args.key === 'field' && Array.isArray(REG.fields)) el = REG.fields[args.index || 0];
        if (!el || !el.isConnected) return null;
        return describe(el);
    }

    case 'scroll_to': {
        let el = REG[args.key];
        if (args.key === 'field' && Array.isArray(REG.fields)) el = REG.fields[args.index || 0];
        if (!el || !el.isConnected) return false;
        try { el.scrollIntoView({block: 'center', inline: 'center', behavior: 'smooth'}); } catch (e) {
            try { el.scrollIntoView(); } catch (e2) { return false; }
        }
        return true;
    }

    }
    return null;
}"""


class _FormCtx:

    def __init__(self, page, frame=None, dx: float = 0, dy: float = 0, label: str = "разметка страницы"):
        self.page = page
        self.frame = frame if frame is not None else page.main_frame
        self.dx, self.dy = dx, dy
        self.label = label

    @property
    def is_main(self) -> bool:
        return self.dx == 0 and self.dy == 0

    def to_page(self, x, y):
        return int(round(x + self.dx)), int(round(y + self.dy))


def _form_frames(page, log_fn):
    out = [_FormCtx(page)]
    try:
        frames = list(page.frames)
    except Exception:
        return out
    for fr in frames:
        if fr is page.main_frame:
            continue
        try:
            el = fr.frame_element()
            box = el.bounding_box()
        except Exception:
            continue   # фрейм уже отсоединился или недоступен
        if not box or box["width"] < 120 or box["height"] < 120:
            continue
        out.append(_FormCtx(page, frame=fr, dx=box["x"], dy=box["y"],
                            label=f"iframe {int(box['width'])}x{int(box['height'])} "
                                  f"в ({int(box['x'])}, {int(box['y'])})"))
    return out


def _dom_query(page, log_fn, what: str, ctx: _FormCtx = None, **kwargs):
    args = {"what": what}
    args.update(kwargs)
    target = ctx.frame if ctx is not None else page
    try:
        return target.evaluate(_DOM_JS, args)
    except Exception as e:
        log_fn(f"[!] Поиск по разметке ({what}) не удался: {e}")
        return None


def _dom_point(page, log_fn, key: str, index: int = 0, what: str = "", ctx: _FormCtx = None):
    res = _dom_query(page, log_fn, "point", key=key, index=index, ctx=ctx)
    if res and res.get("offscreen"):
        if ctx is None:
            _scroll_to_element_human(page, key, log_fn, index=index)
        else:
            _dom_query(page, log_fn, "scroll_to", key=key, index=index, ctx=ctx)
            page.wait_for_timeout(random.randint(350, 600))
        res = _dom_query(page, log_fn, "point", key=key, index=index, ctx=ctx)
    if not res or res.get("x") is None:
        if what:
            log_fn(f"[!] Элемент '{what}' больше не доступен для клика в разметке")
        return None
    if res.get("covered") and what:
        log_fn(f"[i] '{what}' перекрыт сверху ({res.get('blocker')}) — "
               f"кликаю по нему, нажатие примет верхний элемент")
    if ctx is not None:
        return ctx.to_page(res["x"], res["y"])
    return int(res["x"]), int(res["y"])


_REFIND_WHAT = {"cta": "cta", "slots": "slots", "slots_again": "slots_again",
                "register": "register",
                "brand": "brand", "field": "form", "close": "form",
                "serp": "serp", "search_box": "search_box",
                "leave_confirm": "leave_confirm"}


def _dom_point_fresh(page, log_fn, key: str, index: int = 0, what: str = "", ctx: _FormCtx = None,
                     refind_args: dict = None):
    point = _dom_point(page, log_fn, key, index=index, ctx=ctx)
    if point:
        return point
    refind = _REFIND_WHAT.get(key)
    if not refind:
        return None
    log_fn(f"[i] '{what or key}' изменился на странице — ищу заново")
    res = _dom_query(page, log_fn, refind, ctx=ctx, **(refind_args or {})) or {}
    ok = res.get("open") if refind == "form" else res.get("found")
    if not ok:
        log_fn(f"[!] '{what or key}' в разметке больше не находится")
        return None
    return _dom_point(page, log_fn, key, index=index, what=what, ctx=ctx)


def _human_click_dom(page, log_fn, key: str, what: str, index: int = 0,
                     dwell_ms=(700, 1100), ctx: _FormCtx = None, refind_args: dict = None):
    point = _dom_point_fresh(page, log_fn, key, index=index, what=what, ctx=ctx,
                             refind_args=refind_args)
    if not point:
        return None
    _human_move_to(page, point[0], point[1], dwell_ms=dwell_ms, log_fn=log_fn)

    for _ in range(2):
        fresh = _dom_point(page, log_fn, key, index=index, ctx=ctx)
        if not fresh:
            log_fn(f"[!] Цель '{what}' исчезла, пока подводился курсор — кликаю по прежней точке")
            break
        if math.hypot(fresh[0] - point[0], fresh[1] - point[1]) <= 8:
            point = fresh
            break
        log_fn(f"[i] Цель '{what}' сместилась {point} -> {fresh} (страница движется) — довожу курсор")
        _ghost_move(page, point, fresh, log_fn=log_fn)
        point = fresh

    _cursor_click_pulse(page)
    page.mouse.click(point[0], point[1])
    return point


DOM_OFFSCREEN = "offscreen"


def _dom_find(page, log_fn, what: str, label: str, scroll: bool = True, out: dict = None,
              **args):
    res = _dom_query(page, log_fn, what, **args)
    if out is not None and isinstance(res, dict):
        out.clear()
        out.update(res)
    if not res or not res.get("found"):
        log_fn(f"[i] {label}: в разметке не нашёл")
        return None
    why = res.get("why")
    tail = (f" ({why})" if why else "")
    if res.get("offscreen") or res.get("x") is None:
        if not scroll:
            log_fn(f"[+] {label}: нашёл в разметке <{res.get('tag')}> {res.get('text')!r}, "
                   f"пока вне экрана (top={res.get('top')}) — доскроллю перед кликом" + tail)
            return DOM_OFFSCREEN
        point = _dom_point(page, log_fn, what, what=label)
        if not point:
            return None
    else:
        point = (int(res["x"]), int(res["y"]))
    if res.get("covered"):
        tail += f" [перекрыт: {res.get('blocker')}]"
    log_fn(f"[+] {label}: нашёл в разметке <{res.get('tag')}> {res.get('text')!r} "
           f"в {point}" + tail)
    return point


def _find_form_dom(page, log_fn, timeout_ms: int = 0):
    deadline = time.time() + timeout_ms / 1000
    while True:
        for ctx in _form_frames(page, log_fn):
            res = _dom_query(page, log_fn, "form", ctx=ctx)
            if res and res.get("open"):
                where = "" if ctx.is_main else f", {ctx.label}"
                log_fn(f"[i] Форма в разметке: {len(res.get('fields') or [])} полей "
                       f"({res.get('why')}{where})")
                return res, ctx
        if time.time() >= deadline:
            return None, None
        page.wait_for_timeout(250)


def _click_through_dom_fields(target_page, dom_form: dict, log_fn, max_fields: int = 5,
                              ctx: _FormCtx = None):
    fields = dom_form.get("fields") or []
    if not fields:
        log_fn("[!] Полей в разметке формы нет — пропускаю (не критично).")
        return
    for i in range(min(len(fields), max_fields)):
        hit = _human_click_dom(target_page, log_fn, "field", f"поле формы #{i + 1}",
                               index=i, dwell_ms=(150, 250), ctx=ctx)
        if hit:
            log_fn(f"[+] Кликнул в поле формы #{i + 1} {hit} "
                   f"<{fields[i].get('tag')}> {fields[i].get('text')!r}")
        target_page.wait_for_timeout(random.randint(80, 160))


