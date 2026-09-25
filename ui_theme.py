"""Shared look for the CCTV web pages (live grid + camera settings): design tokens,
base components, SVG icon sprite and favicon.

Everything is inline -- no web fonts, CDNs or other external requests, because the
server often runs on a LAN without internet access. Pages are written as templates
with three markers that render() fills in:

    /*@theme*/      inside <style>   -> THEME_CSS
    <!--@icons-->   inside <body>    -> ICONS (use as <svg class=ic><use href="#i-back"/></svg>)
    {{FAVICON}}     <link rel=icon>  -> FAVICON (data: URI)
"""
from urllib.parse import quote

THEME_CSS = r"""
:root{
  --bg:#0a0c10;--surface:#10141b;--surface-2:#151a23;--surface-3:#1b212c;--surface-4:#232b38;
  --border:#212833;--border-2:#2c3542;--border-3:#3b4658;
  --text:#e8ebf1;--text-2:#b4bccb;--muted:#7f8899;--faint:#586275;
  --accent:#4f8cff;--accent-2:#6a9eff;--accent-soft:rgba(79,140,255,.16);
  --ok:#22c55e;--ok-soft:rgba(34,197,94,.14);
  --warn:#f59e0b;--warn-soft:rgba(245,158,11,.14);
  --bad:#ef4444;--bad-soft:rgba(239,68,68,.13);
  --r-sm:8px;--r:10px;--r-lg:14px;
  --shadow:0 12px 32px rgba(0,0,0,.5),0 2px 8px rgba(0,0,0,.35);
  --font:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Helvetica Neue",Arial,sans-serif;
  --bar:60px;
  --sat:env(safe-area-inset-top,0px);--sar:env(safe-area-inset-right,0px);
  --sab:env(safe-area-inset-bottom,0px);--sal:env(safe-area-inset-left,0px);
  color-scheme:dark;
}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%;scrollbar-color:#2b3442 transparent}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--font);
     -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
     -webkit-tap-highlight-color:transparent;touch-action:manipulation}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ic{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:2;
    stroke-linecap:round;stroke-linejoin:round;pointer-events:none}

/* buttons: <button>, <a class=btn>; modifiers .primary .ghost .danger .icon .sm */
button,.btn{appearance:none;-webkit-appearance:none;display:inline-flex;align-items:center;justify-content:center;
  gap:7px;height:36px;padding:0 14px;border-radius:var(--r);border:1px solid var(--border-2);
  background:var(--surface-3);color:var(--text);font:500 13.5px/1 var(--font);text-decoration:none;
  white-space:nowrap;cursor:pointer;user-select:none;-webkit-user-select:none;
  transition:background-color .15s,border-color .15s,color .15s,box-shadow .15s,opacity .15s}
button:hover:not(:disabled),.btn:hover{background:var(--surface-4);border-color:var(--border-3)}
button:disabled{opacity:.42;cursor:not-allowed}
button.primary,.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover:not(:disabled),.btn.primary:hover{background:var(--accent-2);border-color:var(--accent-2)}
button.ghost,.btn.ghost{background:transparent;border-color:transparent;color:var(--text-2)}
button.ghost:hover:not(:disabled),.btn.ghost:hover{background:var(--surface-3);border-color:var(--border-2);color:var(--text)}
button.danger{color:#fca5a5}
button.danger:hover:not(:disabled){background:var(--bad-soft);border-color:rgba(239,68,68,.4);color:#fecaca}
button.icon,.btn.icon{width:36px;padding:0}
button.sm,.btn.sm{height:30px;padding:0 10px;font-size:12.5px;border-radius:var(--r-sm);gap:6px}
button.sm .ic,.btn.sm .ic{width:15px;height:15px}
button.icon.sm{width:30px;padding:0}

/* inputs */
input{font:inherit;color:var(--text)}
input[type=text],input[type=search],input[type=number],input:not([type]){height:38px;padding:0 12px;
  border-radius:var(--r);border:1px solid var(--border-2);background:var(--bg);color:var(--text);
  font-size:14px;min-width:0;transition:border-color .15s,box-shadow .15s}
input:hover{border-color:var(--border-3)}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input::placeholder{color:var(--faint);opacity:1}
input[type=number]{-moz-appearance:textfield;appearance:textfield}
input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}

/* app bar + brand */
.appbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:12px;
  min-height:calc(var(--bar) + var(--sat));padding:var(--sat) max(16px,var(--sar)) 0 max(16px,var(--sal));
  background:rgba(13,16,22,.97);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{width:34px;height:34px;flex:none;display:grid;place-items:center;border-radius:10px;color:#fff;
  background:linear-gradient(140deg,#5b97ff 0%,#2f6af0 100%);
  box-shadow:0 4px 14px rgba(47,106,240,.35),inset 0 1px 0 rgba(255,255,255,.25)}
.brand-txt{display:flex;flex-direction:column;min-width:0;line-height:1.2}
.brand-txt b{font-size:15px;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.brand-txt small{font-size:12px;color:var(--muted);white-space:nowrap}
.grow{flex:1}
.hint{color:var(--muted);font-size:12.5px}
.kbd{display:inline-block;min-width:22px;padding:1px 6px;border:1px solid var(--border-2);border-bottom-width:2px;
  border-radius:6px;background:var(--surface-2);color:var(--text-2);font:600 11px/1.5 var(--font);text-align:center}
.spinner{width:24px;height:24px;border-radius:50%;border:2px solid rgba(255,255,255,.1);
  border-top-color:var(--accent);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* floating message: never shifts the layout; click to dismiss */
.toast{position:fixed;left:50%;top:calc(var(--bar) + var(--sat) + 12px);transform:translateX(-50%);z-index:70;
  display:flex;align-items:center;gap:10px;width:max-content;max-width:min(640px,calc(100vw - 24px));
  padding:10px 12px 10px 14px;border-radius:var(--r);background:var(--surface-3);border:1px solid var(--border-3);
  box-shadow:var(--shadow);font-size:13.5px;cursor:pointer;animation:toast-in .18s ease-out}
.toast .tx{flex:1;min-width:0}
.toast.ok{border-color:rgba(34,197,94,.5)} .toast.ok>.ic{color:#4ade80}
.toast.bad{border-color:rgba(239,68,68,.55)} .toast.bad>.ic{color:#f87171}
@keyframes toast-in{from{opacity:0;transform:translate(-50%,-6px)}}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
"""

