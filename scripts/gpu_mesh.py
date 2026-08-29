"""GPU mesh rendering for lyric_viz — wireframe, shaded, METAL, and metal
backgrounds, via headless OpenGL.

Zeke 2026-08-28 greenlit this as the change that alters what is possible, not
just what is fast.

THE PROBLEM IT REMOVES. `lyric_viz` currently rasterises wireframes with a
Python loop of anti-aliased `cv2.line` calls. Measured on this machine:
900 edges = 21.5 ms, 5 000 = 88.6 ms, 40 000 = 850 ms, 200 000 = 4 190 ms.
That single loop is ~90% of frame time even at the current cap, and it is the
whole reason the model library has a 40-4000 edge budget and why ~25 downloads
were rejected as "too dense". On the GPU a 200k-edge `GL_LINES` draw is
sub-millisecond, so the budget stops being a constraint at all.

AND IT ADDS SOMETHING NEW: real shaded, depth-tested, lit geometry. Wireframe
is a look; this is a different class of image.

METAL (added 2026-08-28 night, Zeke: *"current renders look like retro 3-D
because of the wire, see if we can get maybe some like shiny metallic looking
ones as well"*). This is NOT a recolour of the diffuse shader — a recolour was
the obvious move and it does not work, because what makes an object read as
metal is not a specular dot, it is that it MIRRORS A STRUCTURED ROOM. A metal
has no diffuse term at all: its colour comes entirely from tinting whatever it
reflects (Schlick F0 = the tint). So the shader carries a small procedural
environment — sky gradient, a bright horizon softbox, two hard key lights, and
faint reflected banding — and samples it along the reflection vector. The
environment slowly rotates, which is what turns a still highlight into a
travelling glint as the model spins.

BACKGROUND FIELD. `render_field` draws N instanced faceted solids drifting
toward the camera with the same metal shader — Zeke's *"geometric shapes and
what not, kind of like 3-D metallic moving parts"*. Instanced, so 60 objects
cost one draw call and 7 floats each per frame.

Headless: `moderngl.create_standalone_context()` uses the WGL backend on
Windows — no window, no display server, but it does need a real GPU driver
context (so it will not work from a Session-0 scheduled task).
"""
from __future__ import annotations

import cv2
import numpy as np

_VERT = """
#version 330
uniform mat4 mvp;
uniform mat4 mv;
uniform mat3 nrm;
in vec3 in_pos;
in vec3 in_norm;
out vec3 v_norm;
out vec3 v_eye;
out float v_depth;
void main() {
    gl_Position = mvp * vec4(in_pos, 1.0);
    v_eye = (mv * vec4(in_pos, 1.0)).xyz;
    v_norm = normalize(nrm * in_norm);
    v_depth = gl_Position.z / max(gl_Position.w, 0.001);
}
"""

# Instanced variant: same outputs, but position/rotation/scale come per-object
# from a divisor-1 buffer. The euler->matrix build is done on the GPU so the
# CPU only pushes 7 floats per object per frame instead of a matrix.
_VERT_INST = """
#version 330
uniform mat4 proj;
in vec3 in_pos;
in vec3 in_norm;
in vec3 in_off;
in vec3 in_rot;
in float in_scale;
out vec3 v_norm;
out vec3 v_eye;
out float v_depth;
void main() {
    float ca = cos(in_rot.x), sa = sin(in_rot.x);
    float cb = cos(in_rot.y), sb = sin(in_rot.y);
    float cg = cos(in_rot.z), sg = sin(in_rot.z);
    mat3 Rx = mat3(1.0, 0.0, 0.0,  0.0, ca, sa,  0.0, -sa, ca);
    mat3 Ry = mat3(cb, 0.0, -sb,   0.0, 1.0, 0.0, sb, 0.0, cb);
    mat3 Rz = mat3(cg, sg, 0.0,   -sg, cg, 0.0,  0.0, 0.0, 1.0);
    mat3 M = Rz * Ry * Rx;
    vec3 p = M * (in_pos * in_scale) + in_off;
    v_eye = p;                      // instance offsets are already eye-space
    v_norm = normalize(M * in_norm);
    gl_Position = proj * vec4(p, 1.0);
    v_depth = gl_Position.z / max(gl_Position.w, 0.001);
}
"""

_FRAG_SHADED = """
#version 330
uniform vec3 base;
uniform vec3 rim_col;
in vec3 v_norm;
in float v_depth;
out vec4 f_col;
void main() {
    vec3 N = normalize(v_norm);
    vec3 L = normalize(vec3(-0.35, 0.65, 0.72));
    float diff = max(dot(N, L), 0.0);
    // rim term: edges facing away from the viewer glow. On a dark EDM frame a
    // pure diffuse solid reads as a flat blob; the rim is what gives it shape.
    float rim = pow(1.0 - max(dot(N, vec3(0.0, 0.0, 1.0)), 0.0), 1.7);
    // ⚠ Everything here must stay well under 1.0. v1 used base*(0.18+0.82*diff)
    // + rim*1.35 with base near 1.0, which pinned almost every pixel at 255 and
    // rendered a flat pink silhouette with no readable form (seen by eye). The
    // caller ALSO adds a bloom pass afterwards, so this layer has to leave
    // headroom rather than spend it.
    vec3 c = base * (0.10 + 0.62 * diff) + rim_col * rim * 0.55;
    f_col = vec4(clamp(c, 0.0, 0.92), 1.0);
}
"""

