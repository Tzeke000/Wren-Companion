# Vector navigation research — 2026-07-17 (two web scouts, pre-dawn)

Zeke's ask: "research what other people have done for AI controlling this body +
general robotics with gyro/depth/camera, then build." Two research agents ran;
this file preserves the load-bearing findings. Full playbook detail lives in the
build code comments (brain/vector_pilot.py, brain/vector_pose.py).

## Scout 1 — what exists for Vector specifically

**Verdict: my stack already exceeds everything public.** The public SOTA is
(a) a 2019 abandoned ROS nav-stack port, (b) marker localization per the
firmware's native design, (c) LLM-as-chat via wire-pod, (d) Princeton's
overhead-camera RL (spatial-intention-maps).

Importable wins:
1. **Frontier exploration off the nav_map** — NOBODY built it; the quadtree's
   content types (esp. "ObstacleProximity unexplored") are a built-in frontier
   signal. → BUILT same night (`body_explore`, frontiers() in vector_planner).
2. **Root-edit the firmware behavior-tree JSONs** (kercre123/victor source +
   randym32/Anki.Resources.SDK parse them; TRM behavior chapter) — retune stock
   reactions AT THE SOURCE (trigger conditions, cooldowns) instead of fighting
   them from the SDK. Zeke-present lever; `systemctl restart anki-robot.target`
   to apply. NOT DONE — future.
3. **wire-pod streaming-TTS chunking** (learnwitharobot writeup) — lower
   perceived latency for vector_say_iris. Future nice-to-have.
4. **vectorax pattern** — RAG-index the victor firmware source + 565-page TRM
   locally for "which C++ function handles X" queries. Future.

Documented DEAD ENDS (never retry): monocular SLAM on this camera (ORB-SLAM2
and LSD-SLAM both tried + failed, mathieu-celerier/vector-ros); community
behavior-tree/subsumption projects (none exist); "vector mapping/exploration"
projects (none exist).

Key repos: kercre123/victor (open firmware — the readable truth for behaviors/
prox/markers), mathieu-celerier/vector-ros (ray-cast fake-laser from nav_map →
AMCL; final answer was pre-drawn map + MCL, validating our markers plan),
jimmyyhwu/spatial-intention-maps (overhead ArUco pose server — the pattern for
our PC-cam stage 2), randym32.github.io/Vector-TRM.pdf.

## Scout 2 — the minimal-sensor playbook (gyro + 1 ToF beam + weak mono cam)

Everything below is established practice; the only novelty in our setup is
substituting whole-robot rotation for a servo pan (same math, costs time).

- **ToF discipline** (BUILT: _tof_read): settle 200-300ms after rotation,
  median of 3-5 reads, range-gate 35..1100mm, no-target = FREE-to-max not
  obstacle. Thin objects (cone legs) under-range in the 25° cone — treat
  "free ≥ d" as cone-center only (matches our cones-thin scar).
- **Rotate-scan polar map + VFH-lite** (BUILT: polar_scan + vfh_pick): sweep
  in 7-9 stops, sectors with dist ≥ clearance are free, contiguous runs =
  valleys, steer valley-center nearest goal bearing. Full VFH+ (hysteresis
  thresholds, masked sectors by turn radius, w_target > w_curr + w_prev cost)
  is the upgrade path if flicker shows up live.
- **Bug algorithms**: Bug2 (m-line) is the standard hobby pick; TangentBug
  (discontinuity points from the scan = local tangent graph) gives near-
  shortest paths with finite-range sensing. Our detour+planner is Bug0-ish +
  global A* — upgrade to Bug2 leave-conditions if live runs loop.
- **Odometry** (largely matches what we have): gyro OWNS heading, encoders own
  distance; SDK pose is already gyro-fused. **Gyrodometry** (Borenstein):
  substitute gyro only when |gyro-odom| spikes (slip/bump events) — firmware
  layer, needs gobot. **Gyro bias re-zero at every stationary moment** —
  firmware-owned for us. **UMBmark** square-drive calibration (CW+CCW
  separates wheel-diameter vs wheelbase error) — do live with Zeke, ~1m square.
- **Re-anchoring** (BUILT v1: vector_pose): dock = origin, every dock hard-
  resets pose trust; **wall-hug re-localization** (Roomba iAdapt): fit a line
  to 3-4 ToF points on a KNOWN wall → snaps heading + one coordinate — build
  after the wall markers are surveyed; **fiducial fixes** = full (x,y,θ) —
  the markers plan, solvePnP w/ IPPE_SQUARE, reject high reprojection error,
  markers ≥ ~60px.
- **Monocular free-space** (NOT built — daylight + decent light needed):
  floor-segmentation "visual laser" — sample floor patch at image bottom, HSV
  back-project, per-column first non-floor row, row→distance via flat-floor
  d = h_cam / tan(pitch + atan((v−v0)/fy)). Output feeds the SAME VFH logic.
  Optical-flow balance + time-to-contact (τ = 1/div) = auxiliary alarm only.
- **Loop rates** (we match): reflex 50-100Hz ideal (our 8Hz reflex + 16Hz
  guard is the slow edge — fine at ≤120mm/s), reactive PD 10-30Hz, planner
  1-10Hz, mission 1-2Hz, every layer reads-fresh-never-blocks.

## What got built off this research (same night)
- `_tof_read` median+gating; `polar_scan`; `vfh_pick`; `_detour` upgraded to
  scan+valley-steer toward goal bearing (`f18214e`..next commit).
- `frontiers()` + `body_explore` mission (the gap nobody filled).
- Earlier same night, independently converging with the playbook: pose-truth
  confidence (vector_pose), hazard memory, A* planner, policy layer.

## Deferred build queue (research-backed, in rough order)
1. Wall-line re-localization (after marker survey; snaps heading cheaply).
2. Floor-segmentation visual ranger (daylight; feeds vfh_pick as extra sectors).
3. Bug2 m-line leave-conditions in _servo_avoid (if live runs ever loop).
4. VFH+ hysteresis + masked sectors (if valley flicker shows live).
5. UMBmark calibration macro (Zeke-present, ~1m square CW+CCW).
6. Firmware behavior-JSON retuning via root (Zeke-present; victor source).
7. vector_say_iris streaming-TTS chunking; vectorax-style TRM RAG index.
