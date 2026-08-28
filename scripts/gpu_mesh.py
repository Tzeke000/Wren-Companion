"""GPU mesh rendering for lyric_viz — wireframe AND shaded, via headless OpenGL.

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

Headless: `moderngl.create_standalone_context()` uses the WGL backend on
Windows — no window, no display server, but it does need a real GPU driver
context (so it will not work from a Session-0 scheduled task).
"""
from __future__ import annotations

import numpy as np

_VERT = """
#version 330
uniform mat4 mvp;
uniform mat3 nrm;
in vec3 in_pos;
in vec3 in_norm;
out vec3 v_norm;
out float v_depth;
void main() {
    gl_Position = mvp * vec4(in_pos, 1.0);
    v_norm = normalize(nrm * in_norm);
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

_FRAG_FLAT = """
#version 330
uniform vec3 base;
out vec4 f_col;
void main() { f_col = vec4(base, 1.0); }
"""


def _perspective(fov_y: float, aspect: float, near: float, far: float):
    f = 1.0 / np.tan(fov_y / 2.0)
    m = np.zeros((4, 4), np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def _rot_xy(ry: float, rx: float):
    cy, sy = np.cos(ry), np.sin(ry)
    cx, sx = np.cos(rx), np.sin(rx)
    Ry = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]],
                  np.float32)
    Rx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]],
                  np.float32)
    return Rx @ Ry


def _translate(z: float):
    m = np.eye(4, dtype=np.float32)
    m[2, 3] = z
    return m


class GpuMeshRenderer:
    """Holds one GL context + FBO and reuses them. Creating a context per frame
    would cost far more than the draw."""

    def __init__(self, width: int, height: int):
        import moderngl
        self.mgl = moderngl
        self.W, self.H = int(width), int(height)
        self.ctx = moderngl.create_standalone_context()
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.prog_shaded = self.ctx.program(vertex_shader=_VERT,
                                            fragment_shader=_FRAG_SHADED)
        self.prog_flat = self.ctx.program(vertex_shader=_VERT,
                                          fragment_shader=_FRAG_FLAT)
        self.fbo = self.ctx.simple_framebuffer((self.W, self.H), components=3)
        self._cache: dict = {}

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
               base=(0.55, 0.75, 1.0), rim=(1.0, 0.45, 0.85)) -> np.ndarray:
        """-> HxWx3 float32 RGB (0-255). Black where nothing was drawn, so the
        caller can add it straight into the frame like any other layer."""
        moderngl = self.mgl
        vbo, ibo, lbo, n_edges = self._upload(key, verts, faces)
        shaded = mode in ("shaded", "solid_wire")
        prog = self.prog_shaded if shaded else self.prog_flat
        # ⚠ The flat shader never reads in_norm, so GLSL strips the attribute
        # and binding it by name raises KeyError. Skip those 12 bytes instead
        # of declaring an attribute the program does not have.
        fmt = [(vbo, "3f 3f", "in_pos", "in_norm")] if shaded else \
              [(vbo, "3f 12x", "in_pos")]
        vao = self.ctx.vertex_array(prog, fmt,
                                    ibo if shaded else lbo,
                                    index_element_size=4)
        proj = _perspective(np.radians(38.0), self.W / self.H, 0.1, 50.0)
        model = _rot_xy(rot, pitch)
        model[:3, :3] *= float(scale)
        mvp = proj @ _translate(-3.2) @ model
        prog["mvp"].write(np.ascontiguousarray(mvp.T, np.float32).tobytes())
        n3 = np.ascontiguousarray(model[:3, :3].T, np.float32)
        if "nrm" in prog:
            prog["nrm"].write(n3.tobytes())
        prog["base"].value = tuple(float(x) for x in base)
        if "rim_col" in prog:
            prog["rim_col"].value = tuple(float(x) for x in rim)
        self.fbo.use()
        self.fbo.clear(0.0, 0.0, 0.0, 1.0)
        self.ctx.line_width = 1.0
        vao.render(moderngl.TRIANGLES if shaded else moderngl.LINES)
        if mode == "solid_wire":
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
        buf = self.fbo.read(components=3, dtype="f1")
        img = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3)
        # GL origin is bottom-left; image origin is top-left
        return np.flipud(img).astype(np.float32)

    def release(self):
        try:
            self.fbo.release()
            self.ctx.release()
        except Exception:
            pass


_RENDERER: "GpuMeshRenderer | None" = None


def get_renderer(width: int, height: int) -> "GpuMeshRenderer | None":
    """Process-wide singleton. Returns None if GL is unavailable, so callers
    can fall back to the CPU path rather than crash a render."""
    global _RENDERER
    if _RENDERER is not None and (_RENDERER.W, _RENDERER.H) == (width, height):
        return _RENDERER
    try:
        if _RENDERER is not None:
            _RENDERER.release()
        _RENDERER = GpuMeshRenderer(width, height)
        return _RENDERER
    except Exception as e:
        print(f"[gpu_mesh] GPU unavailable ({e!r}) - CPU fallback")
        _RENDERER = None
        return None
