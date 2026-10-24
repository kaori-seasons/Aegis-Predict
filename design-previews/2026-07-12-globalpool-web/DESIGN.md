# AEGIS PREDICT Web · DESIGN.md

## 1. Visual Theme & Atmosphere
Warm warehouse utility at high density. Parchment workbench, olive ink, charcoal CTAs, orange only as risk/hot signal. Feels like a real cross-border ops console that escaped a tidy SaaS template — paper grain, dashed lanes, chip queues, bold utility type. Variance 10, motion 8, density 8.

DNA:
- PostHog: parchment `#fdfdf8`, sage borders `#bfc1b7`, orange hover `#F54E00`, dark CTA `#1e1f23`
- Stripe: multi-layer tinted shadow stack, crisp table rhythm
- Linear (map only): cool dark map inset as contrast island, not whole-page dark

## 2. Color Palette & Roles
- Page bg: `#f6f5ee` / parchment `#fdfdf8`
- Surface: `#ffffff`, secondary `#eeefe9`, tertiary `#e5e7e0`
- Ink: `#23251d`, muted `#65675e`, faint `#9ea096`
- Line: `#bfc1b7`, soft line `#d7d8d0`
- CTA / charcoal: `#1e1f23`
- Hot / risk accent: `#F54E00`
- Healthy: `#2f6b3a`
- Watch: `#9a5600`
- Pool / info: `#3d5a40`
- Map deep: `#151912` with olive-tinted land, orange risk nodes

## 3. Typography
- UI/body Chinese stack: `-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif`
- Display latin for eyebrow/code: `"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace`
- No italics. Hierarchy via weight/color/size only.
- Fluid titles with clamp where hero needs punch.

## 4. Components
- Cards: radius 14px, sage border, soft multi shadow, no nested card spam
- Chips / tabs: pill rails, hot chip = charcoal fill
- Buttons: primary charcoal, secondary sage fill, danger orange text or fill sparingly
- Feeds: left accent bar, badge pills for stockout/delay/ok
- Tables: hairline dividers, dense padding, sticky header optional

## 5. Layout
- Sticky chip-style page tabs
- Overview primary: map 1.7fr + order rail .9fr
- High density grids, 8–12px gaps
- Story strip as numbered work steps, not marketing hero

## 6. Depth
- Atmosphere: subtle paper noise + faint grid under body
- Shadows: 2–3 layer tinted stack
- Map is the only deep dark surface

## 7. Motion
- Route pulse, live dot breathe, bar width ease, panel enter slight rise
- Prefer transition over long keyframes
- Respect reduced-motion

## 8. Do / Don't
Do: queue risk first, orange as exception, warm utility density, monospaced clock/ids  
Don't: purple gradients, pure black page, glass everywhere, italic labels, generic blue SaaS chrome

## 9. Responsive
- ≤1040: single column primary, metric 3-col
- ≤720: tabs wrap, full-width controls, charts stack
