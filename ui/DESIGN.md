# Admin UI — Design Language ("terminal-tech")

The Litestar Gateway admin console is a dark, developer-native operational
console. Reference vibe: `graphifylabs.ai`. This document is the source of truth
for the look; the tokens live in [`src/styles/globals.css`](./src/styles/globals.css).

## Vibe

Near-black, green-tinted canvas with a subtle grid and a glowing node-graph
motif. Command-line voice throughout: `$ cmd`, `[ label ]`, `// section`,
`> prompt`, `--flag`. **Dark is the DEFAULT; light is a fully supported mode.**

## Color tokens (shadcn CSS variables — single source of truth)

Declared in `src/styles/globals.css` for both `:root` (light) and `.dark`, and
wired into Tailwind in `tailwind.config.ts`.

### Dark (default)

| Token | Value |
| --- | --- |
| `--background` | `#060a07` |
| `--card` / `--popover` | `#0c120e` |
| `--border` / `--input` | `rgba(34,197,94,.15)` |
| `--foreground` | `#e8f0ea` |
| `--muted-foreground` | `#6b8a76` |
| `--primary` | `#22c55e` (foreground `#060a07`, near-black) |
| `--accent` | `#a78bfa` |
| `--warning` | `#f5a623` |
| `--ring` | `#22c55e` |

### Light

| Token | Value |
| --- | --- |
| `--background` | `#f6f8f6` |
| `--card` | `#ffffff` |
| `--border` / `--input` | `#dde7e0` |
| `--foreground` | `#0f1a13` |
| `--muted-foreground` | `#5b6f61` |
| `--primary` | `#16a34a` |
| `--accent` | `#7c3aed` |
| `--warning` | `#c2740b` |
| `--ring` | `#16a34a` |

`--radius: 0.5rem`. Headline gradient (`.text-gradient`): dark
`#22c55e → #2dd4bf → #a78bfa`, light `#16a34a → #0891b2 → #7c3aed`.

> **Green is an ACCENT, not body text.** Body copy uses `--foreground`
> (off-white / near-black) so all text meets WCAG AA.

## Typography (self-hosted via `@fontsource` — NO Google Fonts / CDN)

| Role | Family | Weights |
| --- | --- | --- |
| Display / headings | **Space Grotesk** | 700 |
| Body / prose | **Inter** | 400 / 500 |
| UI chrome, data, terminal, numbers/tables | **JetBrains Mono** | 400 / 500 / 700 |

Monospace-everywhere-for-chrome (nav, buttons, badges, labels, tabular numbers,
code) is the signature. Fonts are imported in `src/main.tsx` and bundled at
build time — nothing is fetched at runtime.

## Components

- **Buttons** — primary = solid green, near-black mono label, terminal-prefixed
  (`$`, `>`, `--flag`); secondary = ghost/outline green.
- **Badges** — `[ bracketed ]` or `● label`, thin border, mono, color-coded
  (green default, violet/amber categories). See `TerminalBadge`.
- **Cards** — dark/light surface, 1px low-opacity border, `--radius`; section
  headers render as `// comment`.
- **Tables** — dense, mono tabular numbers, thin dividers, status dots
  (green/violet/amber). See `DataTable` + `StatusDot`.
- **Inputs** — thin border; focus = colored ring/glow (`--ring`).
- **Nav** — topbar (logo + theme toggle + user/logout) and a sidebar with mono
  links prefixed `/`; CTA on the right.
- **Background** — subtle grid (`.bg-grid`). A very-low-opacity animated
  "code-rain" appears on the login screen only.

## Motion

Sparing — this is an operational console. Subtle transitions everywhere. The
animated node-graph + code-rain are **login-screen only**. Count-up numbers and a
pulsing `● LIVE` dot (`animate-pulse-live`) are reserved for the usage/stats
views later. No heavy animation on data screens.

## Accessibility

- AA contrast for all text (green is accent-only; body uses foreground).
- Visible focus rings on all interactive elements (`--ring`).
- The theme toggle actually switches dark/light and persists the choice.

## Layout / routing note

The console is served under **`/ui/`** (the gateway's `/` is Swagger UI):
Vite `base: '/ui/'`, TanStack Router `basepath: '/ui'`. The typed API client
targets the gateway API **root (`/`)**, not `/ui`.