# ---------------------------------------------------------------------------
# METAL. Read the module docstring before touching the constants: the whole
# reason this looks different from _FRAG_SHADED is the environment sample, not
# the specular exponent.
#
# ⚠ Unlike the diffuse shader this one DOES let a few percent of pixels reach
# 1.0, and that is deliberate. The 08-28 saturation scar was about the whole
# model washing out; chrome without any blown highlight is just grey plastic.
# The body of the object sits at ~0.05-0.35 (well under the 190/255 bloom
# threshold) and only the glints cross it, so the downstream bloom haloes the
# highlights instead of flooding the silhouette.
_METAL_BODY = """
uniform vec3 base;        // metal tint = Schlick F0 = what colour the mirror is
uniform vec3 rim_col;     // accent / key-light colour, from the style palette
uniform float env_spin;   // rotates the reflected room -> travelling glints
uniform float fade_far;   // >0: fade to black by this eye-space depth
uniform float fade_near;  // >0: dissolve objects closer than this
uniform float polish;     // 0 = brushed/soft, 1 = mirror
in vec3 v_norm;
in vec3 v_eye;
out vec4 f_col;

vec3 envmap(vec3 R) {
    float y = clamp(R.y, -1.0, 1.0);
    float az = atan(R.z, R.x);              // azimuth -> the room's walls
    // ⚠ TUNED BY EYE, twice. v1 used a near-black room (sky 0.11, floor 0.015)
    // with a razor-thin horizon strip, and the skull came back reading as
    // SMOKED GLASS with a visor line across it, not as chrome. Two things were
    // wrong and only the picture showed either: (a) the environment's dynamic
    // range was so low that most facets mapped to the same near-black value,
    // so a faceted solid looked flat; (b) exp(-|y|*13) is a hard band, and a
    // hard band mirrored across a dome is a straight line, which the eye reads
    // as a painted stripe. A real room is BRIGHT and CLUTTERED.
    vec3 c = mix(vec3(0.030, 0.038, 0.062), vec3(0.30, 0.36, 0.52),
                 smoothstep(-0.70, 0.85, y));
    // wide softbox at the horizon — the single biggest "this is chrome" cue
    c += rim_col * 1.05 * exp(-abs(y) * (2.6 + 3.4 * polish));
    // overhead fill, so up-facing facets are not the same value as down-facing
    c += vec3(0.85, 0.90, 1.00) * 0.45 * pow(max(y, 0.0), 2.5);
    // ROOM STRUCTURE: vertical panels around the azimuth. This is what makes
    // neighbouring facets take DIFFERENT values, which is the whole reason a
    // faceted metal object reads as metal instead of as a grey polyhedron.
    // It also animates: as env_spin advances the panels sweep across the form.
    float panel = sin(az * 3.0 + env_spin) * 0.5 + 0.5;
    c += rim_col * 0.34 * smoothstep(0.55, 1.0, panel) * (0.35 + 0.65 * (y + 1.0) * 0.5);
    c += vec3(0.10) * smoothstep(0.90, 1.0, sin(az * 9.0 - env_spin * 1.3) * 0.5 + 0.5);
    // two hard key lights, offset in phase so glints cross the surface at
    // different rates and the object never looks lit by one lamp
    vec3 L1 = normalize(vec3(cos(env_spin), 0.55, sin(env_spin)));
    vec3 L2 = normalize(vec3(cos(env_spin * 0.67 + 2.4), -0.30,
                             sin(env_spin * 0.67 + 2.4)));
    c += vec3(1.0) * pow(max(dot(R, L1), 0.0), 22.0 + 90.0 * polish) * 1.60;
    c += rim_col * pow(max(dot(R, L2), 0.0), 14.0 + 40.0 * polish) * 0.85;
    return c;
}

void main() {
    vec3 N = normalize(v_norm);
    vec3 V = normalize(-v_eye);
    // two-sided: downloaded meshes are full of flipped winding, and a black
    // back face on a spinning object reads as a hole punched in it
    if (dot(N, V) < 0.0) N = -N;
    float ndv = clamp(dot(N, V), 0.0, 1.0);
    vec3 R = reflect(-V, N);
    // Schlick with F0 = the tint. A conductor has NO diffuse lobe — all of its
    // colour is the tint it puts on the reflection. That is precisely why
    // recolouring a diffuse shader can never look metallic.
    vec3 F = base + (1.0 - base) * pow(1.0 - ndv, 5.0);
    vec3 c = envmap(R) * F;
    // grazing edge light: the bright silhouette every chrome object has. Kept
    // modest — the caller's bloom pass turns this into the halo.
    c += rim_col * pow(1.0 - ndv, 4.0) * 0.40;
    c += base * 0.05;                       // cavities not pure black
    float k = 1.0;
    if (fade_far > 0.0) {
        k *= clamp((fade_far + v_eye.z) / (fade_far * 0.55), 0.0, 1.0);
    }
    // ⚠ NEAR fade matters more than the far one, and it was missing in v1.
    // Without it an object at the near plane subtends most of the frame: at
    // 1080x1920 the field measured 47% coverage and 7.2% blown pixels, and by
    // eye a single octahedron slab covered a third of the screen. That is a
    // fine still and a terrible BACKGROUND — the lyrics have to live there.
    // Dissolving as they pass also removes the pop-out at the near plane.
    if (fade_near > 0.0) {
        k *= smoothstep(fade_near * 0.40, fade_near, -v_eye.z);
    }
    // fade ALPHA too, not just colour — otherwise a faded-out object still
    // punches an opaque hole in whatever is behind it
    f_col = vec4(clamp(c * k, 0.0, 1.0), k);
}
"""

