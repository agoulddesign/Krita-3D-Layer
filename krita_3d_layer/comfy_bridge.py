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

class ComfyUIBridge(QThread):
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
        
        # Thread state vars
        self._workflow_json_path = None
        self._img_str = None
        self._timeout_minutes = 5

    def cancel(self):
        """Request to stop the current operation."""
        self._cancel_requested = True
        self.progressChanged.emit("Cancellation requested...")
        
        # True cancellation: send interrupt directly to ComfyUI
        try:
            req = urllib.request.Request(f"{self.server_address}/interrupt", data=b"", method="POST")
            with urllib.request.urlopen(req) as f:
                self.logAdded.emit("Sent interrupt signal to ComfyUI.")
        except Exception as e:
            self.logAdded.emit(f"Note: Could not interrupt ComfyUI API: {e}")

    def run_workflow(self, workflow_json_path, qimage, timeout_minutes=5):
        """ Prepares image data on main GUI thread and starts worker thread. """
        self._cancel_requested = False
        self._workflow_json_path = workflow_json_path
        self._timeout_minutes = timeout_minutes
        
        try:
            # 1. Convert QImage to Base64 (PNG preserves alpha)
            # Must run on main thread so we avoid cross-thread Qt issues.
            from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
            
            # DEBUG: Save the exact image being sent to ComfyUI to the Desktop
            import os
            debug_path = os.path.join(os.path.expanduser("~"), "Desktop", "debug_krita_input.png")
            qimage.save(debug_path, "PNG")
            # comment to here to remove debug

            byte_array = QByteArray()
            buffer = QBuffer(byte_array)
            buffer.open(QIODevice.WriteOnly)
            qimage.save(buffer, "PNG")
            self._img_str = base64.b64encode(byte_array.data()).decode('utf-8')
        except Exception as e:
            self.errorOccurred.emit(f"Failed to encode Krita layer to Base64: {e}")
            return
            
        self.start()

    def run(self):
        """ Background thread execution block """
        try:
            with open(self._workflow_json_path, 'r', encoding='utf-8') as f:
                workflow = json.load(f)

            # Check for API vs Workflow format
            if 'nodes' in workflow and 'links' in workflow:
                self.logAdded.emit("Error: Detected 'Workflow' JSON format.")
                self.logAdded.emit("Please enable 'Developer Mode' in ComfyUI settings")
                self.logAdded.emit("and use 'Save (API Format)' to export your JSON.")
                raise Exception("JSON is in 'Workflow' format, but 'API' format is required.")

            img_str = self._img_str
            
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

            self._listen_for_result(prompt_id, self._timeout_minutes)

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
        self.logAdded.emit("Scanning ComfyUI execution history for .glb outputs...")
        outputs = history_item.get('outputs', {})
        prompt_data = history_item.get('prompt', [{}, {}])[2] if isinstance(history_item.get('prompt'), list) else history_item.get('prompt', {})
        # Note: In some ComfyUI versions, 'prompt' is [prompt_id, client_id, nodes_dict, extra_data]
        # In others, it's just the nodes_dict. We handle both.
        nodes = prompt_data if isinstance(prompt_data, dict) else {}
        
        if not outputs:
            self.logAdded.emit("Warning: No 'outputs' found in history item.")
            self.errorOccurred.emit("ComfyUI returned completion but no output data found.")
            return

        candidates = []
        
        # 1. Collect ALL GLB candidates from all nodes
        for node_id, node_data in outputs.items():
            node_info = nodes.get(node_id, {})
            node_title = node_info.get('_meta', {}).get('title', f"Node {node_id}")
            
            for key, val in node_data.items():
                file_items = []
                if isinstance(val, list): file_items = val
                elif isinstance(val, dict): file_items = [val]
                elif isinstance(val, str): file_items = [val]
                
                for item in file_items:
                    filename = None
                    subfolder = ""
                    folder_type = "output"

                    if isinstance(item, str) and item.lower().endswith('.glb'):
                        filename = item
                    elif isinstance(item, dict):
                        for glb_key in ['filename', 'mesh', 'filepath', 'file_name', 'glb_path', 'relative_path', 'file']:
                            path_val = item.get(glb_key, '')
                            if isinstance(path_val, str) and path_val.lower().endswith('.glb'):
                                filename = path_val
                                subfolder = item.get('subfolder', '')
                                folder_type = item.get('type', 'output')
                                break
                    
                    if filename:
                        candidates.append({
                            'filename': filename,
                            'subfolder': subfolder,
                            'type': folder_type,
                            'node_id': node_id,
                            'node_title': node_title
                        })

        if not candidates:
            self.logAdded.emit("Error: Scanned all nodes but found no .glb references.")
            self.errorOccurred.emit("Could not find a .glb file in ComfyUI output.")
            return

        # 2. Apply Selection Heuristics
        self.logAdded.emit(f"Found {len(candidates)} GLB candidate(s). Applying selection logic...")
        
        # Priority 1: Check for node title "Krita_Output"
        output_nodes = [c for c in candidates if c['node_title'] == 'Krita_Output']
        if output_nodes:
            self.logAdded.emit("  -> Priority 1: Found node titled 'Krita_Output'.")
            best = self._pick_best_from_subset(output_nodes)
        else:
            # Priority 2/3: Pick from all, but highest priority to those with numeric suffixes
            self.logAdded.emit("  -> Priority 2/3: No 'Krita_Output' node. Selecting best from all candidates.")
            best = self._pick_best_from_subset(candidates)

        if best:
            self.logAdded.emit(f"  -> Selected: {best['filename']} from '{best['node_title']}'")
            self._download_and_load_glb(best['filename'], best['subfolder'], best['type'])
        else:
            self.errorOccurred.emit("Internal error: Could not select best GLB candidate.")

    def _pick_best_from_subset(self, subset):
        """Pick the best GLB from a list based on numeric suffix and execution order."""
        if not subset: return None
        
        import re
        def get_score(cand):
            # Extract numeric suffix like _00001
            match = re.search(r'_(\d+)\.glb$', cand['filename'].lower())
            suffix_val = int(match.group(1)) if match else -1
            # Return tuple for sorting: (has_suffix, suffix_val, node_id_as_int)
            node_id = 0
            try: node_id = int(cand['node_id'])
            except: pass
            return (suffix_val, node_id)

        # Sort by suffix (primary) then node_id (secondary - execution order)
        sorted_subset = sorted(subset, key=get_score, reverse=True)
        return sorted_subset[0]

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
