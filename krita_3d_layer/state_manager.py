import json
import os
import traceback
from PyQt5.QtCore import QByteArray, QTimer
from PyQt5.QtGui import QMatrix4x4

try:
    from krita import Krita
except ImportError:
    pass # For testing outside krita


class StateManager:
    """
    Manages embedding 3D layer states (model paths, camera rotation, etc.)
    into the Krita Document's annotation registry.
    """
    ANNOTATION_KEY = "krita_3d_layer_data"
    
    def __init__(self, docker_ui):
        self.docker = docker_ui
        self.enabled = True
        self.model_cache = {}    # dict[filepath: MODEL_INSTANCE]
        self.use_cache = True    # Controlled by docker UI toggle
        self._last_uid = None
        
        # Poll for layer changes since Krita's Notifier doesn't support activeNodeChanged reliably
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_node)
        self.poll_timer.start(500) # Poll every 500ms

    def _log_error(self, msg):
        """Internal error logger to avoid circular imports with docker_ui."""
        print(f"[StateManager ERROR] {msg}")
        try:
            app = Krita.instance()
            if app: app.setErrorMessage(f"3D StateManager: {msg}")
        except:
            pass

    def _get_doc(self):
        try:
            return Krita.instance().activeDocument()
        except:
            return None

    def _read_registry(self, doc):
        """Reads the JSON registry from the document's hidden annotations."""
        try:
            byte_arr = doc.annotation(self.ANNOTATION_KEY)
            if byte_arr and not byte_arr.isEmpty():
                # Krita's annotation sometimes returns QByteArray, sometimes str.
                if isinstance(byte_arr, QByteArray):
                    s = str(byte_arr.data(), encoding='utf-8')
                else:
                    s = str(byte_arr)
                return json.loads(s)
        except Exception as e:
            self._log_error(f"Error reading annotation: {e}\n{traceback.format_exc()}")
        return {}

    def _write_registry(self, doc, registry):
        """Writes the JSON registry to the document's hidden annotations."""
        try:
            s = json.dumps(registry)
            data = QByteArray(s.encode('utf-8'))
            doc.setAnnotation(self.ANNOTATION_KEY, "3D Layer Linked States", data)
        except Exception as e:
            self._log_error(f"Error writing annotation: {e}\n{traceback.format_exc()}")

    def is_node_linked(self, node):
        """Returns True if the node has a 3D state in the document registry."""
        if not node:
            return False
        doc = self._get_doc()
        if not doc:
            return False
        registry = self._read_registry(doc)
        return str(node.uniqueId()) in registry

    def save_state_for_current_layer(self, model_path, renderer, node=None):
        """Saves 3D state for a layer. If node is None, uses active layer."""
        if not model_path or not renderer or not self.enabled:
            return
            
        doc = self._get_doc()
        if not doc:
            return
            
        target_node = node if node else doc.activeNode()
        if not target_node:
            return
            
        uid = str(target_node.uniqueId())
        registry = self._read_registry(doc)
        
        # Serialize camera parameters and view settings
        state = {
            "model_path": model_path,
            "rot_x": float(renderer.rotation_x),
            "rot_y": float(renderer.rotation_y),
            "rot_z": float(renderer.rotation_z),
            "pan_x": float(renderer.pan_x),
            "pan_y": float(renderer.pan_y),
            "zoom": float(renderer.zoom),
            "fov": float(renderer.fov),
            "view_mode": renderer.view_mode,
            "camera_mode": getattr(renderer, 'camera_mode', 'Orbit')
        }
        
        # Serialize the baked matrix
        m = renderer.model_base_matrix
        state["matrix"] = [
            m.row(0).x(), m.row(0).y(), m.row(0).z(), m.row(0).w(),
            m.row(1).x(), m.row(1).y(), m.row(1).z(), m.row(1).w(),
            m.row(2).x(), m.row(2).y(), m.row(2).z(), m.row(2).w(),
            m.row(3).x(), m.row(3).y(), m.row(3).z(), m.row(3).w()
        ]
        
        registry[uid] = state
        self._write_registry(doc, registry)

    def _poll_node(self):
        """Check if the active node has changed."""
        if not self.enabled:
            return
            
        doc = self._get_doc()
        if not doc:
            return
            
        node = doc.activeNode()
        if not node:
            return
            
        uid = str(node.uniqueId())
        if uid != self._last_uid:
            self._last_uid = uid
            self._on_node_changed(node)

    def _on_node_changed(self, node):
        """Triggered automatically when the user clicks a different layer in Krita."""
        if not self.enabled or not node:
            return
            
        doc = self._get_doc()
        if not doc:
            return
            
        uid = str(node.uniqueId())
        registry = self._read_registry(doc)
        
        if uid in registry:
            state = registry[uid]
            self._load_state(state)

    def _load_state(self, state):
        """Reconstructs the 3D viewport from the saved state."""
        try:
            path = state.get("model_path")
            if not path or not os.path.exists(path):
                self.docker.status_label.setText(f"Linked 3D model missing: {os.path.basename(path) if path else 'Unknown'}")
                return
            
            # Use cache or load fresh from disk
            model = self._get_model(path)
            if not model:
                return
            
            # Pass the loaded data back to the UI to update sliders and renderer safely
            self.docker._apply_loaded_state(model, path, state)
            
        except Exception as e:
            self._log_error(f"Error applying loaded state: {e}\n{traceback.format_exc()}")

    def _get_model(self, path):
        if self.use_cache and path in self.model_cache:
            return self.model_cache[path]
            
        # Import loaders specifically when needed to avoid circular imports during init
        try:
            from .obj_loader import OBJModel
            from .glb_loader import GLBModel
            
            self.docker.status_label.setText(f"Loading {os.path.basename(path)} from disk...")
            if path.lower().endswith('.glb'):
                m = GLBModel(path)
            else:
                m = OBJModel(path)
                
            if self.use_cache:
                self.model_cache[path] = m
                
            return m
        except Exception as e:
            self._log_error(f"Failed to load model {path}: {e}")
            return None
            
    def clear_cache(self):
        """Drops all models from memory."""
        self.model_cache.clear()
        self.docker.status_label.setText("Model cache cleared from memory.")
