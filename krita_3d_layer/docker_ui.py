"""
3D Layer Manager docker panel for Krita.

Provides controls for loading OBJ models, switching view modes, adjusting
FOV, and syncing the 3D viewport to a Krita paint layer.
"""

import os
import tempfile
import traceback

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QSlider, QSizePolicy,
    QTabWidget, QLineEdit, QFileDialog, QProgressBar, QSpinBox, 
    QPlainTextEdit, QCheckBox
)
from PyQt5.QtGui import QImage, QMatrix4x4
from PyQt5.QtCore import QTimer, Qt, QThread, QSize
from krita import DockWidget

LOG_FILE = os.path.join(tempfile.gettempdir(), "krita_3d_error.log")


def _log_error(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class DynamicTabWidget(QTabWidget):
    """A QTabWidget that dynamicly adjusts its height based on the current tab's content."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.currentChanged.connect(self._on_current_changed)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _on_current_changed(self, index):
        # Force recalculation of sizes
        self.updateGeometry()
        if self.parentWidget():
            self.parentWidget().updateGeometry()

    def sizeHint(self):
        # Return standard width, but custom height
        sh = super().sizeHint()
        if self.currentWidget():
            # Height of current page + tab bar + small padding
            # This is the secret sauce for a shrinking tab widget
            h = self.currentWidget().sizeHint().height() + self.tabBar().sizeHint().height() + 10
            return QSize(sh.width(), h)
        return sh

    def minimumSizeHint(self):
        return self.sizeHint()


class Krita3DLayerDocker(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("3D Layer Manager")

        self.gl_widget = None
        self.synchronizer = None

        # Keep track of the currently loaded model path for state manager
        self.current_model_path = None
        from .state_manager import StateManager
        self.state_manager = StateManager(self)

        # Debounce timer — prevents rapid-fire syncs while the user drags
        self._sync_timer = QTimer()
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(200)
        self._sync_timer.timeout.connect(self._do_sync)

        try:
            self._build_ui()
            self._load_settings()
        except Exception:
            _log_error("Error initializing Krita3DLayerDocker:\n"
                       + traceback.format_exc())
            fallback = QLabel("3D Layer failed to initialise.\n"
                              "See krita_3d_error.txt for details.")
            self.setWidget(fallback)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self):
        main = QWidget(self)
        self.setWidget(main)
        # The layout holding everything
        layout = QVBoxLayout(main)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        
        # We wrap the tabs in another layout with a stretch so it stays at the top
        # and doesn't center itself vertically
        top_wrapper = QVBoxLayout()
        
        self.tabs = DynamicTabWidget()
        top_wrapper.addWidget(self.tabs)
        layout.addLayout(top_wrapper)

        # ── Unified Options / Log Viewer ──────────────────────────────────
        self.btn_toggle_opts = QPushButton("Options / Log Viewer ▼")
        self.btn_toggle_opts.setStyleSheet("text-align: left; padding: 4px; border: None;")
        self.btn_toggle_opts.setCheckable(True)
        self.btn_toggle_opts.toggled.connect(self._on_toggle_opts)
        layout.addWidget(self.btn_toggle_opts)

        self.opts_widget = QWidget()
        opts_layout = QVBoxLayout(self.opts_widget)
        opts_layout.setContentsMargins(8, 4, 8, 4)
        opts_layout.setSpacing(4)
        
        # Section A: Import Options
        opts_layout.addWidget(QLabel("<b>Import Rotation Offset</b> (applied at load time):"))
        row_import_rot = QHBoxLayout()
        row_import_rot.addWidget(QLabel("X:"))
        self.spin_import_x = QSpinBox()
        self.spin_import_x.setRange(-180, 180)
        self.spin_import_x.setSingleStep(90)
        self.spin_import_x.setValue(0)
        self.spin_import_x.setSuffix("°")
        self.spin_import_x.valueChanged.connect(self._save_settings)
        row_import_rot.addWidget(self.spin_import_x)
        
        row_import_rot.addWidget(QLabel("Y:"))
        self.spin_import_y = QSpinBox()
        self.spin_import_y.setRange(-180, 180)
        self.spin_import_y.setSingleStep(90)
        self.spin_import_y.setValue(0)
        self.spin_import_y.setSuffix("°")
        self.spin_import_y.valueChanged.connect(self._save_settings)
        row_import_rot.addWidget(self.spin_import_y)
        
        row_import_rot.addWidget(QLabel("Z:"))
        self.spin_import_z = QSpinBox()
        self.spin_import_z.setRange(-180, 180)
        self.spin_import_z.setSingleStep(90)
        self.spin_import_z.setValue(0)
        self.spin_import_z.setSuffix("°")
        self.spin_import_z.valueChanged.connect(self._save_settings)
        row_import_rot.addWidget(self.spin_import_z)
        opts_layout.addLayout(row_import_rot)

        # Section B: General Settings (Cache, Timeout, Invert Y)
        row_settings = QHBoxLayout()
        self.chk_cache = QCheckBox("Cache Models")
        self.chk_cache.setChecked(True)
        self.chk_cache.stateChanged.connect(self._on_cache_toggled)
        row_settings.addWidget(self.chk_cache)
        
        self.chk_invert_y = QCheckBox("Invert Y")
        self.chk_invert_y.setToolTip("Invert vertical mouse axis in Walk mode (look/strafe)")
        self.chk_invert_y.setChecked(False)
        self.chk_invert_y.stateChanged.connect(self._on_invert_y_changed)
        row_settings.addWidget(self.chk_invert_y)
        
        row_settings.addStretch()
        row_settings.addWidget(QLabel("Gen Timeout (min):"))
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(1, 20)
        self.spin_timeout.setValue(5)
        self.spin_timeout.valueChanged.connect(self._save_settings)
        row_settings.addWidget(self.spin_timeout)
        opts_layout.addLayout(row_settings)
        
        # Section C: Log Viewer
        self.text_log = QPlainTextEdit()
        self.text_log.setReadOnly(True)
        self.text_log.setMaximumHeight(150)
        self.text_log.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 10pt;")
        opts_layout.addWidget(self.text_log)
        
        self.opts_widget.setVisible(False)
        layout.addWidget(self.opts_widget)


        # --- TAB 1: File (Local Model) ---
        tab_local = QWidget()
        layout_local = QVBoxLayout(tab_local)
        layout_local.setContentsMargins(4, 4, 4, 4)
        layout_local.setSpacing(4)
        
        row_local = QHBoxLayout()
        
        # 1. Load Button
        btn_load = QPushButton("Load")
        btn_load.setToolTip("Load a local OBJ or GLB model")
        btn_load.clicked.connect(self._on_load_model)
        row_local.addWidget(btn_load)
        
        # 2. Model Label (Condensed)
        self.lbl_loaded_model = QLabel("None")
        self.lbl_loaded_model.setMinimumWidth(60)
        self.lbl_loaded_model.setStyleSheet("color: #66ccff; font-weight: bold; font-size: 10px;")
        row_local.addWidget(self.lbl_loaded_model, stretch=1)
        
        # 3. View Style & Camera Mode
        row_local.addWidget(QLabel("View:"))
        self.combo_view = QComboBox()
        self.combo_view.addItems(["Wireframe", "Clay", "Textured"])
        self.combo_view.currentIndexChanged.connect(self._on_view_mode_changed)
        row_local.addWidget(self.combo_view)
        
        row_local.addWidget(QLabel("Cam:"))
        self.combo_camera = QComboBox()
        self.combo_camera.addItems(["Orbit", "Walk"])
        self.combo_camera.currentIndexChanged.connect(self._on_camera_mode_changed)
        row_local.addWidget(self.combo_camera)
        
        layout_local.addLayout(row_local)
        
        layout_local.addLayout(row_local)
        self.tabs.addTab(tab_local, "File")
        
        self.tabs.addTab(tab_local, "File")

        # --- TAB 2: Generate (ComfyUI) ---
        tab_gen = QWidget()
        layout_gen = QVBoxLayout(tab_gen)
        layout_gen.setContentsMargins(4, 4, 4, 4)

        layout_gen.addWidget(QLabel("ComfyUI Server:"))
        self.edit_comfy_url = QLineEdit("http://127.0.0.1:8188")
        self.edit_comfy_url.editingFinished.connect(self._save_settings)
        layout_gen.addWidget(self.edit_comfy_url)

        layout_gen.addWidget(QLabel("Workflow (API JSON):"))
        row_json = QHBoxLayout()
        self.edit_workflow_path = QLineEdit()
        self.edit_workflow_path.setPlaceholderText("Select ComfyUI API JSON...")
        self.edit_workflow_path.editingFinished.connect(self._save_settings)
        row_json.addWidget(self.edit_workflow_path)
        btn_browse_json = QPushButton("...")
        btn_browse_json.setFixedWidth(30)
        btn_browse_json.clicked.connect(self._on_browse_workflow)
        row_json.addWidget(btn_browse_json)
        layout_gen.addLayout(row_json)

        self.btn_generate = QPushButton("Generate 3D Model")
        self.btn_generate.setStyleSheet("background-color: #2d5a27; font-weight: bold; min-height: 30px;")
        self.btn_generate.clicked.connect(self._on_generate_click)
        layout_gen.addWidget(self.btn_generate)

        # NEW: Save Generated Model Button
        self.btn_save_model = QPushButton("Save Generated Model...")
        self.btn_save_model.setEnabled(False)
        self.btn_save_model.clicked.connect(self._on_save_generated_model)
        layout_gen.addWidget(self.btn_save_model)

        self.gen_progress = QProgressBar()
        self.gen_progress.setVisible(False)
        layout_gen.addWidget(self.gen_progress)

        self.tabs.addTab(tab_gen, "Generate")

        self.tabs.addTab(tab_gen, "Generate")

        # ── Common Controls (Viewport & Navigation) ─────────────────────────
        # Move the renderer and rotation controls below the tabs
        
        # Add a stretch here so the tabs stay perfectly at the top and don't expand vertically
        # layout.addStretch() # Actually, we want the viewport to take up space, so we won't add stretch here.
        # Instead, we just let the GLWidget take up the expanding space.
        
        # ── Rotation Controls ─────────────────────────────────────────────
        grid_rot = QVBoxLayout()
        grid_rot.setSpacing(2)
        
        # X Rotation
        row_x = QHBoxLayout()
        self.lbl_x = QLabel("Rot X: 180")
        self.lbl_x.setFixedWidth(60)
        self.lbl_x.setStyleSheet("color: #ff3232; font-weight: bold;")
        row_x.addWidget(self.lbl_x)
        btn_minus_x = QPushButton("-90°")
        btn_minus_x.setFixedWidth(35)
        btn_minus_x.clicked.connect(lambda: self._add_rot(self.sld_x, -90))
        row_x.addWidget(btn_minus_x)
        self.sld_x = QSlider(Qt.Horizontal)
        self.sld_x.setRange(0, 360)
        self.sld_x.setValue(180)
        self.sld_x.valueChanged.connect(self._on_rot_changed)
        row_x.addWidget(self.sld_x)
        btn_plus_x = QPushButton("+90°")
        btn_plus_x.setFixedWidth(35)
        btn_plus_x.clicked.connect(lambda: self._add_rot(self.sld_x, 90))
        row_x.addWidget(btn_plus_x)
        grid_rot.addLayout(row_x)

        # Y Rotation
        row_y = QHBoxLayout()
        self.lbl_y = QLabel("Rot Y: 180")
        self.lbl_y.setFixedWidth(60)
        self.lbl_y.setStyleSheet("color: #32ff32; font-weight: bold;")
        row_y.addWidget(self.lbl_y)
        btn_minus_y = QPushButton("-90°")
        btn_minus_y.setFixedWidth(35)
        btn_minus_y.clicked.connect(lambda: self._add_rot(self.sld_y, -90))
        row_y.addWidget(btn_minus_y)
        self.sld_y = QSlider(Qt.Horizontal)
        self.sld_y.setRange(0, 360)
        self.sld_y.setValue(180)
        self.sld_y.valueChanged.connect(self._on_rot_changed)
        row_y.addWidget(self.sld_y)
        btn_plus_y = QPushButton("+90°")
        btn_plus_y.setFixedWidth(35)
        btn_plus_y.clicked.connect(lambda: self._add_rot(self.sld_y, 90))
        row_y.addWidget(btn_plus_y)
        grid_rot.addLayout(row_y)

        # Z Rotation
        row_z = QHBoxLayout()
        self.lbl_z = QLabel("Rot Z: 180")
        self.lbl_z.setFixedWidth(60)
        self.lbl_z.setStyleSheet("color: #3296ff; font-weight: bold;")
        row_z.addWidget(self.lbl_z)
        btn_minus_z = QPushButton("-90°")
        btn_minus_z.setFixedWidth(35)
        btn_minus_z.clicked.connect(lambda: self._add_rot(self.sld_z, -90))
        row_z.addWidget(btn_minus_z)
        self.sld_z = QSlider(Qt.Horizontal)
        self.sld_z.setRange(0, 360)
        self.sld_z.setValue(180)
        self.sld_z.valueChanged.connect(self._on_rot_changed)
        row_z.addWidget(self.sld_z)
        btn_plus_z = QPushButton("+90°")
        btn_plus_z.setFixedWidth(35)
        btn_plus_z.clicked.connect(lambda: self._add_rot(self.sld_z, 90))
        row_z.addWidget(btn_plus_z)
        grid_rot.addLayout(row_z)

        layout.addLayout(grid_rot)

        # ── FOV slider ───────────────────────────────────────────────────
        row_fov = QHBoxLayout()
        self.fov_label = QLabel("FOV: 45")
        self.fov_label.setFixedWidth(60)
        row_fov.addWidget(self.fov_label)
        self.fov_slider = QSlider(Qt.Horizontal)
        self.fov_slider.setRange(10, 120)
        self.fov_slider.setValue(45)
        self.fov_slider.valueChanged.connect(self._on_fov_changed)
        row_fov.addWidget(self.fov_slider, stretch=1)
        layout.addLayout(row_fov)

        # ── Zoom slider ──────────────────────────────────────────────────
        row_zoom = QHBoxLayout()
        self.zoom_label = QLabel("Zoom: -5.0")
        self.zoom_label.setFixedWidth(60)
        row_zoom.addWidget(self.zoom_label)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(1, 1000) # 1 = -0.01, 1000 = -50.0
        self.zoom_slider.setValue(100) # -5.0 approx
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        row_zoom.addWidget(self.zoom_slider, stretch=1)
        layout.addLayout(row_zoom)

        # ── 3D viewport ─────────────────────────────────────────────────
        from .gl_renderer import GLRendererWidget

        self.gl_widget = GLRendererWidget()
        self.gl_widget.setMinimumHeight(200)
        self.gl_widget.setSizePolicy(QSizePolicy.Expanding,
                                     QSizePolicy.Expanding)
        layout.addWidget(self.gl_widget, stretch=1)

        # ── Navigation hints ─────────────────────────────────────────────
        self.nav_label = QLabel("LMB: Yaw  |  RMB: Pitch  |  LMB+RMB: Roll  |  MMB: Pan  |  (Shift: Precision)")
        self.nav_label.setWordWrap(True)
        self.nav_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.nav_label)

        # ── Action buttons ───────────────────────────────────────────────
        row_actions = QHBoxLayout()

        btn_sync = QPushButton("Sync to Layer")
        btn_sync.setToolTip("Push the current 3D view to a '3D View' "
                            "paint layer on the active document")
        btn_sync.clicked.connect(self._do_sync)
        row_actions.addWidget(btn_sync)

        btn_apply = QPushButton("Apply Layer")
        btn_apply.setToolTip("Bake the current 3D view and start fresh on a new layer")
        btn_apply.clicked.connect(self._on_apply_layer)
        row_actions.addWidget(btn_apply)

        self.btn_set_axis = QPushButton("Set Axis")
        self.btn_set_axis.setToolTip("Define current view as Front orientation without moving model")
        self.btn_set_axis.clicked.connect(self._on_set_axis)
        row_actions.addWidget(self.btn_set_axis)
        
        btn_clear_axis = QPushButton("Clear Axis")
        btn_clear_axis.setToolTip("Remove axis offsets and return to imported orientation")
        btn_clear_axis.clicked.connect(self._on_clear_axis)
        row_actions.addWidget(btn_clear_axis)

        btn_reset = QPushButton("Reset View")
        btn_reset.clicked.connect(self._on_reset_view)
        row_actions.addWidget(btn_reset)

        layout.addLayout(row_actions)

        # ── Status bar ───────────────────────────────────────────────────
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #aaa; font-size: 10px;")
        layout.addWidget(self.status_label)

        # ── Synchronizer & Signals ───────────────────────────────────────
        from .canvas_sync import CanvasSynchronizer

        self.synchronizer = CanvasSynchronizer()
        self.gl_widget.renderComplete.connect(self._schedule_sync)
        self.gl_widget.rotationChanged.connect(self._on_gl_rotation_changed)
        
    def _on_cache_toggled(self, state):
        self.state_manager.use_cache = bool(state == Qt.Checked)
        if not self.state_manager.use_cache:
            self.state_manager.clear_cache()
        
        # Trigger an initial geometry update
        self.tabs.updateGeometry()

    def _on_invert_y_changed(self, state):
        renderer = getattr(self.gl_widget, '_renderer', None) if self.gl_widget else None
        if renderer:
            renderer.invert_y = bool(state == Qt.Checked)
        self._save_settings()

    # ── Krita overrides ──────────────────────────────────────────────────

    def canvasChanged(self, canvas):
        pass

    # ── Slots ────────────────────────────────────────────────────────────

    def _on_browse_workflow(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select ComfyUI Workflow JSON", "", "JSON Files (*.api.json *.json)")
        if path:
            self.edit_workflow_path.setText(path)

    def _on_toggle_opts(self, checked):
        if checked:
            self.btn_toggle_opts.setText("Options / Log Viewer ▲")
            self.opts_widget.setVisible(True)
        else:
            self.btn_toggle_opts.setText("Options / Log Viewer ▼")
            self.opts_widget.setVisible(False)
        # Update tab height
        self.tabs.updateGeometry()
        if self.tabs.parentWidget():
            self.tabs.parentWidget().updateGeometry()

    def _append_log(self, msg):
        import time
        t = time.strftime("%H:%M:%S")
        self.text_log.appendPlainText(f"[{t}] {msg}")
        # Auto-scroll to bottom
        bar = self.text_log.verticalScrollBar()
        if bar:
            bar.setValue(bar.maximum())

    def _save_settings(self):
        try:
            from PyQt5.QtCore import QSettings
            settings = QSettings("Krita", "3DLayerPlugin")
            settings.setValue("comfyUrl", self.edit_comfy_url.text())
            settings.setValue("workflowPath", self.edit_workflow_path.text())
            settings.setValue("timeout", str(self.spin_timeout.value()))
            settings.setValue("importRotX", str(self.spin_import_x.value()))
            settings.setValue("importRotY", str(self.spin_import_y.value()))
            settings.setValue("importRotZ", str(self.spin_import_z.value()))
            settings.setValue("invertY", "1" if self.chk_invert_y.isChecked() else "0")
            settings.sync()
        except Exception:
            pass

    def _load_settings(self):
        try:
            from PyQt5.QtCore import QSettings
            settings = QSettings("Krita", "3DLayerPlugin")
            
            saved_url = settings.value("comfyUrl", "http://127.0.0.1:8188")
            self.edit_comfy_url.setText(saved_url)
            
            saved_wf = settings.value("workflowPath", "")
            self.edit_workflow_path.setText(saved_wf)
            
            saved_timeout = settings.value("timeout", "5")
            self.spin_timeout.setValue(int(saved_timeout))
            
            self.spin_import_x.setValue(int(settings.value("importRotX", "0")))
            self.spin_import_y.setValue(int(settings.value("importRotY", "0")))
            self.spin_import_z.setValue(int(settings.value("importRotZ", "0")))
            self.chk_invert_y.setChecked(settings.value("invertY", "0") == "1")
        except Exception:
            pass

    def _on_generate_click(self):
        if not self.gl_widget: return
        workflow_path = self.edit_workflow_path.text()
        if not workflow_path or not os.path.exists(workflow_path):
            self.status_label.setText("Error: Select a valid workflow JSON first.")
            return

        self._save_settings()

        from .comfy_bridge import ComfyUIBridge
        
        # Capture active layer
        try:
            import krita
            app = krita.Krita.instance()
            doc = app.activeDocument()
        except Exception:
            self.status_label.setText("Could not get active document.")
            return

        if not doc:
            self.status_label.setText("No active document.")
            return

        node = doc.activeNode()
        if not node:
            self.status_label.setText("No active layer selected.")
            return

        # Get layer bounds and extract pixels
        bounds = node.bounds() # QRect
        if bounds.width() <= 0 or bounds.height() <= 0:
            self.status_label.setText("Active layer is empty.")
            return

        pixel_data = node.pixelData(bounds.x(), bounds.y(), bounds.width(), bounds.height())
        image = QImage(pixel_data, bounds.width(), bounds.height(), QImage.Format_ARGB32)

        self.bridge = ComfyUIBridge(self.edit_comfy_url.text())
        self.bridge.progressChanged.connect(lambda msg: self.status_label.setText(msg))
        self.bridge.modelReady.connect(self._on_gen_model_ready)
        self.bridge.errorOccurred.connect(self._on_gen_error)
        self.bridge.logAdded.connect(self._append_log)

        # Clear previous logs and show active state
        self.text_log.clear()
        self._append_log("--- Starting Generation ---")
        
        # UI state for active generation
        self.btn_generate.setText("Cancel Generation")
        self.btn_generate.setStyleSheet("background-color: #7a2121; font-weight: bold; min-height: 30px;")
        try:
            self.btn_generate.clicked.disconnect()
        except: pass
        self.btn_generate.clicked.connect(self._on_cancel_generate)
        
        self.gen_progress.setVisible(True)
        self.gen_progress.setRange(0, 0) # Pulsing

        # Run the workflow with the layer image
        self.bridge.run_workflow(workflow_path, image, timeout_minutes=self.spin_timeout.value())

    def _on_cancel_generate(self):
        if hasattr(self, 'bridge') and self.bridge:
            if self.bridge.isRunning():
                self.bridge.cancel()
                self.btn_generate.setText("Cancelling...")
                self.btn_generate.setEnabled(False)
            else:
                self._reset_gen_button()
        else:
            self._reset_gen_button()

    def _on_gen_error(self, err):
        self.status_label.setText(f"Error: {err}")
        self._reset_gen_button()

    def _reset_gen_button(self):
        self.btn_generate.setText("Generate 3D Model")
        self.btn_generate.setStyleSheet("background-color: #2d5a27; font-weight: bold; min-height: 30px;")
        try:
            self.btn_generate.clicked.disconnect()
        except: pass
        self.btn_generate.clicked.connect(self._on_generate_click)
        self.btn_generate.setEnabled(True)
        self.gen_progress.setVisible(False)

    def _on_gen_model_ready(self, model):
        self._reset_gen_button()
        if self.gl_widget:
            self.gl_widget.reset_view()
            # Pass import rotation to the renderer before loading model
            renderer = getattr(self.gl_widget, '_renderer', None)
            if renderer:
                renderer.import_rotation = (
                    self.spin_import_x.value(),
                    self.spin_import_y.value(),
                    self.spin_import_z.value()
                )
            self.gl_widget.set_model(model)
        
        path = getattr(self.bridge, 'last_downloaded_path', None)
        self.current_model_path = path  # Track for StateManager
        
        if path:
            self.btn_save_model.setEnabled(True)
            self.btn_save_model.setToolTip(f"Save temporary model to a permanent location")
            
        self.lbl_loaded_model.setText("Generated Model")
        self.status_label.setText("Generation complete!")
        self._on_gl_rotation_changed(180.0, 180.0, 180.0)
        self._schedule_sync()

    def _on_save_generated_model(self):
        path = getattr(self.bridge, 'last_downloaded_path', None)
        if not path or not os.path.exists(path):
            self.status_label.setText("No generated model found to save.")
            return
            
        import shutil
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save Generated Model", "generated_model.glb",
            "GLB Files (*.glb);;All Files (*)"
        )
        if save_path:
            try:
                shutil.copy2(path, save_path)
                
                # Update current paths so layer sync points to permanent file
                self.bridge.last_downloaded_path = save_path
                self.current_model_path = save_path
                self.lbl_loaded_model.setText(os.path.basename(save_path))
                self.status_label.setText(f"Model saved to {os.path.basename(save_path)}")
                
                # Active sync update if this was already painted
                renderer = getattr(self.gl_widget, '_renderer', None) if self.gl_widget else None
                if renderer:
                    self.state_manager.save_state_for_current_layer(save_path, renderer)
            except Exception as e:
                self.status_label.setText(f"Error saving model: {e}")

    def _on_load_model(self):
        from PyQt5.QtWidgets import QFileDialog
        from .obj_loader import OBJModel
        from .glb_loader import GLBModel

        path, _ = QFileDialog.getOpenFileName(
            self, "Load 3D Model", "",
            "3D Models (*.obj *.glb);;OBJ Files (*.obj);;GLB Files (*.glb);;All Files (*)")
        if not path:
            return

        if path.lower().endswith('.glb'):
            model = GLBModel(path)
        else:
            model = OBJModel(path)
            
        if model.faces:
            if self.gl_widget:
                self.gl_widget.reset_view()
                # Pass import rotation to the renderer before loading model
                renderer = getattr(self.gl_widget, '_renderer', None)
                if renderer:
                    renderer.import_rotation = (
                        self.spin_import_x.value(),
                        self.spin_import_y.value(),
                        self.spin_import_z.value()
                    )
                self.gl_widget.set_model(model)
            self.current_model_path = path
            self.lbl_loaded_model.setText(os.path.basename(path))
            self._on_gl_rotation_changed(180.0, 180.0, 180.0)
            self.status_label.setText(
                f"Loaded: {os.path.basename(path)}  "
                f"({len(model.vertices)} verts, {len(model.faces)} faces)")
            # Trigger an immediate sync so the layer updates
            self._schedule_sync()
        else:
            self.lbl_loaded_model.setText("Load failed")
            self.status_label.setText(
                "Failed to load model (no faces found)")

    def _on_view_mode_changed(self, index):
        mode = self.combo_view.itemText(index)
        if self.gl_widget:
            self.gl_widget.set_view_mode(mode)

    def _on_camera_mode_changed(self, index):
        mode = self.combo_camera.itemText(index)
        renderer = getattr(self.gl_widget, '_renderer', None) if self.gl_widget else None
        if renderer:
            old_mode = renderer.camera_mode
            # Convert camera parameters so the view doesn't jump
            if old_mode != mode and getattr(self.gl_widget, 'convert_camera_params', None):
                self.gl_widget.convert_camera_params(old_mode, mode)
            renderer.camera_mode = mode
            if mode == "Walk":
                self.sld_x.setEnabled(False)
                self.sld_y.setEnabled(False)
                self.sld_z.setEnabled(False)
                self.nav_label.setText("LMB: Look  |  MMB/RMB: Strafe  |  Wheel: Move  |  (Shift: Precision)")
                self.status_label.setText("Walk Mode: LMB to look, Wheel to move, MMB to strafe (Hold Shift for Precision)")
            else:
                self.sld_x.setEnabled(True)
                self.sld_y.setEnabled(True)
                self.sld_z.setEnabled(True)
                self.nav_label.setText("LMB: Yaw  |  RMB: Pitch  |  LMB+RMB: Roll  |  MMB: Pan  |  (Shift: Precision)")
                self.status_label.setText("Orbit Mode: LMB to rotate, Wheel to zoom, MMB to pan")
                # Sync sliders to actual camera orientation
                self._on_gl_rotation_changed(renderer.rotation_x, renderer.rotation_y, renderer.rotation_z)

    def _add_rot(self, slider, delta):
        new_val = (slider.value() + delta) % 360
        if new_val < 0:
            new_val += 360
        slider.setValue(new_val)

    def _on_rot_changed(self, _):
        renderer = getattr(self.gl_widget, '_renderer', None) if self.gl_widget else None
        if not renderer: return
        rx, ry, rz = self.sld_x.value(), self.sld_y.value(), self.sld_z.value()
        self.lbl_x.setText(f"Rot X: {rx}")
        self.lbl_y.setText(f"Rot Y: {ry}")
        self.lbl_z.setText(f"Rot Z: {rz}")
        
        renderer.rotation_x = float(rx)
        renderer.rotation_y = float(ry)
        renderer.rotation_z = float(rz)
        self.gl_widget._schedule_refresh()

    def _on_gl_rotation_changed(self, rx, ry, rz):
        """Update sliders when the viewport is rotated via mouse (LMB/RMB)."""
        self.sld_x.blockSignals(True)
        self.sld_y.blockSignals(True)
        self.sld_z.blockSignals(True)
        
        self.sld_x.setValue(int(rx))
        self.sld_y.setValue(int(ry))
        self.sld_z.setValue(int(rz))
        
        self.lbl_x.setText(f"Rot X: {int(rx)}")
        self.lbl_y.setText(f"Rot Y: {int(ry)}")
        self.lbl_z.setText(f"Rot Z: {int(rz)}")
        
        self.sld_x.blockSignals(False)
        self.sld_y.blockSignals(False)
        self.sld_z.blockSignals(False)

    def _on_fov_changed(self, value):
        self.fov_label.setText(f"FOV: {value}")
        if self.gl_widget:
            self.gl_widget.set_fov(value)
            # FOV change modifies zoom (compensation), update zoom slider
            self._update_zoom_slider_from_renderer()

    def _on_zoom_changed(self, value):
        # Map slider 1-1000 to -0.01 to -50.0
        # we'll use a simple linear map for now.
        zoom_val = -0.01 - (value - 1) * (49.99 / 999.0)
        self.zoom_label.setText(f"Zoom: {zoom_val:.2f}")
        if self.gl_widget:
            self.gl_widget.set_zoom(zoom_val)

    def _update_zoom_slider_from_renderer(self):
        renderer = getattr(self.gl_widget, '_renderer', None) if self.gl_widget else None
        if not renderer: return
        z = renderer.zoom
        # Invert the map: value = 1 + (zoom - (-0.01)) / (-49.99 / 999.0)
        # slider_val = 1 + (z + 0.01) / (-0.05004)
        val = int(1 + (z + 0.01) * (999.0 / -49.99))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(max(1, min(1000, val)))
        self.zoom_label.setText(f"Zoom: {z:.2f}")
        self.zoom_slider.blockSignals(False)

    def _on_set_axis(self):
        """Set Axis behavior: Store current rotation as offset, don't move model."""
        if self.gl_widget:
            # Tell renderer to bake current rotation into offsets
            self.gl_widget.set_rotation_as_offset(180.0)
            
            # Update UI to reflect the new 180.0 'neutral' base
            self._on_gl_rotation_changed(180.0, 180.0, 180.0)
            self.status_label.setText("Axis set: sliders centered at 180°")

    def _on_clear_axis(self):
        if self.gl_widget:
            r = self.gl_widget._renderer
            # Reset baked matrix and just return to the current slider positions
            r.model_base_matrix = QMatrix4x4()
            self.gl_widget._schedule_refresh()
            self.status_label.setText("Axis cleared (baked orientation reset to identity)")

    def _on_apply_layer(self):
        """Bake the current layer and prepare for a new '3D View'."""
        if self.synchronizer:
            if self.synchronizer.bake_layer():
                self.status_label.setText("Layer applied (baked). Next sync creates new layer.")
                
                # Actively save state to this new layer before moving on
                if self.current_model_path:
                    self.state_manager.save_state_for_current_layer(
                        self.current_model_path, 
                        self.gl_widget._renderer
                    )
            else:
                self.status_label.setText("No '3D View' layer found to apply")

    def _apply_loaded_state(self, model, path, state):
        """Called by StateManager to seamlessly restore a 3D view when a layer is clicked."""
        self.current_model_path = path
        
        # Block signals briefly so we don't trigger recursive saves/syncs on load
        self.sld_x.blockSignals(True)
        self.sld_y.blockSignals(True)
        self.sld_z.blockSignals(True)
        self.fov_slider.blockSignals(True)
        self.zoom_slider.blockSignals(True)
        
        # Parse State
        ix = state.get("import_rot_x", 0.0)
        iy = state.get("import_rot_y", 0.0)
        iz = state.get("import_rot_z", 0.0)
        rx = state.get("rot_x", 20.0)
        ry = state.get("rot_y", 150.0)
        rz = state.get("rot_z", 180.0)
        px = state.get("pan_x", 0.0)
        py = state.get("pan_y", 0.0)
        z  = state.get("zoom", -5.0)
        fov = state.get("fov", 45.0)
        v_mode = state.get("view_mode", "Wireframe")
        c_mode = state.get("camera_mode", "Orbit")
        matrix_list = state.get("matrix")
        
        # Update global UI settings to match the layer's original import rotation
        self.spin_import_x.blockSignals(True)
        self.spin_import_y.blockSignals(True)
        self.spin_import_z.blockSignals(True)
        self.spin_import_x.setValue(int(ix))
        self.spin_import_y.setValue(int(iy))
        self.spin_import_z.setValue(int(iz))
        self.spin_import_x.blockSignals(False)
        self.spin_import_y.blockSignals(False)
        self.spin_import_z.blockSignals(False)
        
        # Update Renderer
        r = self.gl_widget._renderer
        r.import_rotation = (ix, iy, iz)
        r.rotation_x = rx
        r.rotation_y = ry
        r.rotation_z = rz
        r.pan_x = px
        r.pan_y = py
        r.zoom = z
        r.fov = fov
        r.view_mode = v_mode
        r.camera_mode = c_mode
        
        if matrix_list and len(matrix_list) == 16:
            r.model_base_matrix = QMatrix4x4(*matrix_list)
        else:
            r.model_base_matrix = QMatrix4x4()
            
        self.gl_widget.set_model(model)
        
        # Update UI Sliders to match
        self.sld_x.setValue(int(rx))
        self.sld_y.setValue(int(ry))
        self.sld_z.setValue(int(rz))
        self.lbl_x.setText(f"Rot X: {int(rx)}")
        self.lbl_y.setText(f"Rot Y: {int(ry)}")
        self.lbl_z.setText(f"Rot Z: {int(rz)}")
        
        self.fov_slider.setValue(int(fov))
        self.fov_label.setText(f"FOV: {int(fov)}")
        
        # Convert zoom back to slider val (1-1000 map)
        slider_val = int(1 + (z + 0.01) * (999.0 / -49.99))
        self.zoom_slider.setValue(max(1, min(1000, slider_val)))
        self.zoom_label.setText(f"Zoom: {z:.2f}")
        
        idx = self.combo_view.findText(v_mode)
        if idx >= 0:
            self.combo_view.setCurrentIndex(idx)
            
        idx_c = getattr(self, 'combo_camera', None)
        if idx_c and idx_c.findText(c_mode) >= 0:
            idx_c.blockSignals(True)
            idx_c.setCurrentIndex(idx_c.findText(c_mode))
            idx_c.blockSignals(False)
            
        self.lbl_loaded_model.setText(os.path.basename(path))
        self.status_label.setText(f"Restored 3D view from layer's saved state.")
        
        # Unblock and redraw
        self.sld_x.blockSignals(False)
        self.sld_y.blockSignals(False)
        self.sld_z.blockSignals(False)
        self.fov_slider.blockSignals(False)
        self.zoom_slider.blockSignals(False)
        
        self.gl_widget._schedule_refresh()

    def _on_reset_view(self):
        if self.gl_widget:
            self.gl_widget.reset_view()
            # reset_view uses default 180, 180, 180
            rx, ry, rz = 180, 180, 180
            self.sld_x.setValue(rx)
            self.sld_y.setValue(ry)
            self.sld_z.setValue(rz)
            self.fov_slider.setValue(45)
            self.zoom_slider.setValue(100)
            self.lbl_x.setText(f"Rot X: {rx}")
            self.lbl_y.setText(f"Rot Y: {ry}")
            self.lbl_z.setText(f"Rot Z: {rz}")
            self.fov_label.setText("FOV: 45")
            self.zoom_label.setText("Zoom: -5.00")
            self.status_label.setText("View reset")

    # ── Debounced layer sync ─────────────────────────────────────────────

    def _schedule_sync(self):
        """(Re)start the debounce timer.  The actual sync fires once the
        user stops interacting for 200 ms."""
        self._sync_timer.start()

    def _do_sync(self):
        """Push a high-resolution render to the Krita layer."""
        if not self.gl_widget or not self.synchronizer:
            return

        app = __import__('krita').Krita.instance()
        doc = app.activeDocument()
        if not doc:
            self.status_label.setText("No active document — open or create one first")
            return

        # NEW: Direct Layer Editing Tweak
        # If the active layer is already linked, sync directly to it.
        # Otherwise, fall back to "3D View" logic.
        target_node = doc.activeNode()
        if not self.state_manager.is_node_linked(target_node):
            target_node = None
            
        ok = self.synchronizer.sync_to_canvas(self.gl_widget, target_node=target_node)
        
        if ok:
            # Refresh if we didn't use activeNode or just to be safe
            doc.refreshProjection()
            
            # Auto-save 3D state to the target layer
            if getattr(self, 'current_model_path', None):
                # If we were on "3D View" (target_node was None), 
                # find the actual "3D View" layer that was just created/updated
                if target_node is None:
                    # synchronizer has layer_name ("3D View")
                    target_node = self.synchronizer._find_layer(doc.rootNode(), self.synchronizer.layer_name)
                
                if target_node:
                    self.state_manager.save_state_for_current_layer(
                        self.current_model_path, 
                        self.gl_widget._renderer,
                        target_node
                    )
            
            self.status_label.setText(
                f"Synced to '{target_node.name() if target_node else 'Unknown'}' layer")
        else:
            self.status_label.setText("Sync failed.")
