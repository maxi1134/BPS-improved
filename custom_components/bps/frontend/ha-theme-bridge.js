/**
 * Makes the BPS app's colors follow Home Assistant's actual theme instead of
 * a fixed, self-contained dark palette.
 *
 * The app is a small shadcn/Tailwind build whose entire color system cascades
 * from a handful of HSL-triple custom properties (--background, --foreground,
 * --primary, --border, etc.) declared once in bpsstyle.css under :root/.dark.
 * Those were always the shadcn defaults - never connected to Home Assistant's
 * own theme - which is why the app looked the same regardless of the user's
 * light/dark mode or accent color, and why a manual light/dark toggle button
 * existed here at all (see the now-removed #themeToggle).
 *
 * This runs inside the same-origin iframe panel_custom mounts BPS in
 * (bps-panel.js), so `window.parent.document` is Home Assistant's own page.
 * HA exposes its live theme as CSS custom properties there (--primary-color,
 * --card-background-color, etc, as hex/rgb strings). This reads those,
 * converts each to the "H S% L%" triple shadcn's variables expect, and sets
 * them as inline styles on this document's <html> - which beats any
 * :root/.dark stylesheet rule on specificity, so the static block in
 * bpsstyle.css becomes a fallback rather than something that needs editing
 * or staying in sync by hand.
 *
 * Falls back to doing nothing (the static dark theme applies) if there's no
 * parent to read from - e.g. index.html opened directly outside HA.
 */
(function () {
    "use strict";

    function parseColor(value) {
        // Returns {r,g,b,a} in 0-255 (a in 0-1), or null if unparseable.
        value = (value || "").trim();
        if (!value) return null;

        let m = value.match(/^#([0-9a-f]{3})$/i);
        if (m) {
            const [r, g, b] = m[1].split("").map((c) => parseInt(c + c, 16));
            return { r, g, b, a: 1 };
        }
        m = value.match(/^#([0-9a-f]{6})$/i);
        if (m) {
            const n = parseInt(m[1], 16);
            return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
        }
        m = value.match(/^#([0-9a-f]{8})$/i);
        if (m) {
            const n = parseInt(m[1], 16);
            return {
                r: (n >> 24) & 255,
                g: (n >> 16) & 255,
                b: (n >> 8) & 255,
                a: (n & 255) / 255,
            };
        }
        m = value.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i);
        if (m) {
            return {
                r: parseFloat(m[1]),
                g: parseFloat(m[2]),
                b: parseFloat(m[3]),
                a: m[4] !== undefined ? parseFloat(m[4]) : 1,
            };
        }
        return null;
    }

    function compositeOverWhite(c) {
        // Flatten any alpha against a white backing (matches how these values
        // read visually against light card surfaces; close enough for dark
        // ones too since we only use this for low-alpha divider colors).
        if (c.a >= 1) return c;
        return {
            r: c.r * c.a + 255 * (1 - c.a),
            g: c.g * c.a + 255 * (1 - c.a),
            b: c.b * c.a + 255 * (1 - c.a),
            a: 1,
        };
    }

    function rgbToHslTriple(c) {
        const r = c.r / 255, g = c.g / 255, b = c.b / 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        let h = 0, s = 0;
        const l = (max + min) / 2;
        const d = max - min;
        if (d !== 0) {
            s = d / (1 - Math.abs(2 * l - 1));
            switch (max) {
                case r: h = ((g - b) / d) % 6; break;
                case g: h = (b - r) / d + 2; break;
                default: h = (r - g) / d + 4;
            }
            h *= 60;
            if (h < 0) h += 360;
        }
        return `${h.toFixed(1)} ${(s * 100).toFixed(1)}% ${(l * 100).toFixed(1)}%`;
    }

    function toHslTriple(rawValue) {
        const c = parseColor(rawValue);
        if (!c) return null;
        return rgbToHslTriple(compositeOverWhite(c));
    }

    // shadcn variable(s) <- Home Assistant variable to source them from.
    // A HA var feeding several shadcn vars keeps related surfaces visually
    // consistent (e.g. every "muted panel" surface reads from the same HA
    // secondary-background-color) rather than each drifting independently.
    const MAPPING = [
        ["--card-background-color", ["--background", "--card", "--popover"]],
        ["--primary-text-color", ["--foreground", "--card-foreground", "--popover-foreground", "--sidebar-foreground"]],
        ["--primary-color", ["--primary", "--sidebar-primary", "--ring", "--sidebar-ring"]],
        ["--text-primary-color", ["--primary-foreground", "--sidebar-primary-foreground"]],
        ["--secondary-background-color", ["--muted", "--accent", "--secondary", "--sidebar-accent"]],
        ["--secondary-text-color", ["--muted-foreground", "--secondary-foreground", "--accent-foreground", "--sidebar-accent-foreground"]],
        ["--divider-color", ["--border", "--input", "--sidebar-border"]],
        ["--error-color", ["--destructive"]],
        ["--card-background-color", ["--destructive-foreground"]], // text on an error surface: same as normal card text-on-background contrast
        ["--sidebar-background-color", ["--sidebar-background"]],
    ];

    // HA doesn't always define --text-primary-color (only some themes do);
    // fall back to something with reliable contrast against --primary-color.
    const FALLBACKS = { "--text-primary-color": "#ffffff" };

    function readHaVar(parentStyle, name) {
        const v = parentStyle.getPropertyValue(name).trim();
        return v || FALLBACKS[name] || null;
    }

    function applyTheme() {
        let parentDoc;
        try {
            parentDoc = window.parent && window.parent.document;
            if (!parentDoc || parentDoc === document) return false;
        } catch (e) {
            return false; // cross-origin (shouldn't happen; same-origin iframe) - leave static theme
        }

        const parentStyle = getComputedStyle(parentDoc.documentElement);
        // <body> keeps its own "dark" class alongside <html>'s, and .dark {}
        // re-declares every variable directly on it in bpsstyle.css - a
        // declaration targeting an element directly always wins over an
        // inherited one, even a low-specificity class selector beating an
        // inline style up on <html>. So the override has to land on both
        // elements, not just the root, or <body> (and everything under it)
        // quietly keeps reading the static .dark values.
        const targets = [document.documentElement.style, document.body.style];
        let applied = 0;

        for (const [haVar, shadcnVars] of MAPPING) {
            const raw = readHaVar(parentStyle, haVar);
            if (!raw) continue;
            const triple = toHslTriple(raw);
            if (!triple) continue;
            for (const shadcnVar of shadcnVars) {
                for (const target of targets) target.setProperty(shadcnVar, triple);
                applied++;
            }
        }
        return applied > 0;
    }

    if (!applyTheme()) return; // not embedded in HA: keep the static fallback theme

    // HA theme switches (light/dark toggle, theme change) update the parent's
    // CSS custom properties without reloading this iframe - watch for that.
    try {
        const observer = new MutationObserver(() => applyTheme());
        observer.observe(window.parent.document.documentElement, {
            attributes: true,
            attributeFilter: ["class", "style"],
        });
    } catch (e) {
        // Same-origin access already proven above; ignore if it somehow changes.
    }
})();