ICONS = """<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false" style="position:absolute;width:0;height:0;overflow:hidden">
<symbol id="i-cam" viewBox="0 0 24 24"><rect x="2" y="6" width="14" height="12" rx="2.5"/><path d="M16 10.5 22 7v10l-6-3.5z"/></symbol>
<symbol id="i-left" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></symbol>
<symbol id="i-right" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></symbol>
<symbol id="i-up" viewBox="0 0 24 24"><path d="m18 15-6-6-6 6"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-back" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></symbol>
<symbol id="i-max" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></symbol>
<symbol id="i-min" viewBox="0 0 24 24"><path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3"/></symbol>
<symbol id="i-expand" viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></symbol>
<symbol id="i-grip" viewBox="0 0 24 24"><circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></symbol>
<symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></symbol>
<symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/></symbol>
<symbol id="i-rows" viewBox="0 0 24 24"><rect x="3" y="3.5" width="18" height="7.5" rx="1.5"/><rect x="3" y="13" width="18" height="7.5" rx="1.5"/></symbol>
<symbol id="i-off" viewBox="0 0 24 24"><path d="m3 3 18 18"/><path d="M9.5 6H14a2 2 0 0 1 2 2v2.5L22 7v10l-1.3-.8"/><path d="M16 16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h1"/></symbol>
<symbol id="i-list" viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></symbol>
<symbol id="i-reset" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></symbol>
</svg>"""

_FAVICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
                "<rect width='32' height='32' rx='8' fill='#2f6af0'/>"
                "<rect x='5.5' y='10' width='14' height='12' rx='2.5' fill='none' stroke='#fff' stroke-width='2.4'/>"
                "<path d='M19.5 14.5 26.5 11v10l-7-3.5z' fill='#fff'/></svg>")
FAVICON = "data:image/svg+xml," + quote(_FAVICON_SVG)


def render(template):
    """Fill the theme markers of a page template (see the module docstring)."""
    return (template.replace("/*@theme*/", THEME_CSS)
                    .replace("<!--@icons-->", ICONS)
                    .replace("{{FAVICON}}", FAVICON))
