# The Human Visual System as a Staged Filtering Pipeline — research grounding for Iris's perception mechanisms

**Why this exists:** Zeke (2026-06-28) proposed Iris's perception mechanisms be **multi-stage pipelines modeled on how human vision actually works**, and invited grounding research. This is that grounding — visual neuroscience distilled toward software perception-pipeline design. Feeds the mechanism-registry design in `IRIS_AGENT_SDK_MIGRATION_DESIGN.md` §6b.

## The pipeline, stage by stage (what each computes, what it passes on)

- **Retina — the eye computes before the optic nerve.** Photoreceptors → bipolar → ganglion cells, with horizontal/amacrine lateral processing. ~**100:1 compression** (≈100M photoreceptors → ≈1M optic-nerve axons; varies by eccentricity — near 1:1 in the foveal midget system, hundreds:1 in rod periphery). **Center-surround** receptive fields (lateral inhibition) make ganglion cells encode **local contrast/edges, not absolute light** — redundant uniform illumination is discarded for free. **ON/OFF** parallel channels. Early **direction/motion selectivity** and **color opponency** already computed. Output = features (contrast, edges, motion, color-opponency), not pixels.
- **LGN (thalamus)** — relay + gatekeeper; keeps **magno/parvo/konio streams segregated**; first strong point of top-down/attentional modulation.
- **V1** — simple cells = oriented edges (position/orientation/spatial-freq); complex cells add position invariance; start of a **bottom-up saliency map**.
- **V2** — contours, angles, illusory contours, border-ownership, figure-ground.
- **V4** — intermediate shapes, curvature, color constancy; target of top-down attention.
- **MT/V5** — dorsal motion hub: global motion, speed, optic flow, depth-from-motion.
- **Ventral "what" stream** (V1→V2→V4→**IT cortex**) — object identity, form, color, **faces** (face-selective patches); increasingly invariant/abstract; tied to recognition + memory.
- **Dorsal "where/how" stream** (V1→V2→MT→**posterior parietal**) — spatial location, motion, **visuomotor guidance** (saccades, reaching). Goodale & Milner: "how" (vision-for-action), not just "where."

## Attention & salience gating — what reaches awareness

- The brain **processes far more than it represents.** A **saliency map** (substrates: V1, superior colliculus, pulvinar, parietal, V4, FEF) encodes how strongly each location pulls attention.
- **Bottom-up salience = feature contrast** — luminance/color/orientation/motion difference from surround → automatic pop-out. V1 computes much of it; **superior colliculus** drives bottom-up capture + orienting saccades.
- **Top-down attention** — goals bias the map via FEF, LIP, pulvinar (gain modulation in V4/V1). Goals **re-weight**, they don't bypass.
- **Attention is a GATE, not whole-scene processing.** **Inattentional blindness** (invisible-gorilla) and **change blindness** prove only attended, salience-selected info reaches conscious report; the rest is processed coarsely and discarded. Biology accepts missing the unattended in exchange for tractable cost.

## Magno vs Parvo — the cheap-fast / expensive-fine split (the key design analogy)

| | **Magnocellular (M / parasol, ~10% RGCs)** | **Parvocellular (P / midget, ~80% RGCs)** |
|---|---|---|
| Job | "Something changed — look!" | "What exactly is it?" |
| Resolution | Low/coarse | High/fine (acuity) |
| Temporal | Fast, transient | Slow, sustained |
| Color | Color-blind (luminance) | Color-opponent |
| Feeds | Dorsal/motion, fast orienting | Ventral/form & color |

Split is **established in the retina**, segregated through **separate LGN layers**, feeds dorsal vs ventral. M = cheap fast coarse early-warning; P = expensive slow detailed identification.

## Architectural takeaways for Iris's staged mechanism pipeline

1. **Compress at the sensor; transmit features, not pixels.** Earliest always-on stage outputs edges/contrast/motion-deltas, never raw frames. Bandwidth to cognition is the scarce resource.
2. **Early stages = contrast/change detectors, not absolute-value reporters.** Fire on difference-from-surround / difference-from-last-frame (center-surround = local differencing). Discards redundant input for free.
3. **Parallel fast-coarse (magno) + slow-fine (parvo) paths.** A cheap low-res high-rate "something moved" channel runs always-on and triggers orienting; an expensive high-res "identify it" channel runs only when invoked. Don't pay full-detail cost every frame.
4. **Salience = feature contrast, computed early + cheap.** Lightweight saliency (luminance/color/orientation/motion outliers vs local surround). Pop-out is local — it doesn't need object recognition to fire.
5. **Attention is a gate, not exhaustive filtering.** Select first, process the selection. Only salience-flagged / goal-relevant regions get promoted to expensive late stages.
6. **Fuse bottom-up salience with top-down goals at the gate.** Selection sums stimulus-driven salience + task priors (what cognition currently cares about). Goals re-weight, not bypass.
7. **Hierarchical, increasingly invariant features; split "what" from "where."** Stack local→global (edges→contours→shapes→objects); keep an expensive selective **identity** path and a fast **location/motion** path — different latency/cost budgets, keep them separate.
8. **Only salient/identified events reach central cognition.** The optic nerve is a deliberate bottleneck. Cognition gets a sparse stream of *resolved events* ("face X appeared at Y, moving left"), not the sensor firehose. Push filtering to the edge; keep the central reasoner's input small + pre-digested.

## Sources
Webvision (Univ. Pittsburgh, Kolb — retinal compression/center-surround/parasol-midget); ScienceDirect "Retinal Ganglion Cell"/"Visual Pathway"; Kandel *Principles of Neural Science*; Wikipedia "Visual cortex"; OpenStax *Intro to Behavioral Neuroscience* 6.5; Scholarpedia "What and where pathways"; Li/Zhaoping "Neural Activities in V1 Create a Bottom-Up Saliency Map" (*Neuron*); *PNAS* 2018 "Bottom-up saliency and top-down learning in V1"; *Comms Biology* 2025 "Preference-independent saliency map in mouse SC"; PMC3704631 (spiking SC saliency model); PMC11621853 (change-blindness/attention review); *J. Neurosci.* 40(42):8132 (human magno/parvo pathways); UMN Pressbooks "Magnocellular and Parvocellular pathways"; *PNAS* 1997 (early M/P divergence).

*(Caveat: the ~100:1 compression is the standard textbook order-of-magnitude; convergence varies hugely by eccentricity. Treat as a design heuristic — compress hard in the periphery, keep a high-acuity fovea — which is itself a useful pipeline principle.)*