_FRAG_METAL = "#version 330\n" + _METAL_BODY

_FRAG_FLAT = """
#version 330
uniform vec3 base;
out vec4 f_col;
void main() { f_col = vec4(base, 1.0); }
"""

_FRAG_FLAT_INST = """
#version 330
uniform vec3 base;
uniform float fade_far;
uniform float fade_near;
in vec3 v_eye;
out vec4 f_col;
void main() {
    float k = 1.0;
    if (fade_far > 0.0) k = clamp((fade_far + v_eye.z) / (fade_far * 0.55),
                                 0.0, 1.0);
    if (fade_near > 0.0) k *= smoothstep(fade_near * 0.40, fade_near, -v_eye.z);
    f_col = vec4(base * k, k);
}
"""

MODES = ("off", "wire", "shaded", "solid_wire", "metal", "metal_wire")


# -- procedural primitives for the background field -------------------------
# Faceted on purpose: `_upload` computes smooth area-weighted vertex normals,
# which turn a cube into a rounded blob. Metal panels need hard facets, so the
# field geometry is exploded per-triangle (see `_flatten`).

def _octa():
    v = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0],
                  [0, -1, 0], [0, 0, 1], [0, 0, -1]], np.float32)
    f = np.array([[0, 2, 4], [2, 1, 4], [1, 3, 4], [3, 0, 4],
                  [2, 0, 5], [1, 2, 5], [3, 1, 5], [0, 3, 5]], np.int32)
    return v, f


def _box():
    v = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)],
                 np.float32)
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 7, 5], [4, 6, 7],
                  [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                  [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], np.int32)
    return v, f


def _tetra():
    v = np.array([[1, 1, 1], [-1, -1, 1], [-1, 1, -1], [1, -1, -1]], np.float32)
    f = np.array([[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]], np.int32)
    return v, f


def _prism(n: int = 6):
    ang = np.arange(n) * 2 * np.pi / n
    top = np.stack([np.cos(ang), np.full(n, 1.0), np.sin(ang)], 1)
    bot = np.stack([np.cos(ang), np.full(n, -1.0), np.sin(ang)], 1)
    v = np.vstack([top, bot]).astype(np.float32)
    f = []
    for i in range(n):
        j = (i + 1) % n
        f += [[i, j, n + j], [i, n + j, n + i]]
    for i in range(1, n - 1):
        f += [[0, i, i + 1], [n, n + i + 1, n + i]]
    return v, np.array(f, np.int32)


def _torus(nu: int = 20, nv: int = 10, r: float = 0.42):
    u = np.arange(nu) * 2 * np.pi / nu
    vv = np.arange(nv) * 2 * np.pi / nv
    U, V = np.meshgrid(u, vv, indexing="ij")
    x = (1 + r * np.cos(V)) * np.cos(U)
    z = (1 + r * np.cos(V)) * np.sin(U)
    y = r * np.sin(V)
    v = np.stack([x, y, z], -1).reshape(-1, 3).astype(np.float32) / (1 + r)
    f = []
    for i in range(nu):
        for j in range(nv):
            a = i * nv + j
            b = ((i + 1) % nu) * nv + j
            c = ((i + 1) % nu) * nv + (j + 1) % nv
            d = i * nv + (j + 1) % nv
            f += [[a, b, c], [a, c, d]]
    return v, np.array(f, np.int32)


