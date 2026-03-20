import json
import uuid
import base64
import io
import os
from PyQt5.QtCore import QObject, pyqtSignal, QThread
from PyQt5.QtGui import QImage
import urllib.request
import urllib.parse
import urllib.error

from .glb_loader import GLBModel

class ComfyUIBridge(QObject):
    """
    Handles communication with ComfyUI.
    Uses the 'Tooling Nodes' pattern:
    - Injects Base64 image into a Load Image node.
    - Listens for binary output from a Send Image (WebSocket) node.
    """
    progressChanged = pyqtSignal(str)
    modelReady = pyqtSignal(object)  # Emits GLBModel
    errorOccurred = pyqtSignal(str)
    logAdded = pyqtSignal(str)       # Emits detailed log messages

    def __init__(self, server_address="http://127.0.0.1:8188"):
        super().__init__()
        self.server_address = server_address.rstrip('/')
        self.client_id = str(uuid.uuid4())
        self._cancel_requested = False

    def cancel(self):
        """Request to stop the current operation."""
        self._cancel_requested = True
        self.progressChanged.emit("Cancellation requested...")

    def run_workflow(self, workflow_json_path, qimage, timeout_minutes=5):
        """ Runs the workflow and monitors for completion. """
        self._cancel_requested = False
        try:
            with open(workflow_json_path, 'r', encoding='utf-8') as f:
                workflow = json.load(f)

            # Check for API vs Workflow format
            if 'nodes' in workflow and 'links' in workflow:
                self.logAdded.emit("Error: Detected 'Workflow' JSON format.")
                self.logAdded.emit("Please enable 'Developer Mode' in ComfyUI settings")
                self.logAdded.emit("and use 'Save (API Format)' to export your JSON.")
                raise Exception("JSON is in 'Workflow' format, but 'API' format is required.")

            # 1. Convert QImage to Base64 (PNG preserves alpha)
            # Use Qt components instead of io.BytesIO to avoid TypeError
            from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
            byte_array = QByteArray()
            buffer = QBuffer(byte_array)
            buffer.open(QIODevice.WriteOnly)
            qimage.save(buffer, "PNG")
            img_str = base64.b64encode(byte_array.data()).decode('utf-8')
            
            # 2. Inject into 'Krita_Input' node
            input_node_id = None
            for node_id, node_data in workflow.items():
                if isinstance(node_data, dict):
                    if node_data.get('_meta', {}).get('title') == 'Krita_Input':
                        input_node_id = node_id
                        break
            
            if not input_node_id:
                for node_id, node_data in workflow.items():
                    if isinstance(node_data, dict):
                        if node_data.get('class_type') in ['ETN_LoadImageBase64', 'LoadImage']:
                            input_node_id = node_id
                            break
            
            if not input_node_id:
                raise Exception("Could not find 'Krita_Input' or compatible Load node.")

            workflow[input_node_id]['inputs']['image'] = img_str
            self.logAdded.emit(f"Injected base64 image into node '{input_node_id}'")

            if self._cancel_requested: return

            # 3. Send Prompt to ComfyUI
            self.progressChanged.emit("Sending workflow to ComfyUI...")
            self.logAdded.emit(f"Connecting to ComfyUI at {self.server_address}...")
            
            data = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode('utf-8')
            req = urllib.request.Request(f"{self.server_address}/prompt", data=data)
            
            try:
                with urllib.request.urlopen(req) as f:
                    response = json.loads(f.read().decode('utf-8'))
                    prompt_id = response['prompt_id']
                    self.logAdded.emit(f"Workflow dispatched successfully. Prompt ID: {prompt_id}")
            except urllib.error.URLError as e:
                self.logAdded.emit(f"HTTP Error: Failed to reach ComfyUI server. Is it running?\n{e}")
                raise Exception("Failed to connect to ComfyUI.")

            self._listen_for_result(prompt_id, timeout_minutes)

        except Exception as e:
            if self._cancel_requested:
                self.logAdded.emit("Operation cancelled by user.")
                self.errorOccurred.emit("Generation cancelled.")
            else:
                import traceback
                self.logAdded.emit(f"Workflow Execution Error:\n{traceback.format_exc()}")
                self.errorOccurred.emit(str(e))

    def _listen_for_result(self, prompt_id, timeout_minutes):
        self.progressChanged.emit("Waiting for generation...")
        
        import time
        max_retries = int(timeout_minutes * 60)
        self.logAdded.emit(f"Polling for results... (Timeout: {timeout_minutes}m)")
        
        for attempt in range(max_retries):
            if self._cancel_requested:
                self.logAdded.emit("Polling cancelled by user.")
                self.errorOccurred.emit("Generation cancelled.")
                return

            try:
                with urllib.request.urlopen(f"{self.server_address}/history/{prompt_id}") as f:
                    history = json.loads(f.read().decode('utf-8'))
                    if prompt_id in history:
                        self.logAdded.emit(f"Generation complete! Found Prompt ID {prompt_id} in history.")
                        self._process_history_for_glb(history[prompt_id])
                        return
            except urllib.error.URLError as e:
                # Optionally log polling connection errors, but keep it sparse to avoid spam
                pass
            except Exception as e:
                import traceback
                self.logAdded.emit(f"Polling Error:\n{traceback.format_exc()}")
                
            time.sleep(1)
        
        self.logAdded.emit("Polling timed out.")
        self.errorOccurred.emit(f"Timeout (>{timeout_minutes}m) waiting for ComfyUI.")

    def _process_history_for_glb(self, history_item):
        # Look for GLB in outputs
        self.logAdded.emit("Scanning ComfyUI execution history for .glb outputs...")
        outputs = history_item.get('outputs', {})
        
        if not outputs:
            self.logAdded.emit("Warning: No 'outputs' found in history item for this prompt.")
            return

        for node_id, node_data in outputs.items():
            self.logAdded.emit(f"  Checking Output Node {node_id} (Keys: {list(node_data.keys())})")
            
            for key, val in node_data.items():
                files = []
                if isinstance(val, list):
                    files = val
                elif isinstance(val, dict):
                    files = [val]
                elif isinstance(val, str):
                    files = [val]
                
                for item in files:
                    filename = None
                    subfolder = ""
                    folder_type = "output"

                    if isinstance(item, str) and item.lower().endswith('.glb'):
                        filename = item
                    elif isinstance(item, dict):
                        # Check common keys for GLB paths
                        for glb_key in ['filename', 'mesh', 'filepath', 'file_name']:
                            val = item.get(glb_key, '')
                            if isinstance(val, str) and val.lower().endswith('.glb'):
                                filename = val
                                subfolder = item.get('subfolder', '')
                                folder_type = item.get('type', 'output')
                                
                                # If it's an absolute path (contains : or / and starts with drive or root)
                                # we try to make it relative to 'output' for the ComfyUI /view endpoint
                                if os.path.isabs(filename):
                                    self.logAdded.emit(f"  -> Absolute path detected: {filename}")
                                    if 'output' in filename.lower():
                                        parts = filename.replace('\\', '/').split('/output/')
                                        if len(parts) > 1:
                                            rel_path = parts[1]
                                            if '/' in rel_path:
                                                subfolder, filename = rel_path.rsplit('/', 1)
                                            else:
                                                filename = rel_path
                                                subfolder = ""
                                            self.logAdded.emit(f"  -> Extracted relative path: {subfolder}/{filename}")
                                break
                    
                    if filename:
                        self.logAdded.emit(f"  -> Found GLB! filename={filename}, subfolder={subfolder}, type={folder_type}")
                        self._download_and_load_glb(filename, subfolder, folder_type)
                        return
        
        self.logAdded.emit("Error: Scanned all nodes but found no .glb references.")
        # Debug: Print first 500 chars of outputs to help user identify path
        self.logAdded.emit(f"Full Outputs Data (truncated): {str(outputs)[:500]}...")
        self.errorOccurred.emit("Could not find a .glb file in ComfyUI output.")

    def _download_and_load_glb(self, filename, subfolder="", folder_type="output"):
        self.progressChanged.emit("Downloading 3D model...")
        
        # Construct URL with subfolder and type support
        params = {'filename': filename, 'type': folder_type}
        if subfolder:
            params['subfolder'] = subfolder
        
        url = f"{self.server_address}/view?{urllib.parse.urlencode(params)}"
        self.logAdded.emit(f"Downloading from: {url}")
        
        import tempfile
        tmp_dir = tempfile.gettempdir()
        local_path = os.path.join(tmp_dir, "comfy_output.glb")
        
        try:
            urllib.request.urlretrieve(url, local_path)
            self.logAdded.emit(f"Downloaded model to {local_path} successfully. Loading...")
            # Create model and notify
            model = GLBModel(local_path)
            self.last_downloaded_path = local_path
            self.modelReady.emit(model)
        except Exception as e:
            import traceback
            self.logAdded.emit(f"Download or GLB Loading failed:\n{traceback.format_exc()}")
            self.errorOccurred.emit(f"Failed to download/load GLB: {e}")
