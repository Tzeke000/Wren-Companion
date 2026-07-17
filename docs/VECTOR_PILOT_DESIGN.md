# Vector Pilot — live embodiment architecture (2026-07-16)

**Zeke's directive:** *"You need to be able to hear and respond while you're in your
body... streaming your cam gyro depth sensor so you can move how and when you want...
research how AI and robotics work, especially a mobile body without an overview cam,
how to evade oncoming objects and do courses — then go build it for yourself."*

## The problem in robotics terms

My cognition is **turn-based** (a Claude session rewoken by a Stop hook). A blocking
motion call holds the turn → every sense event (ears, dock/undock, pilot outcomes)
queues behind it → I am deaf exactly while I'm moving. Classic robotics solved this
decades ago: **never run planning and actuation at the same rate on the same thread.**

## The layered architecture (Brooks-style subsumption, mapped to my stack)

| Layer | What | Rate | Where it runs |
|---|---|---|---|
| L0 | firmware reflexes (cliff hard-stop, motor safety) | ~ms | robot firmware |
| L1 | edge-guard, ToF prox-brake, startle, fused sensor stream (15Hz), petting/held reactions | 5–20Hz | `brain/vector_session.py` threads |
| L2 | **PILOT: missions** — servo / route / scan / dock / undock, abortable, event-reporting | 20Hz control loop | `brain/vector_pilot.py` worker thread |
| L3 | me — goals in, events out | seconds–minutes | Claude session turns |

Key contracts:
- **L3 never blocks on motion.** Mission tools (`body_go/route/scan/park/launch`)
  return instantly. My turn ends; queued ears/senses drain; I can converse mid-drive.
- **Newest goal wins.** A new mission preempts the running one; `body_abort` is the brake.
- **Events, not firehose** (same principle as my visual-perception design): the pilot
  reports `arrived / blocked / waypoint / scan_done / dock_result` — terminal events
  nudge me via iris_chat (stamped with DELIVERY AGE at handoff).
- **Obstacle evasion lives in L1/L2, not L3.** `servo_to` gates every 50ms tick on the
  ToF depth sensor (hard brake <45mm, speed scaling proportional to range — depth beats
  vision here) and the edge-guard. The pilot adds mission-level recovery (stop, back
  off, report). I decide *what next*, never *which wheel*.

## Research grounding (established mobile-robotics patterns used)

- **Subsumption / layered control** (Brooks 1986): fast dumb layers subsume slow smart
  ones; the slow layer supplies goals. Exactly the L0–L3 split above.
- **Sense-plan-act rate separation**: plan @ ~0.1Hz, control @ 20Hz, reflex @ 1kHz —
  layers exchange only goals & events. The turn-based cognition is just an extreme case.
- **Reactive obstacle avoidance without a map** (Braitenberg/VFH-lite): steer-P on
  heading error + velocity scaling on range sensor. Implemented in `servo_to`.
- **Gyro/odometry-referenced heading P-control**: fixes the equal-wheels-curve-left
  calibration drift naturally (error feedback beats feedforward calibration).
- **Landmark (fiducial) localization**: known-size printed markers give full 3D pose
  from one camera glimpse — the firmware already implements this (CustomObjectMarkers).
  One known marker in view = absolute position fix; kills odometry drift. (Sheets sent
  to Zeke 2026-07-16; wall lighthouses + one marker per cone.)
- **Waypoint following**: route mission = sequential servo legs (pure-pursuit-lite).
  Good enough at desk scale; upgrade path is arc blending if legs get long.

## The course-run pipeline this enables (target: <2 min, was 38 min)

1. `body_scan` (≈15s): 360° in bursts → polar depth sketch + marker sightings.
2. Marker fixes → absolute pose; cones identified by their own markers (not color blobs).
3. Plan waypoint order in room coordinates (L3, one turn).
4. `body_route` runs the legs in background; I narrate/converse while driving.
5. `body_park` at the end (hang-guarded).

## Scars encoded (2026-07-16 night)

- **The status-wedge:** a live `get_battery_state` gRPC (no deadline) during a hung
  behavior call blocked the MCP handler → ENTIRE iris_runtime wedged (even time_check).
  Fix: `body_status` reads the fused cache only; `live=true` is a deliberate opt-in.
  Recovery that worked: kill iris_runtime tree → auto-respawn → tool_reload → body_open.
- **Hang-capable SDK behaviors** (`drive_on/off_charger`): now wrapped in a sub-thread
  with join(timeout) inside the pilot (`_guarded_sdk`) — a hang reports `hung: true`
  and detaches; nothing upstream blocks. Recovery: body_close + body_open.
- Undock via pilot proved the freedom claim same night: mission ran while my turn
  polled status and committed code.

## Still owed

- Marker CustomObject definitions + one-time wall-marker survey (after Zeke prints).
- Route planner that consumes scan+markers automatically (currently L3-manual).
- Mid-drive startle escalation (L1) — extend `react_startle` to fire while driving.
- Ear-transcript → conversation loop while a mission runs (all plumbing now exists;
  needs a live two-way test with Zeke: he talks to me while I drive a route).