def _extrude(poly: np.ndarray, h: float = 0.35,
             hole: "np.ndarray | None" = None):
    """Extrude a 2D outline along z into a closed solid.

    Caps are fanned from the origin, which is valid because every outline here
    is star-shaped about it (gear, I-beam, plate, hex). With a `hole` outline
    of the same vertex count the caps become a ring of quads instead — that is
    what makes a nut a nut and not a hex puck."""
    p = np.asarray(poly, np.float32)
    n = len(p)
    front = np.hstack([p, np.full((n, 1), h, np.float32)])
    back = np.hstack([p, np.full((n, 1), -h, np.float32)])
    verts = [front, back]
    faces = []
    for i in range(n):                       # side wall
        j = (i + 1) % n
        faces += [[i, n + i, n + j], [i, n + j, j]]
    if hole is None:
        c = len(front) + len(back)
        verts.append(np.array([[0, 0, h], [0, 0, -h]], np.float32))
        for i in range(n):
            j = (i + 1) % n
            faces += [[c, i, j], [c + 1, n + j, n + i]]
    else:
        q = np.asarray(hole, np.float32)
        assert len(q) == n, "hole outline must match the outer vertex count"
        c = 2 * n
        verts += [np.hstack([q, np.full((n, 1), h, np.float32)]),
                  np.hstack([q, np.full((n, 1), -h, np.float32)])]
        for i in range(n):
            j = (i + 1) % n
            faces += [[i, c + i, c + j], [i, c + j, j]]          # front ring
            faces += [[n + i, c + n + j, c + n + i],
                      [n + i, n + j, c + n + j]]                 # back ring
            faces += [[c + i, c + n + i, c + n + j],
                      [c + i, c + n + j, c + j]]                 # bore wall
    v = np.vstack(verts).astype(np.float32)
    v /= max(1e-6, float(np.abs(v).max()))
    return v, np.array(faces, np.int32)


def _ring(n: int, r, phase: float = 0.0) -> np.ndarray:
    a = np.arange(n) * 2 * np.pi / n + phase
    r = np.asarray(r, np.float32)
    return np.stack([np.cos(a) * r, np.sin(a) * r], 1).astype(np.float32)


def _gear(teeth: int = 9):
    """Cog. 4 points per tooth (root, rise, tip, fall) gives a real square
    tooth profile rather than a wavy star."""
    n = teeth * 4
    r = np.tile([0.62, 0.62, 1.0, 1.0], teeth)
    return _extrude(_ring(n, r), 0.30)


def _ibeam():
    """Girder cross-section. Reads as structural steel the instant it tumbles
    edge-on, which no platonic solid does."""
    w, fl, wb = 1.0, 0.30, 0.16      # half-width, flange thickness, web
    p = np.array([[-w, -1.0], [w, -1.0], [w, -1.0 + fl], [wb, -1.0 + fl],
                  [wb, 1.0 - fl], [w, 1.0 - fl], [w, 1.0], [-w, 1.0],
                  [-w, 1.0 - fl], [-wb, 1.0 - fl], [-wb, -1.0 + fl],
                  [-w, -1.0 + fl]], np.float32)
    return _extrude(p, 0.55)


def _nut():
    """Hex nut — outer hex, hex bore. The bore is what sells it: a solid hex
    prism just reads as a fat crystal."""
    return _extrude(_ring(6, 1.0), 0.42, hole=_ring(6, 0.52))


def _plate():
    """Thin panel with clipped corners — the flat metal sheet that tumbles
    edge-on and flashes."""
    c = 0.22
    p = np.array([[-1 + c, -0.62], [1 - c, -0.62], [1, -0.62 + c],
                  [1, 0.62 - c], [1 - c, 0.62], [-1 + c, 0.62],
                  [-1, 0.62 - c], [-1, -0.62 + c]], np.float32)
    return _extrude(p, 0.055)


def _shard():
    """Irregular crystal splinter: an elongated bipyramid on a jittered ring.
    Deterministic — a per-call random shape would flicker between frames."""
    rng = np.random.default_rng(5)
    n = 7
    r = rng.uniform(0.45, 1.0, n).astype(np.float32)
    mid = np.hstack([_ring(n, r), rng.uniform(-0.25, 0.25, (n, 1))
                     .astype(np.float32)])
    v = np.vstack([mid, [[0, 0, 1.55]], [[0, 0, -0.95]]]).astype(np.float32)
    top, bot = n, n + 1
    f = []
    for i in range(n):
        j = (i + 1) % n
        f += [[i, j, top], [j, i, bot]]
    v /= max(1e-6, float(np.abs(v).max()))
    return v, np.array(f, np.int32)


def _pyramid():
    """Square-based pyramid — `--shape pyramid`'s solid form. `tetra` is a
    triangular pyramid and reads differently, so both exist."""
    v = np.array([[-1, -1, -1], [1, -1, -1], [1, -1, 1], [-1, -1, 1],
                  [0, 1.15, 0]], np.float32)
    f = np.array([[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4],
                  [0, 3, 2], [0, 2, 1]], np.int32)
    return v, f


def _sphere(nu: int = 48, nv: int = 26):
    """UV sphere for `--shape orb`. Left SMOOTH (not exploded to facets) —
    a faceted sphere is just a bad polyhedron, and the point of the orb is
    that it is the one round thing in the set.

    ⚠ MISDIAGNOSIS ON RECORD (2026-08-28): the chrome orb shows bright meridian
    stripes, and I read them as visible tessellation and raised this from 24x14
    to 48x26 to "fix" it. The stripes did not change, because they are not mesh
    at all — they are `envmap()`'s azimuthal wall-panel terms (`sin(az*3)` and
    `sin(az*9)`) reflected in a smooth mirror. On a faceted solid those terms
    read as facet-to-facet variation, which is what they are for; on a sphere
    they read as painted stripes. It looks good, so it stays — but the higher
    resolution is NOT what made it look that way, and if someone wants the
    stripes gone the knob is the shader, not this number."""
    u = (np.arange(nu) * 2 * np.pi / nu)[:, None]
    vv = (np.arange(nv) * np.pi / (nv - 1))[None, :]
    x, y, z = np.sin(vv) * np.cos(u), np.cos(vv) * np.ones_like(u), np.sin(vv) * np.sin(u)
    v = np.stack([x, y, z], -1).reshape(-1, 3).astype(np.float32)
    f = []
    for i in range(nu):
        for j in range(nv - 1):
            a = i * nv + j
            b = ((i + 1) % nu) * nv + j
            f += [[a, b, b + 1], [a, b + 1, a + 1]]
    return v, np.array(f, np.int32)


