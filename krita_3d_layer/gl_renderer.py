"""
Offscreen OpenGL 3D renderer for the Krita 3D Layer plugin.

Uses QOffscreenSurface + QOpenGLContext + FBO to render 3D scenes with
hardware acceleration, then displays the result in a regular QWidget
via QPainter.  This sidesteps the QOpenGLWidget-in-docker compositing
issue that causes a black screen inside Krita.
"""

import math
import os
import tempfile
import traceback
import ctypes
import ctypes.util

from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import (
    QImage, QPainter, QColor, QSurfaceFormat, QOpenGLContext,
    QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLShaderProgram, QOpenGLShader, QMatrix4x4, QVector3D
)
from PyQt5.QtWidgets import QWidget, QSizePolicy

try:
    from PyQt5.QtGui import QOffscreenSurface
except ImportError:
    try:
        from PyQt5.QtCore import QOffscreenSurface
    except ImportError:
        QOffscreenSurface = None

# ── Logging ──────────────────────────────────────────────────────────────

_LOG = os.path.join(tempfile.gettempdir(), "krita_3d_gl_debug.log")

def _log(msg):
    try:
        with open(_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass

# ── Shaders ──────────────────────────────────────────────────────────────

VERTEX_SHADER = """
attribute highp vec3 a_position;
attribute highp vec3 a_normal;
attribute highp vec2 a_texCoord;
uniform highp mat4 u_mvpMatrix;
uniform highp mat4 u_normalMatrix;
varying highp vec3 v_normal;
varying highp vec2 v_texCoord;
void main() {
    v_normal = normalize((u_normalMatrix * vec4(a_normal, 0.0)).xyz);
    v_texCoord = a_texCoord;
    gl_Position = u_mvpMatrix * vec4(a_position, 1.0);
}
"""

FRAGMENT_SHADER = """
varying highp vec3 v_normal;
varying highp vec2 v_texCoord;
uniform highp vec3 u_lightDir;
uniform highp vec3 u_color;
uniform bool u_isWireframe;
uniform sampler2D u_texture;
uniform bool u_useTexture;

void main() {
    if (u_isWireframe) {
        gl_FragColor = vec4(1.0, 1.0, 1.0, 1.0);
    } else {
        highp vec3 n = normalize(v_normal);
        highp float diff = max(dot(n, u_lightDir), 0.0);
        highp vec3 ambient = u_color * 0.3;
        highp vec3 diffuse = u_color * diff * 0.7;
        
        if (u_useTexture) {
            highp vec4 tex = texture2D(u_texture, v_texCoord);
            gl_FragColor = vec4(tex.rgb * (diff * 0.7 + 0.3), tex.a);
        } else {
            gl_FragColor = vec4(ambient + diffuse, 1.0);
        }
    }
}
"""

# ═════════════════════════════════════════════════════════════════════════
#  Offscreen renderer
# ═════════════════════════════════════════════════════════════════════════

class OffscreenGLRenderer:
    """Hardware-accelerated offscreen OpenGL renderer."""

    GL_DEPTH_TEST        = 0x0B71
    GL_COLOR_BUFFER_BIT  = 0x00004000
    GL_DEPTH_BUFFER_BIT  = 0x00000100
    GL_LINES             = 0x0001
    GL_LINE_LOOP         = 0x0002
    GL_TRIANGLES         = 0x0004
    GL_FLOAT             = 0x1406
    GL_FALSE             = 0
    GL_TEXTURE_2D        = 0x0DE1
    GL_RGBA              = 0x1908
    GL_UNSIGNED_BYTE     = 0x1401
    GL_TEXTURE_MIN_FILTER = 0x2801
    GL_TEXTURE_MAG_FILTER = 0x2800
    GL_LINEAR            = 0x2601

    def __init__(self, width=400, height=300):
        self._width = max(width, 1)
        self._height = max(height, 1)
        self._initialized = False

        self.model = None

        # Buffers
        self._vert_buf   = None
        self._norm_buf   = None
        self._uv_buf     = None
        self._wire_buf   = None
        self._vert_count = 0
        self._wire_count = 0

        # Placeholder
        self._ph_verts = (ctypes.c_float * 9)(0,1,0, -1,-1,0, 1,-1,0)
        self._ph_norms = (ctypes.c_float * 9)(0,0,1, 0,0,1, 0,0,1)

        self.shader = None
        self.view_mode = "Wireframe"
        self.texture_id = None
        self._texture_valid = False

        # Camera
        self.rotation_x = 180.0
        self.rotation_y = 180.0
        self.rotation_z = 180.0
        self.model_base_matrix = QMatrix4x4()
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = -5.0
        self.fov = 45.0
        self.camera_mode = "Orbit"
        self.import_rotation = (0, 0, 0)  # User-configurable import axis rotation (degrees)
        self.invert_y = False
        
        # Gizmo — drawn in OpenGL coords but representing model-space axes
        # Model X (forward) is OpenGL Z => (0,0,1)
        # Model Y (right)   is OpenGL X => (1,0,0)
        # Model Z (up)      is OpenGL Y => (0,1,0)
        self._gizmo_verts = (ctypes.c_float * 18)(
            0,0,0, 0,0,1,   # Model X axis (Red)  -> GL Z
            0,0,0, 1,0,0,   # Model Y axis (Green)-> GL X
            0,0,0, 0,1,0    # Model Z axis (Blue) -> GL Y
        )
        self._gizmo_colors = (ctypes.c_float * 18)(
            1,0,0, 1,0,0,   # Red   (X)
            0,1,0, 0,1,0,   # Green (Y)
            0,0,1, 0,0,1    # Blue  (Z)
        )
        self.gizmo_shader = None

        self._context = None
        self._surface = None
        self._fbo = None

        self._setup()

    def _setup(self):
        if QOffscreenSurface is None:
            _log("QOffscreenSurface is missing from PyQt5")
            return
        try:
            import sys
            _lib = None
            if sys.platform == "win32":
                try: 
                    _lib = ctypes.windll.libGLESv2
                    _log("Loaded libGLESv2")
                except OSError: 
                    _lib = ctypes.windll.opengl32
                    _log("Loaded opengl32")
            else:
                lpath = ctypes.util.find_library("GL")
                if lpath:
                    _lib = ctypes.CDLL(lpath)
                    _log(f"Loaded GL library at {lpath}")
            
            if _lib is None:
                _log("Failed to load any OpenGL library")
                return
                
            self._lib = _lib

            def _bind(name, restype, *argtypes):
                func = getattr(_lib, name)
                func.restype = restype
                func.argtypes = argtypes
                return func

            self._glEnable = _bind("glEnable", None, ctypes.c_uint)
            self._glClearColor = _bind("glClearColor", None, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)
            self._glViewport = _bind("glViewport", None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
            self._glClear = _bind("glClear", None, ctypes.c_uint)
            self._glVertexAttribPointer = _bind("glVertexAttribPointer", None, ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
            self._glEnableVAA = _bind("glEnableVertexAttribArray", None, ctypes.c_uint)
            self._glDisableVAA = _bind("glDisableVertexAttribArray", None, ctypes.c_uint)
            self._glDrawArrays = _bind("glDrawArrays", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)
            self._glFlush = _bind("glFlush", None)
            
            self._glGenTextures = _bind("glGenTextures", None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))
            self._glBindTexture = _bind("glBindTexture", None, ctypes.c_uint, ctypes.c_uint)
            self._glTexParameteri = _bind("glTexParameteri", None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
            self._glTexImage2D = _bind("glTexImage2D", None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)

            fmt = QSurfaceFormat()
            fmt.setDepthBufferSize(24)
            fmt.setStencilBufferSize(8)
            self._context = QOpenGLContext()
            shared = QOpenGLContext.globalShareContext()
            if shared: self._context.setShareContext(shared)
            self._context.setFormat(fmt)
            if not self._context.create():
                _log("Failed to create QOpenGLContext")
                return

            self._surface = QOffscreenSurface()
            self._surface.setFormat(self._context.format())
            self._surface.create()
            if not self._surface.isValid():
                _log("Offscreen surface is invalid")
                return

            self._context.makeCurrent(self._surface)
            self._create_fbo()
            self._init_gl()
            self._context.doneCurrent()
            self._initialized = True
            _log("Offscreen GL Setup complete")
        except: 
            _log("Setup FAILED:\n" + traceback.format_exc())

    def _create_fbo(self):
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        fmt.setSamples(4)
        self._fbo = QOpenGLFramebufferObject(QSize(self._width, self._height), fmt)

    def _init_gl(self):
        self._glClearColor(0.0, 0.0, 0.0, 0.0)
        self._glEnable(self.GL_DEPTH_TEST)
        self.shader = QOpenGLShaderProgram()
        self.shader.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX_SHADER)
        self.shader.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT_SHADER)
        self.shader.link()
        
        # Shader for simple colored lines (gizmo)
        self.gizmo_shader = QOpenGLShaderProgram()
        v_src = """
        attribute highp vec3 a_position;
        attribute highp vec3 a_color;
        uniform highp mat4 u_mvpMatrix;
        varying highp vec3 v_color;
        void main() {
            v_color = a_color;
            gl_Position = u_mvpMatrix * vec4(a_position, 1.0);
        }
        """
        f_src = """
        varying highp vec3 v_color;
        void main() {
            gl_FragColor = vec4(v_color, 1.0);
        }
        """
        self.gizmo_shader.addShaderFromSourceCode(QOpenGLShader.Vertex, v_src)
        self.gizmo_shader.addShaderFromSourceCode(QOpenGLShader.Fragment, f_src)
        self.gizmo_shader.link()

    def set_model(self, model):
        self.model = model
        self._vert_buf = self._norm_buf = self._uv_buf = self._wire_buf = None
        self._vert_count = self._wire_count = 0
        self._unload_texture()
        if not model or not model.faces: return

        solid_v, solid_n, solid_uv, wire_v = [], [], [], []
        has_n = bool(model.normals)
        has_uv = bool(model.texcoords)

        for face in model.faces:
            if len(face) >= 3:
                fn = self._face_normal(face) or [0,0,1]
                v0 = face[0]
                for i in range(1, len(face)-1):
                    v1, v2 = face[i], face[i+1]
                    for (vi, ti, ni) in (v0, v1, v2):
                        p = model.vertices[vi-1] if 0 < vi <= len(model.vertices) else [0,0,0]
                        # Swizzle (x,y,z) -> (y,z,x) for Z-up to Y-up conversion
                        solid_v.extend([float(p[1]), float(p[2]), float(p[0])])
                        if has_n and 0 < ni <= len(model.normals):
                            n = model.normals[ni-1]
                            solid_n.extend([float(n[1]), float(n[2]), float(n[0])])
                        else: solid_n.extend([float(fn[1]), float(fn[2]), float(fn[0])])
                        if has_uv and 0 < ti <= len(model.texcoords):
                            uv = model.texcoords[ti-1]
                            solid_uv.extend([float(uv[0]), float(uv[1])])
                        else: solid_uv.extend([0.0, 0.0])
            for i in range(len(face)):
                v1, v2 = face[i], face[(i+1)%len(face)]
                for vi in (v1[0], v2[0]):
                    p = model.vertices[vi-1] if 0 < vi <= len(model.vertices) else [0,0,0]
                    # Swizzle (x,y,z) -> (y,z,x) for Z-up to Y-up conversion
                    wire_v.extend([float(p[1]), float(p[2]), float(p[0])])

        n = len(solid_v) // 3
        self._vert_buf = (ctypes.c_float * len(solid_v))(*solid_v)
        self._norm_buf = (ctypes.c_float * len(solid_n))(*solid_n)
        self._uv_buf = (ctypes.c_float * len(solid_uv))(*solid_uv)
        self._vert_count = n
        w = len(wire_v) // 3
        self._wire_buf = (ctypes.c_float * len(wire_v))(*wire_v)
        self._wire_count = w

        if model.texture_path and self._initialized: 
            self._load_texture(model.texture_path)

        # Apply import rotation to all vertex/normal data after swizzle
        ix, iy, iz = getattr(self, 'import_rotation', (0, 0, 0))
        if ix != 0 or iy != 0 or iz != 0:
            # Remap from user Z-up model space to OpenGL Y-up space:
            # User X (forward) -> OpenGL Z,  User Y (right) -> OpenGL X,  User Z (up) -> OpenGL Y
            self._apply_import_rotation(iy, iz, ix)

    def _apply_import_rotation(self, rx_deg, ry_deg, rz_deg):
        """Rotate all vertex and normal buffers by the given angles (degrees).
        Applied after the Z-up swizzle, in OpenGL Y-up space."""
        import math
        # Build rotation matrix manually (simple 3x3 for each axis)
        def _rot_x(v, a):
            c, s = math.cos(a), math.sin(a)
            return (v[0], v[1]*c - v[2]*s, v[1]*s + v[2]*c)
        def _rot_y(v, a):
            c, s = math.cos(a), math.sin(a)
            return (v[0]*c + v[2]*s, v[1], -v[0]*s + v[2]*c)
        def _rot_z(v, a):
            c, s = math.cos(a), math.sin(a)
            return (v[0]*c - v[1]*s, v[0]*s + v[1]*c, v[2])
        
        ax = math.radians(rx_deg)
        ay = math.radians(ry_deg)
        az = math.radians(rz_deg)
        
        def rotate_point(p):
            p = _rot_z(p, az)
            p = _rot_y(p, ay)
            p = _rot_x(p, ax)
            return p
        
        # Rotate vertex buffers
        for buf, stride in [(self._vert_buf, 3), (self._norm_buf, 3), (self._wire_buf, 3)]:
            if buf is None:
                continue
            count = len(buf) // stride
            for i in range(count):
                x, y, z = buf[i*3], buf[i*3+1], buf[i*3+2]
                nx, ny, nz = rotate_point((x, y, z))
                buf[i*3]   = nx
                buf[i*3+1] = ny
                buf[i*3+2] = nz

    def _load_texture(self, path):
        if not self._initialized or self._context is None: return
        img = QImage(path)
        if img.isNull(): return
        img = img.convertToFormat(QImage.Format_RGBA8888).mirrored(False, True)
        w, h = img.width(), img.height()
        ptr = img.constBits()
        try: ptr.setsize(img.byteCount())
        except: ptr.setsize(img.sizeInBytes())
        
        try:
            prev_ctx = QOpenGLContext.currentContext()
            prev_surf = prev_ctx.surface() if prev_ctx else None
            if prev_ctx and prev_ctx != self._context: prev_ctx.doneCurrent()
            
            if self._context.makeCurrent(self._surface):
                tid = ctypes.c_uint(0)
                self._glGenTextures(1, ctypes.byref(tid))
                self.texture_id = tid.value
                self._glBindTexture(self.GL_TEXTURE_2D, self.texture_id)
                self._glTexParameteri(self.GL_TEXTURE_2D, self.GL_TEXTURE_MIN_FILTER, self.GL_LINEAR)
                self._glTexParameteri(self.GL_TEXTURE_2D, self.GL_TEXTURE_MAG_FILTER, self.GL_LINEAR)
                self._glTexImage2D(self.GL_TEXTURE_2D, 0, self.GL_RGBA, w, h, 0, self.GL_RGBA, self.GL_UNSIGNED_BYTE, ctypes.c_void_p(int(ptr)))
                self._texture_valid = True
                self._context.doneCurrent()
                
            if prev_ctx and prev_surf: prev_ctx.makeCurrent(prev_surf)
        except:
            _log("Texture loading crashed:\n" + traceback.format_exc())

    def _unload_texture(self):
        self.texture_id = None
        self._texture_valid = False

    def resize(self, w, h, dpr=1.0):
        rw, rh = max(1, int(w*dpr)), max(1, int(h*dpr))
        if rw == self._width and rh == self._height: return
        self._width, self._height = rw, rh
        if self._initialized:
            prev_ctx = QOpenGLContext.currentContext()
            prev_surf = prev_ctx.surface() if prev_ctx else None
            if prev_ctx and prev_ctx != self._context: prev_ctx.doneCurrent()
            
            if self._context.makeCurrent(self._surface):
                self._fbo = None
                self._create_fbo()
                self._context.doneCurrent()
                
            if prev_ctx and prev_surf: prev_ctx.makeCurrent(prev_surf)

    def render(self):
        return self.render_at_size(self._width, self._height)

    def render_at_size(self, width, height, render_gizmo=True):
        if not self._initialized: return QImage()
        
        # Determine if we need a temporary FBO for this size
        temp_fbo = None
        use_fbo = self._fbo
        needs_resize = (width != self._width or height != self._height)
        
        try:
            prev_ctx = QOpenGLContext.currentContext()
            prev_surf = prev_ctx.surface() if prev_ctx else None
            if prev_ctx and prev_ctx != self._context: prev_ctx.doneCurrent()
            if not self._context.makeCurrent(self._surface):
                if prev_ctx and prev_surf: prev_ctx.makeCurrent(prev_surf)
                return QImage()

            # Create temporary FBO if rendering at a different size than the widget
            if needs_resize:
                fmt = QOpenGLFramebufferObjectFormat()
                fmt.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
                fmt.setSamples(4)
                temp_fbo = QOpenGLFramebufferObject(QSize(width, height), fmt)
                use_fbo = temp_fbo
                
            if not use_fbo:
                self._context.doneCurrent()
                if prev_ctx and prev_surf: prev_ctx.makeCurrent(prev_surf)
                return QImage()

            use_fbo.bind()
            self._glViewport(0, 0, width, height)
            self._glClear(self.GL_COLOR_BUFFER_BIT | self.GL_DEPTH_BUFFER_BIT)
            
            if self.shader:
                self.shader.bind()
                proj = QMatrix4x4()
                proj.perspective(self.fov, width/max(1, height), 0.01, 1000.0)
                view = QMatrix4x4()
                if getattr(self, 'camera_mode', 'Orbit') == "Walk":
                    # First-Person camera: apply rotations, then translate in World space, then apply model base
                    view.rotate(self.rotation_y - 180.0, 1, 0, 0) # Pitch (Model Y)
                    view.rotate(self.rotation_z - 180.0, 0, 1, 0) # Yaw (Model Z)
                    view.rotate(self.rotation_x - 180.0, 0, 0, 1) # Roll (Model X)
                    view.translate(-self.pan_x, -self.pan_y, self.zoom)
                    view = view * self.model_base_matrix
                else:                     
                    view.translate(self.pan_x, self.pan_y, self.zoom)
                    view.rotate(self.rotation_y - 180.0, 1, 0, 0) # Pitch (Model Y)
                    view.rotate(self.rotation_z - 180.0, 0, 1, 0) # Yaw (Model Z)
                    view.rotate(self.rotation_x - 180.0, 0, 0, 1) # Roll (Model X)
                    view = view * self.model_base_matrix

                self.shader.setUniformValue("u_mvpMatrix", proj * view)
                self.shader.setUniformValue("u_normalMatrix", view)
                self.shader.setUniformValue("u_lightDir", QVector3D(0.5, 0.7, 1.0).normalized())

                is_wire = (self.view_mode == "Wireframe")
                self.shader.setUniformValue("u_isWireframe", is_wire)
                use_tex = (self.view_mode == "Textured" and self._texture_valid)
                self.shader.setUniformValue("u_useTexture", use_tex)
                if use_tex:
                    self._glBindTexture(self.GL_TEXTURE_2D, self.texture_id)
                    self.shader.setUniformValue("u_texture", 0)

                pos_loc = self.shader.attributeLocation("a_position")
                norm_loc = self.shader.attributeLocation("a_normal")
                uv_loc = self.shader.attributeLocation("a_texCoord")

                if self._vert_count > 0:
                    self._draw_model(is_wire, pos_loc, norm_loc, uv_loc)
                else:
                    self._draw_placeholder(is_wire, pos_loc, norm_loc)

                self.shader.release()
                
            # Draw Gizmo Overlay
            if render_gizmo and self.gizmo_shader:
                self._glClear(self.GL_DEPTH_BUFFER_BIT) # Clear depth for overlay
                
                self.gizmo_shader.bind()
                gizmo_proj = QMatrix4x4()
                
                # Setup orthogonal projection for the bottom-left corner
                # We want the gizmo to always be in the corner, regardless of resolution
                gizmo_size = 80.0
                margin = 20.0
                gizmo_proj.ortho(
                    -margin, width - margin,                  # left, right
                    -margin, height - margin,                 # bottom, top
                    -100.0, 100.0                             # near, far
                )
                
                gizmo_view = QMatrix4x4()
                gizmo_view.translate(gizmo_size/2, gizmo_size/2, 0)
                gizmo_view.scale(gizmo_size/2, gizmo_size/2, gizmo_size/2)
                
                # Apply interactive rotations only for gizmo
                # This ensures the gizmo shows the "view" relative to the current basis,
                # meaning it resets to neutral when the sliders reset to 180.
                gizmo_view.rotate(self.rotation_y - 180.0, 1, 0, 0)
                gizmo_view.rotate(self.rotation_z - 180.0, 0, 1, 0)
                gizmo_view.rotate(self.rotation_x - 180.0, 0, 0, 1)
                
                self.gizmo_shader.setUniformValue("u_mvpMatrix", gizmo_proj * gizmo_view)
                
                p_loc = self.gizmo_shader.attributeLocation("a_position")
                c_loc = self.gizmo_shader.attributeLocation("a_color")
                
                self._draw_buf(p_loc, self._gizmo_verts, 3)
                self._draw_buf(c_loc, self._gizmo_colors, 3)
                self._glDrawArrays(self.GL_LINES, 0, 6)
                
                self._glDisableVAA(p_loc)
                self._glDisableVAA(c_loc)
                self.gizmo_shader.release()

                # Calculate label positions for X, Y, Z
                m = gizmo_proj * gizmo_view
                def project_to_screen(v3d):
                    p = m * v3d
                    return (
                        (p.x() + 1.0) * width / 2.0,
                        (1.0 - p.y()) * height / 2.0
                    )
                
                # Labels at gizmo tips — model axes in GL coords
                label_pos = {
                    "X": project_to_screen(QVector3D(0, 0, 1.1)),   # Model X -> GL Z
                    "Y": project_to_screen(QVector3D(1.1, 0, 0)),   # Model Y -> GL X
                    "Z": project_to_screen(QVector3D(0, 1.1, 0))    # Model Z -> GL Y
                }

            self._glFlush()
            use_fbo.release()
            img = use_fbo.toImage()
            
            # Draw Text Labels on the QImage
            if render_gizmo and self.gizmo_shader:
                painter = QPainter(img)
                painter.setRenderHint(QPainter.Antialiasing)
                font = painter.font()
                font.setBold(True)
                font.setPointSize(10)
                painter.setFont(font)
                
                # Colors matching the axes
                colors = {"X": QColor(255, 50, 50), "Y": QColor(50, 255, 50), "Z": QColor(50, 150, 255)}
                
                for label, pos in label_pos.items():
                    px, py = pos
                    painter.setPen(colors[label])
                    painter.drawText(int(px - 5), int(py + 5), label)
                painter.end()
            
            if temp_fbo:
                del temp_fbo
                
            self._context.doneCurrent()
            if prev_ctx and prev_surf: prev_ctx.makeCurrent(prev_surf)
            return img
        except:
            _log(traceback.format_exc())
            return QImage()

    def _draw_buf(self, loc, buf, size=3):
        if loc < 0 or buf is None: return
        ptr = ctypes.cast(buf, ctypes.c_void_p)
        self._glVertexAttribPointer(loc, size, self.GL_FLOAT, self.GL_FALSE, 0, ptr.value)
        self._glEnableVAA(loc)

    def _draw_model(self, is_wire, p_loc, n_loc, u_loc=-1):
        if is_wire and self._wire_buf:
            self.shader.setUniformValue("u_color", QVector3D(1, 1, 1))
            self._draw_buf(p_loc, self._wire_buf, 3)
            self._glDrawArrays(self.GL_LINES, 0, self._wire_count)
            self._glDisableVAA(p_loc)
        elif not is_wire and self._vert_buf:
            self.shader.setUniformValue("u_color", QVector3D(0.75, 0.75, 0.78))
            self._draw_buf(p_loc, self._vert_buf, 3)
            self._draw_buf(n_loc, self._norm_buf, 3)
            if u_loc >= 0: self._draw_buf(u_loc, self._uv_buf, 2)
            self._glDrawArrays(self.GL_TRIANGLES, 0, self._vert_count)
            self._glDisableVAA(p_loc)
            self._glDisableVAA(n_loc)
            if u_loc >= 0: self._glDisableVAA(u_loc)

    def _draw_placeholder(self, is_wire, p_loc, n_loc):
        self.shader.setUniformValue("u_useTexture", False)
        self.shader.setUniformValue("u_color", QVector3D(0.5, 0.5, 1))
        self._draw_buf(p_loc, self._ph_verts, 3)
        self._draw_buf(n_loc, self._ph_norms, 3)
        self._glDrawArrays(self.GL_TRIANGLES, 0, 3)
        self._glDisableVAA(p_loc)
        self._glDisableVAA(n_loc)

    def _face_normal(self, face):
        if not self.model: return None
        v = self.model.vertices
        try:
            p0, p1, p2 = v[face[0][0]-1], v[face[1][0]-1], v[face[2][0]-1]
            e1 = [p1[i]-p0[i] for i in range(3)]
            e2 = [p2[i]-p0[i] for i in range(3)]
            n = [e1[1]*e2[2]-e1[2]*e2[1], e1[2]*e2[0]-e1[0]*e2[2], e1[0]*e2[1]-e1[1]*e2[0]]
            l = math.sqrt(sum(x*x for x in n))
            return [x/l for x in n] if l > 0 else [0,0,1]
        except: return [0,0,1]

class GLRendererWidget(QWidget):
    renderComplete = pyqtSignal()
    rotationChanged = pyqtSignal(float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._renderer = OffscreenGLRenderer()
        self._current_image = None
        self._last_mouse_pos = None
        self._mouse_button = None
        self._render_timer = None
        self.setMinimumSize(100, 100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def _get_timer(self):
        if self._render_timer is None:
            from PyQt5.QtCore import QTimer
            self._render_timer = QTimer(self)
            self._render_timer.setSingleShot(True)
            self._render_timer.timeout.connect(self._do_refresh)
        return self._render_timer

    def _schedule_refresh(self): self._get_timer().start(16)

    def _do_refresh(self):
        w, h = self.width(), self.height()
        if w < 1 or h < 1: return
        try: dpr = self.devicePixelRatioF()
        except: dpr = 1.0
        self._renderer.resize(w, h, dpr)
        self._current_image = self._renderer.render()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(51, 51, 51))
        if self._current_image and not self._current_image.isNull():
            p.drawImage(self.rect(), self._current_image)
        else:
            p.setPen(QColor(128, 128, 128))
            p.drawText(self.rect(), Qt.AlignCenter, "3D Viewport\n(Initialising…)")
        p.end()

    def resizeEvent(self, event): super().resizeEvent(event); self._schedule_refresh()
    def showEvent(self, event): super().showEvent(event); self._schedule_refresh()

    def set_model(self, model): self._renderer.set_model(model); self._schedule_refresh()
    def set_view_mode(self, mode): self._renderer.view_mode = mode; self._schedule_refresh(); self.renderComplete.emit()
    def set_fov(self, val): 
        import math
        new_fov = max(10, min(120, float(val)))
        old_fov = self._renderer.fov
        if old_fov != new_fov:
            old_rad = math.radians(old_fov / 2.0)
            new_rad = math.radians(new_fov / 2.0)
            self._renderer.zoom = self._renderer.zoom * (math.tan(old_rad) / math.tan(new_rad))
            self._renderer.fov = new_fov
        self._schedule_refresh()
        self.renderComplete.emit()

    def set_zoom(self, val):
        self._renderer.zoom = max(-50.0, min(-0.01, float(val)))
        self._schedule_refresh()
        self.renderComplete.emit()
        
    def reset_view(self):
        r = self._renderer
        r.rotation_x, r.rotation_y, r.rotation_z = 180.0, 180.0, 180.0
        r.model_base_matrix = QMatrix4x4()
        r.pan_x, r.pan_y, r.zoom, r.fov = 0, 0, -5, 45
        self._schedule_refresh()
        self.rotationChanged.emit(180.0, 180.0, 180.0)

    def convert_camera_params(self, from_mode, to_mode):
        """Convert pan_x/pan_y/zoom so the view stays the same when switching modes.
        
        Orbit view: T_o * R * M
        Walk view:  R * T_w * M
        
        Orbit->Walk: v = R^T * t_o
        Walk->Orbit: v = R * t_w
        """
        r = self._renderer
        if not r: return
        
        q = QMatrix4x4()
        q.rotate(r.rotation_y - 180.0, 1, 0, 0)
        q.rotate(r.rotation_z - 180.0, 0, 1, 0)
        q.rotate(r.rotation_x - 180.0, 0, 0, 1)
        q = q * r.model_base_matrix
        
        if from_mode == "Orbit" and to_mode == "Walk":
            # v = Q^T * (pan_x, pan_y, zoom)
            t = QVector3D(r.pan_x, r.pan_y, r.zoom)
            qt = q.transposed()
            v = qt.map(t)
            r.pan_x = -v.x()
            r.pan_y = -v.y()
            r.zoom = v.z()
        elif from_mode == "Walk" and to_mode == "Orbit":
            # v = Q * (-pan_x, -pan_y, zoom)
            t = QVector3D(-r.pan_x, -r.pan_y, r.zoom)
            v = q.map(t)
            r.pan_x = v.x()
            r.pan_y = v.y()
            r.zoom = v.z()

    def set_rotation_as_offset(self, base=180.0):
        """Bake the current interactive rotation into the model base matrix."""
        r = self._renderer
        
        # Create a rotation matrix from current INTERACTIVE rotation
        rot_mat = QMatrix4x4()
        rot_mat.rotate(r.rotation_y - base, 1, 0, 0)
        rot_mat.rotate(r.rotation_z - base, 0, 1, 0)
        rot_mat.rotate(r.rotation_x - base, 0, 0, 1)
        
        # Accumulate into model base matrix
        # Note: Order matters. We want the new rotation to be the new Front.
        r.model_base_matrix = rot_mat * r.model_base_matrix
        
        # Reset interactive angles to the base (180.0)
        r.rotation_x, r.rotation_y, r.rotation_z = base, base, base
        self._schedule_refresh()
        self.rotationChanged.emit(base, base, base)
        self.renderComplete.emit()

    def grabFramebuffer(self):
        if self._current_image and not self._current_image.isNull(): return self._current_image.copy()
        return self._renderer.render()

    def mousePressEvent(self, event):
        self._last_mouse_pos = event.pos()
        self._mouse_button = event.button()

    def mouseMoveEvent(self, event):
        if self._last_mouse_pos is None: return
        dx = event.pos().x() - self._last_mouse_pos.x()
        dy = event.pos().y() - self._last_mouse_pos.y()
        self._last_mouse_pos = event.pos()
        r = self._renderer
        btns = event.buttons()
        l, r_btn, m = bool(btns & Qt.LeftButton), bool(btns & Qt.RightButton), bool(btns & Qt.MiddleButton)
        
        if getattr(r, 'camera_mode', 'Orbit') == "Walk":
            if l: # Look around
                invert = -1.0 if getattr(r, 'invert_y', False) else 1.0
                r.rotation_z += dx * 0.2  # Yaw: mouse right = look right (Model Z)
                r.rotation_y += dy * 0.2 * invert # Pitch (Model Y)
                # Clamp Pitch to avoid flipping over
                r.rotation_y = max(90.1, min(269.9, r.rotation_y))
            elif m or r_btn: # Strafe/Pan
                # pan_x/pan_y/zoom are in WORLD space (applied before rotation),
                # so we must use right/up vectors to convert camera-local movement.
                rmat = QMatrix4x4()
                rmat.rotate(r.rotation_y - 180.0, 1, 0, 0)
                rmat.rotate(r.rotation_z - 180.0, 0, 1, 0)
                rmat.rotate(r.rotation_x - 180.0, 0, 0, 1)
                # Translation is decoupled from model base matrix
                
                right = rmat.row(0).toVector3D()
                up = rmat.row(1).toVector3D()
                
                invert = -1.0 if getattr(r, 'invert_y', False) else 1.0
                multiplier = 0.2 if event.modifiers() & Qt.ShiftModifier else 1.0
                speed = 0.01 * min(20.0, max(0.5, abs(r.zoom))) * multiplier
                
                dy_adj = dy * invert
                r.pan_x += (right.x() * dx - up.x() * dy_adj) * speed
                r.pan_y += (right.y() * dx - up.y() * dy_adj) * speed
                r.zoom -= (right.z() * dx - up.z() * dy_adj) * speed
        else: # Orbit mode
            if m or (l and event.modifiers() & Qt.ShiftModifier):
                r.pan_x += dx * 0.01; r.pan_y -= dy * 0.01
            elif l and r_btn: r.rotation_x -= dx * 0.5   # Both = Roll (Model X) [REVERSED]
            elif l: r.rotation_z += dx * 0.5             # LMB = Yaw (Model Z) [REVERSED]
            elif r_btn: r.rotation_y += dy * 0.5         # RMB = Pitch (Model Y)
            
            r.rotation_x %= 360.0
            r.rotation_y %= 360.0
            r.rotation_z %= 360.0
            
        # Only emit rotation UI updates if we are in Orbit mode
        if getattr(r, 'camera_mode', 'Orbit') != "Walk":
            self.rotationChanged.emit(r.rotation_x, r.rotation_y, r.rotation_z)
        self._schedule_refresh()

    def mouseReleaseEvent(self, event): self._last_mouse_pos = self._mouse_button = None; self.renderComplete.emit()
    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        r = self._renderer
        if getattr(r, 'camera_mode', 'Orbit') == "Walk":
            # Derive forward vector from the rotation matrix
            rmat = QMatrix4x4()
            rmat.rotate(r.rotation_y - 180.0, 1, 0, 0)
            rmat.rotate(r.rotation_z - 180.0, 0, 1, 0)
            rmat.rotate(r.rotation_x - 180.0, 0, 0, 1)
            # Translation decoupled from model base matrix
            fwd = -rmat.row(2).toVector3D()
            
            multiplier = 0.2 if event.modifiers() & Qt.ShiftModifier else 1.0
            speed = 0.5 * min(20.0, max(0.1, abs(r.zoom))) * multiplier
            
            r.pan_x += fwd.x() * delta * speed
            r.pan_y += fwd.y() * delta * speed
            r.zoom -= fwd.z() * delta * speed
        else:
            # Reduced sensitivity from 0.5 to 0.1 for finer control
            r.zoom = max(-50.0, min(-0.01, r.zoom + delta * 0.1))
            
        self._schedule_refresh(); self.renderComplete.emit()
