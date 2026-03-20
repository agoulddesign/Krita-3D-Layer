"""
Synchronizes the 3D viewport framebuffer to a Krita paint layer.

Accepts a QImage (from QOpenGLWidget.grabFramebuffer()) and writes it
to a layer named '3D View' in the active document.  No numpy required.
"""

from PyQt5.QtGui import QImage
from krita import Krita


class CanvasSynchronizer:
    """Pushes a QImage onto a Krita paint layer."""

    def __init__(self, layer_name="3D View"):
        self.layer_name = layer_name
        self._is_syncing = False

    def sync_to_canvas(self, renderer_widget, target_node=None):
        """Request a high-res image from the renderer and write it to a layer.
        If target_node is provided, writes to that node. 
        Otherwise, finds or creates the '3D View' layer.

        Returns True on success, False otherwise.
        """
        if self._is_syncing or not renderer_widget:
            return False

        app = Krita.instance()
        doc = app.activeDocument()
        if doc is None:
            return False

        self._is_syncing = True
        try:
            target = target_node if target_node else self._find_or_create_layer(doc)
            if target is None:
                return False

            w, h = doc.width(), doc.height()
            
            # Request High-Res Render from the Widget's renderer
            image = renderer_widget._renderer.render_at_size(w, h, render_gizmo=False)
            if image is None or image.isNull():
                return False

            # QImage.Format_ARGB32 stores bytes as B,G,R,A on
            # little-endian — exactly the BGRA order Krita expects for
            # 8-bit sRGB paint layers.
            converted = image.convertToFormat(QImage.Format_ARGB32)
            w, h = converted.width(), converted.height()

            # Extract raw pixel bytes from the QImage
            ptr = converted.constBits()
            try:
                size = converted.sizeInBytes()
            except AttributeError:
                # Older PyQt5 versions
                size = converted.byteCount()
            ptr.setsize(size)
            byte_data = bytes(ptr)

            target.setPixelData(byte_data, 0, 0, w, h)
            doc.refreshProjection()
            return True
        except Exception as exc:
            print(f"[3D Layer] Sync error: {exc}")
            return False
        finally:
            self._is_syncing = False

    # ──────────────────────────────────────────────────────────────────────

    def _find_or_create_layer(self, doc):
        root = doc.rootNode()
        layer = self._find_layer(root, self.layer_name)
        if layer is None:
            layer = doc.createNode(self.layer_name, "paintLayer")
            root.addChildNode(layer, None)
        return layer

    def bake_layer(self):
        """Rename the current '3D View' layer so it's 'baked'.
        The next sync will create a new '3D View' layer.
        """
        import datetime
        app = Krita.instance()
        doc = app.activeDocument()
        if not doc:
            return False

        root = doc.rootNode()
        layer = self._find_layer(root, self.layer_name)
        if layer:
            timestamp = datetime.datetime.now().strftime("%H%M%S")
            layer.setName(f"3D Baked {timestamp}")
            # Optional: move it down? Krita API for moving is subtle,
            # but changing the name is enough to "detach" it from our sync.
            doc.refreshProjection()
            return True
        return False

    @staticmethod
    def _find_layer(parent, name):
        for child in parent.childNodes():
            if child.name() == name:
                return child
            found = CanvasSynchronizer._find_layer(child, name)
            if found:
                return found
        return None