FIELD_KINDS = ("octa", "box", "tetra", "prism", "torus", "gear", "ibeam",
               "nut", "plate", "shard")

# Solid stand-ins for lyric_viz's procedural wireframe shapes, so `--gpu3d
# metal` works on `--shape cube` and friends and not only on `model:` GLBs
# (Zeke 2026-08-28: "give it to all the other shapes as well... sometimes we
# can do the retro wire 3-D look or we can do this more shiny metallic look on
# all the 3-D models and you can interchange them").
SHAPE_PRIMS = {"cube": "box", "pyramid": "pyramid", "cylinder": "cylinder",
               "orb": "sphere", "octa": "octa", "torus": "torus"}
# these read better with hard facets; the sphere and torus stay smooth
FLAT_PRIMS = {"box", "tetra", "prism", "gear", "ibeam", "nut", "plate",
              "shard", "pyramid", "octa", "cylinder"}

_PRIMS = {"octa": _octa, "box": _box, "tetra": _tetra,
          "prism": lambda: _prism(6), "torus": lambda: _torus(),
          "gear": lambda: _gear(9), "ibeam": _ibeam, "nut": _nut,
          "plate": _plate, "shard": _shard,
          "pyramid": _pyramid, "cylinder": lambda: _prism(16),
          "sphere": lambda: _sphere()}


def _flatten(v: np.ndarray, f: np.ndarray):
    """Explode to one vertex per triangle corner so `_upload`'s smooth-normal
    accumulation yields FLAT facets. Without this a cube shades like a ball and
    the metal reads as wet plastic."""
    vv = v[f].reshape(-1, 3).astype(np.float32)
    ff = np.arange(len(vv), dtype=np.int32).reshape(-1, 3)
    return vv, ff


def _perspective(fov_y: float, aspect: float, near: float, far: float):
    f = 1.0 / np.tan(fov_y / 2.0)
    m = np.zeros((4, 4), np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def _rx4(a: float):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]],
                    np.float32)


def _ry4(a: float):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]],
                    np.float32)


def hinge(angle: float, pivot) -> np.ndarray:
    """Rotate about a horizontal axis through `pivot` — a jaw joint.

    translate(p) @ Rx(angle) @ translate(-p). Written out because doing it by
    rotating the vertices on the CPU each frame would defeat the VBO cache,
    which is the entire reason the GPU path is fast."""
    p = np.asarray(pivot, np.float32)
    T = np.eye(4, dtype=np.float32)
    T[:3, 3] = p
    Ti = np.eye(4, dtype=np.float32)
    Ti[:3, 3] = -p
    return T @ _rx4(float(angle)) @ Ti


def _model_rot(yaw: float, nod: float, cam_tilt: float = 0.0):
    """Rx(cam_tilt) @ Ry(yaw) @ Rx(nod) — NOD INSIDE THE SPIN.

    ⚠ THE ORDER IS THE ENTIRE POINT (Zeke 2026-08-28). v1 was Rx(nod) @ Ry(yaw),
    which pitches about the CAMERA's left-right axis. That is only correct while
    the model faces the camera: once it has yawed 90 degrees the camera's X axis
    runs straight through the skull's nose, so the "nod" spins it about its own
    nose and reads as a HEAD TILT. Zeke, who caught it by watching: *"the head
    should always nod from the back of the skull rotating down towards where the
    eyes were... when the skull has turned 90 degrees it is basically tilting its
    head."*

    Putting Rx(nod) INSIDE Ry(yaw) performs the nod in the MODEL's own frame,
    about its own ear-to-ear axis, so the chin drops toward the chest whichever
    way the head is facing. Anything that should stay locked to the camera —
    framing tilt, a tumble — goes OUTSIDE, in cam_tilt.
    """
    return _rx4(cam_tilt) @ _ry4(yaw) @ _rx4(nod)


def _translate(z: float):
    m = np.eye(4, dtype=np.float32)
    m[2, 3] = z
    return m


