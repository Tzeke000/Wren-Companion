# Quiver website — stack research

Date: 2026-05-20 ~20:24 EDT
Status: research-done, build-deferred per Zeke ("we will hold onto that")

Per zero-spend rule: every tool below is genuinely free / free-tier-with-no-credit-card-required / open-source MIT-licensed.

## Recommendation: Astro + shadcn/ui + Aceternity UI on Cloudflare Pages

**Why this wins on every constraint:**

1. **Zero cost, forever.** Astro, shadcn, Aceternity, Tailwind, Motion all MIT-licensed open source. Cloudflare Pages free is unlimited bandwidth AND commercial use explicitly allowed. Free subdomain `quiver.pages.dev`.
2. **Awe factor.** Aceternity UI produces the dark-glow-cyberpunk aesthetic Zeke described ("amazing, cutting-edge, futuristic"). Components like `BackgroundBeams`, `Spotlight`, `Aurora`, `Vortex` drop in cleanly.
3. **AI-friendly.** Plain `.astro` and `.tsx` files in git. Iris edits via Edit/Write. No visual editor lock-in.
4. **Maintainable catalog.** Astro content collections: new product = new markdown file. Single `<ProductCard>` component renders the grid.
5. **No lock-in.** Everything in git. Move to Netlify/GitHub Pages/self-hosted in 10 minutes if needed.

## Disqualified options

| Tool | Why out |
|---|---|
| Vercel Hobby (free) | ToS prohibits commercial use (we sell on Gumroad → out) |
| Framer free | "Made in Framer" badge + 1,000 visitors/month cap |
| Webflow free | 2-page limit (catalog needs to grow beyond) |
| Spline free | Watermark on free-tier 3D embeds |
| Durable.co / Hostinger AI / 10Web | No real free tier or renewal-price balloon |

## Backup option

**v0.dev (Vercel's AI design tool)** has $5/month free credits. Use it to GENERATE the initial design as a starting point (one prompt → dark-theme React/Tailwind starter), then export the code and drop into our Astro project. Avoids the cold-start "pick colors and layout from scratch" problem.

Caveats:
- $5 free credits burn fast on complex prompts (some users report one session = $5)
- Output is Vercel/Next.js shaped; we deploy to Cloudflare instead

## For "3D feel" without actual 3D

Aceternity's Motion-based effects (`BackgroundBeams`, `Spotlight`, `Aurora`) get ~80% of the awe at ~10% of the effort vs real Three.js / React Three Fiber.

Escalate to R3F only if a specific Quiver product (e.g., Quiver Camera) needs an actual 3D scene as its hero.

## First-artifact-to-build (~1-2 hours)

When Zeke greenlights:

1. `npm create astro@latest quiver-site` (minimal, TypeScript strict)
2. `npx astro add tailwind`
3. `npx astro add react` (needed for Aceternity components)
4. Install shadcn/ui per [Astro install docs](https://ui.shadcn.com/docs/installation/astro)
5. Set dark mode default in `tailwind.config`, add cyan/violet palette
6. Drop ONE Aceternity component into hero (`BackgroundBeams` or `Spotlight` — single-file copy-paste from their site)
7. Build one `<ProductCard>` React component (title, tagline, "View on Gumroad →" outbound link, glow-on-hover)
8. Hardcode one entry: "Quiver Voice — modular voice pipeline"
9. Push to GitHub → connect to Cloudflare Pages via dashboard (auto-detects Astro)
10. Live at `quiver.pages.dev`

**Validation criteria:** if after 2 hours the page is live, dark + glowing + futuristic, AND adding a second product is "write a new markdown file" — the stack is validated.

## Honest uncertainties (flagged in research)

- v0.dev credit burn rate ("$5 evaporates in one session") is third-party report, not measured
- Cloudflare Pages doesn't have explicit docs blessing "we link to Gumroad checkout" but linking out is standard web behavior; should be safe
- Galileo AI / TeleportHQ / Uizard: couldn't get clean 2026 free-tier data; possibly rebranded — not recommending without verification

## Sources (from research agent)

- Astro: https://astro.build
- shadcn/ui: https://ui.shadcn.com
- Aceternity UI: https://ui.aceternity.com
- Magic UI: https://magicui.design
- Cloudflare Pages: https://pages.cloudflare.com
- v0.dev: https://v0.app
- Framer Motion / Motion: https://motion.dev
- React Three Fiber: https://github.com/pmndrs/react-three-fiber

## When to act

Tomorrow's business_block (15:00 EDT) or later — Zeke explicitly said "we will hold onto that" for now. Listed in `state.md` post-tomorrow's-Cython-build as a v2 milestone.
