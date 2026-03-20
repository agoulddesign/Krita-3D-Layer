"""
Krita 3D Layer plugin entry point.

Registers the Extension and DockWidgetFactory with Krita.
"""

import sys
import os

# Add the plugin directory to sys.path so the bundled PyOpenGL package
# (shipped alongside this plugin) can be imported.
_plugin_dir = os.path.dirname(os.path.realpath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from krita import Krita, Extension, DockWidgetFactory, DockWidgetFactoryBase
from .docker_ui import Krita3DLayerDocker

DOCKER_ID = "krita_3d_layer"


class Krita3DExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        pass

    def createActions(self, window):
        pass


_app = Krita.instance()
_app.addExtension(Krita3DExtension(_app))
_app.addDockWidgetFactory(
    DockWidgetFactory(DOCKER_ID, DockWidgetFactoryBase.DockRight, Krita3DLayerDocker)
)