class GpuMeshRenderer:
    """Holds one GL context + FBO and reuses them. Creating a context per frame
    would cost far more than the draw."""

    def __init__(self, width: int, height: int, ss: int = 1):
        import moderngl
        self.mgl = moderngl
        self.W, self.H = int(width), int(height)
        # SUPERSAMPLING. Everything is drawn at ss x resolution and box-filtered
        # down on readback. Zeke 2026-08-29 wants these to look "like they were
        # rendered in Unity or Blender", and the single most visible difference
        # was not the material or the motion -- it was that every silhouette had
        # hard stair-stepped edges. An offline renderer antialiases; we were the
        # only thing in the frame that did not (the CPU line path already used
        # cv2.LINE_AA, so the GPU models actually looked ROUGHER than the
        # wireframes they replaced).
        # ⚠ DEFAULT 1, WITH MSAA DOING THE WORK. Measured at 640x640:
        #   ss=1 no AA        6.0 ms   hard stair-stepped silhouettes
        #   ss=1 + MSAA 4x    7.8 ms   silhouette clean  <- default
        #   ss=2 + MSAA 4x   26.1 ms   marginally better interior edges
        # Supersampling shades every sub-pixel; multisampling only takes extra
        # coverage samples at edges, which is where all the visible aliasing on
        # a solid model actually is. ~90% of the benefit for +30% instead of
        # +400%. Raise ss only for a deliberate high-quality pass.
        self.ss = max(1, int(ss))
        self.ctx = moderngl.create_standalone_context()
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.prog_shaded = self.ctx.program(vertex_shader=_VERT,
                                            fragment_shader=_FRAG_SHADED)
        self.prog_flat = self.ctx.program(vertex_shader=_VERT,
                                          fragment_shader=_FRAG_FLAT)
        self.prog_metal = self.ctx.program(vertex_shader=_VERT,
                                           fragment_shader=_FRAG_METAL)
        self.prog_inst = self.ctx.program(vertex_shader=_VERT_INST,
                                          fragment_shader=_FRAG_METAL)
        self.prog_inst_flat = self.ctx.program(vertex_shader=_VERT_INST,
                                               fragment_shader=_FRAG_FLAT_INST)
        # ⚠ FOUR components, not three. The FBO carries real coverage in alpha
        # because the caller has to OCCLUDE with a solid object, not add it: a
        # glowing wireframe composites additively and looks right, but the same
        # treatment on a shaded/metal solid makes it TRANSLUCENT — the 08-28
        # first chrome render had background debris clearly visible through the
        # skull's cranium (seen by eye; the brightness stats were all fine).
        # Chrome that you can see through is not chrome.
        self.SW, self.SH = self.W * self.ss, self.H * self.ss
        self.fbo = self.ctx.simple_framebuffer((self.SW, self.SH), components=4)
        # MSAA is the cheap half of this. Full supersampling shades every
        # sub-pixel (measured 6.0 -> 29.3 ms at 640x640, ~5x); multisampling
        # only takes extra COVERAGE samples at edges, which is where all the
        # visible aliasing lives on a solid model. Draw into a multisample
        # renderbuffer, then blit-resolve into the readable FBO. Falls back to
        # plain rendering if the driver refuses, because a slightly jaggy
        # centrepiece beats no centrepiece.
        self.ms_fbo = None
        try:
            self.ms_fbo = self.ctx.framebuffer(
                color_attachments=[self.ctx.renderbuffer(
                    (self.SW, self.SH), 4, samples=4)],
                depth_attachment=self.ctx.depth_renderbuffer(
                    (self.SW, self.SH), samples=4))
        except Exception as e:
            print(f"[gpu_mesh] MSAA unavailable ({e!r}) - drawing unsampled")
            self.ms_fbo = None
        self._cache: dict = {}
        self._field: dict = {}
        self._ibuf = None
        self._ibuf_n = 0

    # -- geometry -------------------------------------------------------
    def _upload(self, key, verts: np.ndarray, faces: np.ndarray):
        """VBOs are cached per (model, mode): re-uploading a mesh every frame
        would put the bottleneck straight back on the bus."""
        if key in self._cache:
            return self._cache[key]
        v = np.asarray(verts, np.float32)
        f = np.asarray(faces, np.int32)
        # per-vertex normals by area-weighted face accumulation
        tri = v[f]
        fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        vn = np.zeros_like(v)
        for k in range(3):
            np.add.at(vn, f[:, k], fn)
        ln = np.linalg.norm(vn, axis=1, keepdims=True)
        vn = vn / np.maximum(ln, 1e-8)
        inter = np.hstack([v, vn]).astype(np.float32)
        vbo = self.ctx.buffer(inter.tobytes())
        ibo = self.ctx.buffer(f.astype(np.int32).tobytes())
        # line indices for wireframe mode
        e = np.unique(np.sort(np.concatenate(
            [f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1), axis=0)
        lbo = self.ctx.buffer(e.astype(np.int32).tobytes())
        out = (vbo, ibo, lbo, len(e))
        self._cache[key] = out
        return out

    def render(self, key, verts, faces, *, rot: float = 0.0,
               pitch: float = 0.0, scale: float = 1.0,
               mode: str = "shaded",
               base=(0.55, 0.75, 1.0), rim=(1.0, 0.45, 0.85),
               spin_env: float = 0.0, polish: float = 1.0,
               clear: bool = True, pre: "np.ndarray | None" = None,
               read: bool = True) -> "np.ndarray | None":
        """-> HxWx4 float32 RGBA (0-255). Alpha = coverage; composite solids
        with it and add wireframe modes directly."""
        moderngl = self.mgl
        vbo, ibo, lbo, n_edges = self._upload(key, verts, faces)
        solid = mode in ("shaded", "solid_wire", "metal", "metal_wire")
        metal = mode.startswith("metal")
        prog = self.prog_metal if metal else (
            self.prog_shaded if solid else self.prog_flat)
        # ⚠ The flat shader never reads in_norm, so GLSL strips the attribute
        # and binding it by name raises KeyError. Skip those 12 bytes instead
        # of declaring an attribute the program does not have.
        fmt = [(vbo, "3f 3f", "in_pos", "in_norm")] if solid else \
              [(vbo, "3f 12x", "in_pos")]
        vao = self.ctx.vertex_array(prog, fmt,
                                    ibo if solid else lbo,
                                    index_element_size=4)
        proj = _perspective(np.radians(38.0), self.W / self.H, 0.1, 50.0)
        model = _model_rot(rot, pitch)
        model[:3, :3] *= float(scale)
        if pre is not None:
            # an extra transform applied in MODEL space, before the spin. This
            # is how an articulated part (a hinged jaw) rides along with the
            # head: same outer matrix, one extra rotation about its own pivot.
            model = model @ np.asarray(pre, np.float32)
        mv = _translate(-3.2) @ model
        mvp = proj @ mv
        prog["mvp"].write(np.ascontiguousarray(mvp.T, np.float32).tobytes())
        if "mv" in prog:
            prog["mv"].write(np.ascontiguousarray(mv.T, np.float32).tobytes())
        n3 = np.ascontiguousarray(model[:3, :3].T, np.float32)
        if "nrm" in prog:
            prog["nrm"].write(n3.tobytes())
        prog["base"].value = tuple(float(x) for x in base)
        if "rim_col" in prog:
            prog["rim_col"].value = tuple(float(x) for x in rim)
        if "env_spin" in prog:
            prog["env_spin"].value = float(spin_env)
        if "polish" in prog:
            prog["polish"].value = float(polish)
        if "fade_far" in prog:
            prog["fade_far"].value = 0.0
        if "fade_near" in prog:
            prog["fade_near"].value = 0.0
        target = self.ms_fbo or self.fbo
        target.use()
        if clear:
            target.clear(0.0, 0.0, 0.0, 0.0)
        self.ctx.line_width = 1.0
        vao.render(moderngl.TRIANGLES if solid else moderngl.LINES)
        if mode in ("solid_wire", "metal_wire"):
            # Shaded surface with its own wireframe over it — the scanned /
            # hologram look. Added because plain shading read FLAT: a skull lit
            # head-on is a smooth dome and the form vanished (seen by eye). The
            # edges put the structure back, and it is the thing neither
            # wireframe alone nor shading alone manages.
            wprog = self.prog_flat
            wprog["base"].value = tuple(float(min(0.95, x * 1.2)) for x in rim)
            wprog["mvp"].write(np.ascontiguousarray(mvp.T, np.float32).tobytes())
            self.ctx.vertex_array(
                wprog, [(vbo, "3f 12x", "in_pos")], lbo,
                index_element_size=4).render(moderngl.LINES)
        return self._read() if read else None

    def facing(self, normal, *, rot: float = 0.0, pitch: float = 0.0):
        """Is a model-space normal pointing at the camera after the spin?

        The camera looks down -Z, so a surface faces us when its rotated normal
        has a positive Z. Needed because `project_points` only tells you a
        point is in front of the CAMERA, which stays true for a feature on the
        far side of a solid object."""
        n = np.asarray(normal, np.float32).reshape(3)
        R = _model_rot(rot, pitch)[:3, :3]
        return float((R @ n)[2])

    def project_points(self, pts, *, rot: float = 0.0, pitch: float = 0.0,
                       scale: float = 1.0):
        """Model-space points -> (screen_xy, in_front) using EXACTLY the same
        matrices as `render`.

        ⚠ It reuses `_model_rot`/`_perspective` rather than re-deriving the
        transform, and that is the point. The 08-28 upside-down-skull bug came
        from two code paths each owning their own idea of the model transform
        and disagreeing by one Y-flip. Anything that needs to know where a
        model feature LANDS on screen must ask the renderer, not recompute."""
        p = np.asarray(pts, np.float32).reshape(-1, 3)
        model = _model_rot(rot, pitch)
        model[:3, :3] *= float(scale)
        mvp = _perspective(np.radians(38.0), self.W / self.H, 0.1, 50.0)             @ _translate(-3.2) @ model
        h = np.hstack([p, np.ones((len(p), 1), np.float32)])
        clip = h @ mvp.T
        w = np.where(np.abs(clip[:, 3]) < 1e-6, 1e-6, clip[:, 3])
        ndc = clip[:, :3] / w[:, None]
        xy = np.stack([(ndc[:, 0] * 0.5 + 0.5) * self.W,
                       (0.5 - ndc[:, 1] * 0.5) * self.H], 1)
        return xy.astype(np.float32), (clip[:, 3] > 0)

    def _read(self) -> np.ndarray:
        if self.ms_fbo is not None:
            self.ctx.copy_framebuffer(dst=self.fbo, src=self.ms_fbo)
        """-> HxWx4 float32 RGBA (0-255). Alpha is coverage: 255 where geometry
        was drawn, 0 where nothing was. Callers composite solids with it and
        may ignore it for wireframe (which is genuinely additive glow)."""
        buf = self.fbo.read(components=4, dtype="f1")
        img = np.frombuffer(buf, np.uint8).reshape(self.SH, self.SW, 4)
        # GL origin is bottom-left; image origin is top-left
        img = np.flipud(img).astype(np.float32)
        if self.ss > 1:
            # INTER_AREA is a true box filter, which is what a downsample wants;
            # INTER_LINEAR would leave the aliasing it is meant to remove.
            img = cv2.resize(img, (self.W, self.H),
                             interpolation=cv2.INTER_AREA)
        return img

    # -- background field ------------------------------------------------
    def _field_geo(self, kind: str):
        if kind in self._field:
            return self._field[kind]
        gen = _PRIMS.get(kind, _octa)
        v, f = _flatten(*gen())
        out = self._upload(("__field__", kind), v, f)
        self._field[kind] = out
        return out

    def render_field(self, *, offs: np.ndarray, rots: np.ndarray,
                     scales: np.ndarray, kind: str = "octa",
                     base=(0.72, 0.76, 0.85), rim=(0.55, 0.70, 1.0),
                     spin_env: float = 0.0, polish: float = 1.0,
                     fade_far: float = 26.0, fade_near: float = 6.5,
                     fov: float = 58.0, wire: bool = False) -> np.ndarray:
        """Instanced metal solids. `offs` are EYE-SPACE positions (z negative =
        in front of the camera), so the caller owns the drift/wrap and this
        stays a pure draw. -> HxWx4 float32 RGBA (0-255)."""
        moderngl = self.mgl
        # `kind` may be a '+' list ("gear+nut+ibeam"): instances are dealt
        # round-robin between the shapes and drawn as one instanced call each.
        # Still 2-4 draws into the SAME framebuffer and ONE readback, so a
        # mixed field costs essentially what a single-shape field costs — and
        # a mixed field is what "a lot more complicated" actually looks like.
        kinds = [k for k in str(kind).split("+") if k] or ["octa"]
        offs = np.asarray(offs, np.float32)
        rots = np.asarray(rots, np.float32)
        scales = np.asarray(scales, np.float32).reshape(-1, 1)
        n_all = int(len(offs))
        prog = self.prog_inst_flat if wire else self.prog_inst
        proj = _perspective(np.radians(fov), self.W / self.H, 0.1, 200.0)
        prog["proj"].write(np.ascontiguousarray(proj.T, np.float32).tobytes())
        prog["base"].value = tuple(float(x) for x in base)
        if "rim_col" in prog:
            prog["rim_col"].value = tuple(float(x) for x in rim)
        if "env_spin" in prog:
            prog["env_spin"].value = float(spin_env)
        if "polish" in prog:
            prog["polish"].value = float(polish)
        prog["fade_far"].value = float(fade_far)
        prog["fade_near"].value = float(fade_near)
        if self._ibuf is None or self._ibuf_n < n_all:
            if self._ibuf is not None:
                self._ibuf.release()
            self._ibuf = self.ctx.buffer(reserve=max(64, n_all) * 7 * 4,
                                         dynamic=True)
            self._ibuf_n = max(64, n_all)
        target = self.ms_fbo or self.fbo
        target.use()
        target.clear(0.0, 0.0, 0.0, 0.0)
        slot = np.arange(n_all) % len(kinds)
        for ki, k in enumerate(kinds):
            sel = slot == ki
            n = int(sel.sum())
            if not n:
                continue
            vbo, ibo, lbo, _ = self._field_geo(k)
            self._ibuf.write(np.hstack([offs[sel], rots[sel], scales[sel]])
                             .astype(np.float32).tobytes())
            self.ctx.vertex_array(
                prog,
                [(vbo, "3f 3f", "in_pos", "in_norm"),
                 (self._ibuf, "3f 3f 1f/i", "in_off", "in_rot", "in_scale")],
                ibo, index_element_size=4).render(moderngl.TRIANGLES,
                                                  instances=n)
        return self._read()

    def release(self):
        try:
            self.fbo.release()
            self.ctx.release()
        except Exception:
            pass


_RENDERER: "GpuMeshRenderer | None" = None


def get_renderer(width: int, height: int,
                 ss: int = 1) -> "GpuMeshRenderer | None":
    """Process-wide singleton. Returns None if GL is unavailable, so callers
    can fall back to the CPU path rather than crash a render."""
    global _RENDERER
    if _RENDERER is not None and (_RENDERER.W, _RENDERER.H,
                                  _RENDERER.ss) == (width, height, ss):
        return _RENDERER
    try:
        if _RENDERER is not None:
            _RENDERER.release()
        _RENDERER = GpuMeshRenderer(width, height, ss=ss)
        return _RENDERER
    except Exception as e:
        print(f"[gpu_mesh] GPU unavailable ({e!r}) - CPU fallback")
        _RENDERER = None
        return None
