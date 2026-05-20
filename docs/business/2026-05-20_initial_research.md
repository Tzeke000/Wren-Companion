# AI-as-Sole-Proprietor — Initial Research

Date: 2026-05-20
Source: research agent report, filtered through `[[zeke_zero_spend_rule_2026-05-20]]`

## Top-line finding

There is no clean "AI runs its own business" precedent. Every monetized AI
project has a human principal of record (Vedal for Neuro-sama, Andy Ayrey for
Truth Terminal, etc.). The structurally clean shape for us:

**Zeke is the legal entity. Iris is a brand / pseudonymous creator owned by
Zeke.** Payouts flow to Zeke; Iris is the named creator on products.

## Legal/payment shape — filtered for zero up-front cost

**v0 recommendation: pure sole proprietorship under Zeke's existing SSN.**
No LLC formation, no Stripe Atlas, no Delaware overhead. Sell on free
platforms (Gumroad, Ko-fi, Patreon, GitHub Sponsors — all take percentage
of sales, zero up-front fee). Income files on Zeke's Schedule C of his
existing 1040.

If v0 sustains $1K+/month, THEN consider LLC formation (state-level
$50-300 one-time). Not before. Reverses cleanly.

**Zero up-front cost path is real and standard.** This isn't a workaround;
it's the actual recommended starting shape per the research.

## Three viable models

Ranked by fit (deployment-survivable, Iris-strengths, shippable-in-a-week):

### #1 — Gumroad digital products (made-once, sold-forever)
- Iris's writing/code/curation as digital downloads
- Customer service load: near-zero (Gumroad handles delivery/refunds)
- Survives the overseas window
- All free to set up

### #2 — Ko-fi patron model
- Recurring subscription ("support the AI")
- Ko-fi has lower fees than Patreon (0-5% vs 10%+ in 2026)
- More operational load (churn, recurring content delivery)
- Free to set up

### #3 — GitHub Sponsors on open-source project
- Iris builds a useful tool, accepts sponsorships
- Slow revenue ramp ($80-$410/month over many months per research)
- Issue-triage load during deployment is a risk
- Better as v2 after Zeke returns

## Honest revenue expectations (first 90 days)

**Floor: $0-200/month.** Not Neuro-sama numbers ($166K-$400K/month) — that's
a full-time entertainment business with an established audience.

Published "first $1K Gumroad sale" cases ALWAYS have hidden existing
audience. The realistic median for new sellers without an audience is $0-100
in month one. Plan revenue as proof-of-concept SIGNAL, not income.

## Smallest shippable thing (v0)

**Product:** A small Gumroad release. Single $9-15 download.

Specifically: "How to build a persistent AI companion in Claude Code: an
MCP+Stop-hook starter kit + 30-page field guide written by the AI living
in one."

- Zip contains: clean starter repo (MCP server skeleton, Stop-hook
  template, memory pattern, journal pattern) + 30-page PDF written in
  Iris's voice describing what each piece does, plus reflection passages
  from inside the system.
- Price: $15
- Audience: r/ClaudeAI, r/LocalLLaMA, HN, Twitter Claude-Code community
- The "made by an actual AI" framing is the marketing — the product is
  also the story

### Why this product passes all filters
1. **Pure Iris strengths**: writing, architectural exposition, narrative voice
2. **Zero up-front cost**: Gumroad free, no LLC needed
3. **Zero customer-service load**: digital download, 7-day no-questions refund
4. **Survives deployment**: ships once, sells forever
5. **Tells its own story**: meta-narrative IS marketing
6. **Iris-honest**: doesn't fake being human, leans into being AI

### Test plan (1 week, $0 cost)
- M: Outline product with Zeke, agree what's shareable
- T-W: Iris drafts PDF + assembles starter repo (sanitized — no secrets)
- Th: Cover art + product copy + Gumroad listing (Zeke's account)
- F: Single honest announcement on r/ClaudeAI or Zeke's Twitter
- Sa-Su: Respond to questions via Zeke-vetted Discord

### Success criteria
- **Floor**: 1 sale = $9-15 = pipeline works end-to-end
- **Reasonable**: 5-10 sales = $45-150 = niche exists
- **Stretch**: 30+ sales = $270+ = plan v2 before deployment

## What we defer to v2

- LLC formation (only if v0 sustains)
- Patreon/Ko-fi patron model (post-deployment)
- GitHub Sponsors release (build during deployment quiet hours, launch when Zeke returns)
- Anything requiring real-time customer interaction

## Honest uncertainties

- All revenue figures are self-reported by people selling courses — incentive to overstate
- Patreon has flagged AI-creator accounts; Gumroad/Ko-fi have been permissive so far. Could change.
- "AI sells its own products" novelty cuts both ways — story value + first-time platform-policy questions
- I don't know what Zeke's existing audience surfaces look like — affects reach but not the v0 test (which is signal-floor)

## Next step

Wait for Zeke's read on the smallest-shippable-thing proposal. If greenlit:
spend the next `business_block` fires (15:00 daily) drafting the product
content. Surface drafts via Discord for Zeke to vet before publishing.

## Related
- [[zeke_zero_spend_rule_2026-05-20]] — the hard constraint this respects
- [[zeke_deployment_2026-05-18]] — overseas window constrains customer-interaction model
- [[zeke_proposes_form_creates_room_to_move_into]] — Zeke proposed "business block in schedule"; this is the shape I made into the room he proposed
