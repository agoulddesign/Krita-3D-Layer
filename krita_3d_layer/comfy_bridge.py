import json
import uuid
import base64
import io
import os
import re
import time
import random
import tempfile
import traceback
import shutil

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
    - Polls /history for completion and retrieves the generated .glb model.

    Supports output from:
    - Trellis2ExportMesh (returns STRING: absolute path + relative path)
    - SaveGLB / StableFast3DSave (returns dict: {filename, subfolder, type})
    """
    progressChanged = pyqtSignal(str)
    modelReady = pyqtSignal(object)  # Emits GLBModel
    errorOccurred = pyqtSignal(str)
    logAdded = pyqtSignal(str)       # Emits detailed log messages

    def __init__(self, server_address="http://127.0.0.1:8188", server_output_path=""):
        super().__init__()
        self.server_address = server_address.rstrip('/')
        self.server_output_path = server_output_path.strip().replace('\\', '/')
        self.client_id = str(uuid.uuid4())

        self._cancel_requested = False
        
        # Thread state vars
        self._workflow_json_path = None
        self._img_str = None
        self._timeout_minutes = 5
        self._output_prefix = None

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
            # Convert QImage to Base64 (PNG preserves alpha)
            # Must run on main thread so we avoid cross-thread Qt issues.
            from PyQt5.QtCore import QByteArray, QBuffer, QIODevice

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
            
            # 1. Inject into 'Krita_Input' node
            input_node_id = self._find_node(workflow, title='Krita_Input')
            if not input_node_id:
                input_node_id = self._find_node(workflow, class_types=['ETN_LoadImageBase64', 'LoadImage'])
            
            if not input_node_id:
                raise Exception("Could not find 'Krita_Input' or compatible Load node.")

            workflow[input_node_id]['inputs']['image'] = img_str
            self.logAdded.emit(f"Injected base64 image into node '{input_node_id}'")

            # 2. Inject unique filename prefix for robust output identification
            short_id = f"{int(time.time() % 10000):04d}{random.randint(0, 99):02d}"
            self._output_prefix = f"K3D_{short_id}"
            
            output_node_id = self._find_node(workflow, title='Krita_Output')
            if not output_node_id:
                output_node_id = self._find_node(workflow, class_types=[
                    'Trellis2ExportMesh', 'SaveGLB', 'StableFast3DSave'
                ])
            
            if output_node_id:
                self._inject_prefix(workflow, output_node_id)
            else:
                self.logAdded.emit("Warning: No 'Krita_Output' or compatible save node found. "
                                   "Falling back to default identification.")

            if self._cancel_requested:
                return

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
                self.logAdded.emit(f"Workflow Execution Error:\n{traceback.format_exc()}")
                self.errorOccurred.emit(str(e))

    # ── Helpers for node lookup and prefix injection ─────────────────────

    def _find_node(self, workflow, title=None, class_types=None):
        """Find a node in the workflow by title or class_type list."""
        for node_id, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            if title and node_data.get('_meta', {}).get('title') == title:
                return node_id
            if class_types and node_data.get('class_type') in class_types:
                return node_id
        return None

    def _inject_prefix(self, workflow, output_node_id):
        """Inject a unique filename prefix into the output node, preserving directory components."""
        inputs = workflow[output_node_id].get('inputs', {})
        
        # Find the correct input key
        found_key = None
        for key in ['filename_prefix', 'file_name_prefix', 'file_name', 'prefix']:
            if key in inputs:
                found_key = key
                break
        
        if not found_key:
            # Fallback: add filename_prefix if node is a known output type
            found_key = 'filename_prefix'
        
        # Preserve directory component (e.g., "3D/Trellis2" → "3D/K3D_123456")
        old_value = str(inputs.get(found_key, ''))
        if '/' in old_value:
            dir_part = old_value.rsplit('/', 1)[0]
            new_value = f"{dir_part}/{self._output_prefix}"
        else:
            new_value = self._output_prefix
        
        workflow[output_node_id]['inputs'][found_key] = new_value
        self.logAdded.emit(f"Injected prefix '{new_value}' into node '{output_node_id}' ({found_key})")

    # ── Polling ──────────────────────────────────────────────────────────

    def _listen_for_result(self, prompt_id, timeout_minutes):
        self.progressChanged.emit("Waiting for generation...")
        
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
            except urllib.error.URLError:
                pass  # Server might be busy, keep polling
            except Exception as e:
                self.logAdded.emit(f"Polling Error:\n{traceback.format_exc()}")
                
            time.sleep(1)
        
        self.logAdded.emit("Polling timed out.")
        self.errorOccurred.emit(f"Timeout (>{timeout_minutes}m) waiting for ComfyUI.")

    # ── History Processing ───────────────────────────────────────────────

    def _process_history_for_glb(self, history_item):
        """Scan ComfyUI execution history for .glb outputs.
        
        Handles two output formats:
        1. Dict format (SaveGLB, StableFast3DSave):
           {"filename": "model.glb", "subfolder": "3D", "type": "output"}
        2. String format (Trellis2ExportMesh):
           "D:/ComfyUI/output/3D/K3D_123456_00001_.glb"
        """
        self.logAdded.emit("Scanning ComfyUI execution history for .glb outputs...")
        
        outputs = history_item.get('outputs', {})
        ui_outputs = history_item.get('ui', {})
        
        # Get the original prompt data to look up node metadata
        prompt_data = history_item.get('prompt', [{}, {}])
        if isinstance(prompt_data, list) and len(prompt_data) > 2:
            nodes = prompt_data[2] if isinstance(prompt_data[2], dict) else {}
        else:
            nodes = prompt_data if isinstance(prompt_data, dict) else {}
        
        if not outputs and not ui_outputs:
            self.logAdded.emit(f"Warning: No 'outputs' or 'ui' found in history item. Trying direct directory scan...")
            if self._scan_output_dir_for_prefix():
                return
            self.logAdded.emit(f"Available keys in history: {list(history_item.keys())}")
            self.errorOccurred.emit("ComfyUI returned completion but no output data found.")
            return


        candidates = []
        
        # Merge outputs and ui_outputs into a single search space
        search_space = {}
        search_space.update(outputs)
        for node_id, node_data in ui_outputs.items():
            if node_id not in search_space:
                search_space[node_id] = node_data
            elif isinstance(search_space[node_id], dict) and isinstance(node_data, dict):
                search_space[node_id].update(node_data)

        self.logAdded.emit(f"Searching {len(search_space)} output node(s)...")

        for node_id, node_data in search_space.items():
            node_info = nodes.get(node_id, {})
            node_title = node_info.get('_meta', {}).get('title', f"Node {node_id}")
            
            if not isinstance(node_data, dict):
                continue

            for key, val in node_data.items():
                # Normalize val to a list of items
                if isinstance(val, list):
                    file_items = val
                elif isinstance(val, dict):
                    file_items = [val]
                elif isinstance(val, str):
                    file_items = [val]
                else:
                    continue
                
                for item in file_items:
                    candidate = self._extract_glb_candidate(item, node_id, node_title, key)
                    if candidate:
                        candidates.append(candidate)

        if not candidates:
            self.logAdded.emit("No .glb references found in history. Trying direct directory scan...")
            if self._scan_output_dir_for_prefix():
                return
            self.errorOccurred.emit("Could not find a .glb file in ComfyUI output or directory.")
            return

        # Apply selection logic
        self.logAdded.emit(f"Found {len(candidates)} GLB candidate(s). Applying selection logic...")
        
        # Priority 1: Node titled "Krita_Output"
        krita_output = [c for c in candidates if c['node_title'] == 'Krita_Output']
        if krita_output:
            self.logAdded.emit("  -> Priority 1: Found node titled 'Krita_Output'.")
            best = self._pick_best_from_subset(krita_output)
        else:
            self.logAdded.emit("  -> Priority 2: No 'Krita_Output' node. Selecting from all candidates.")
            best = self._pick_best_from_subset(candidates)

        if best:
            self.logAdded.emit(f"  -> Selected: {best['filename']} from '{best['node_title']}'")
            self._download_and_load_glb(best)
        else:
            self.errorOccurred.emit("Internal error: Could not select best GLB candidate.")

    def _extract_glb_candidate(self, item, node_id, node_title, key):
        """Extract a GLB candidate from a single output item.
        
        Handles:
        - str: raw filepath (absolute or relative) from Trellis2ExportMesh STRING output
        - dict: standard ComfyUI file info dict with filename/subfolder/type keys
        """
        filename = None
        subfolder = ""
        folder_type = "output"
        absolute_path = None  # Track absolute paths for direct loading

        if isinstance(item, str):
            # STRING output — could be absolute path or relative path
            path_str = item.strip()
            if not path_str.lower().endswith('.glb'):
                return None
            
            # Normalize separators
            path_str = path_str.replace('\\', '/')
            
            # Check if it's an absolute path (Windows drive letter or Unix root)
            is_absolute = (len(path_str) > 2 and path_str[1] == ':') or path_str.startswith('/')
            
            if is_absolute:
                absolute_path = path_str.replace('/', os.sep)  # Restore OS-native separators
                filename = os.path.basename(absolute_path)
                # Try to extract subfolder relative to a known "output" directory
                subfolder = self._extract_subfolder_from_abs_path(path_str)
            else:
                # Relative path like "3D/K3D_123456_00001_.glb"
                if '/' in path_str:
                    parts = path_str.rsplit('/', 1)
                    subfolder = parts[0]
                    filename = parts[1]
                else:
                    filename = path_str
                    
        elif isinstance(item, dict):
            # Standard ComfyUI dict format
            for glb_key in ['filename', 'mesh', 'filepath', 'file_name', 'glb_path',
                            'relative_path', 'file', 'mesh_path', 'output_path']:
                path_val = item.get(glb_key, '')
                if isinstance(path_val, str) and path_val.lower().endswith('.glb'):
                    raw = path_val.replace('\\', '/')
                    
                    is_abs = (len(raw) > 2 and raw[1] == ':') or raw.startswith('/')
                    if is_abs:
                        absolute_path = raw.replace('/', os.sep)
                        filename = os.path.basename(absolute_path)
                        subfolder = item.get('subfolder', self._extract_subfolder_from_abs_path(raw))
                    elif '/' in raw:
                        parts = raw.rsplit('/', 1)
                        filename = parts[1]
                        subfolder = item.get('subfolder', parts[0])
                    else:
                        filename = raw
                        subfolder = item.get('subfolder', '')
                    
                    folder_type = item.get('type', 'output')
                    break
        else:
            return None
        
        if not filename:
            return None
            
        # Try to build a "trusted" local absolute path if it doesn't exist yet
        if not absolute_path and self.server_output_path:
            full_path = os.path.join(self.server_output_path, subfolder, filename).replace('\\', '/')
            if os.path.isfile(full_path):
                absolute_path = full_path.replace('/', os.sep)
                self.logAdded.emit(f"  -> Reconstructed trusted local path: {absolute_path}")
        
        self.logAdded.emit(f"  -> Found GLB: key='{key}', file='{filename}', "
                          f"subfolder='{subfolder}', absolute={absolute_path is not None}")

        
        return {
            'filename': filename,
            'subfolder': subfolder,
            'type': folder_type,
            'absolute_path': absolute_path,
            'node_id': node_id,
            'node_title': node_title,
        }

    def _extract_subfolder_from_abs_path(self, normalized_path):
        """Extract the subfolder component from an absolute path.
        
        Given "D:/ComfyUI/output/3D/file.glb", tries to find "output/" marker
        and returns "3D" (the part between output/ and the filename).
        """
        # Look for /output/ in the path to determine the subfolder
        lower = normalized_path.lower()
        output_marker = '/output/'
        idx = lower.rfind(output_marker)
        if idx >= 0:
            after_output = normalized_path[idx + len(output_marker):]
            # after_output = "3D/K3D_123456_00001_.glb" — split off filename
            if '/' in after_output:
                return after_output.rsplit('/', 1)[0]
        return ""

    def _pick_best_from_subset(self, subset):
        """Pick the best GLB from a list based on prefix match, numeric suffix, and node order."""
        if not subset:
            return None
        
        def get_score(cand):
            # Priority 1: Prefix match with our injected prefix
            prefix_match = 0
            if self._output_prefix and cand['filename'].startswith(self._output_prefix):
                prefix_match = 1
            
            # Priority 2: Highest numeric suffix (e.g., _00002 > _00001)
            # Match patterns like _00001_ or _00001 before .glb
            match = re.search(r'_(\d+)_?\.glb$', cand['filename'].lower())
            suffix_val = int(match.group(1)) if match else -1
            
            if suffix_val != -1:
                self.logAdded.emit(f"    Scored {cand['filename']}: suffix={suffix_val}, node={cand['node_id']}")

            
            # Priority 3: Higher node ID (later in execution)
            node_id = 0
            try:
                node_id = int(cand['node_id'])
            except (ValueError, TypeError):
                pass
            
            return (prefix_match, suffix_val, node_id)

        sorted_subset = sorted(subset, key=get_score, reverse=True)
        return sorted_subset[0]

    # ── Download & Load ──────────────────────────────────────────────────

    def _download_and_load_glb(self, candidate):
        """Download or locate the GLB file and load it as a GLBModel.
        
        Strategy (in order):
        1. Direct local file — if absolute_path exists on disk, copy to temp and load.
        2. /view endpoint — use filename + subfolder + type to download from ComfyUI.
        """
        self.progressChanged.emit("Downloading 3D model...")
        
        tmp_dir = tempfile.gettempdir()
        local_path = os.path.join(tmp_dir, "comfy_output.glb")
        
        filename = candidate['filename']
        subfolder = candidate.get('subfolder', '')
        folder_type = candidate.get('type', 'output')
        absolute_path = candidate.get('absolute_path')
        
        # ── Strategy 1: Direct local file access ────────────────────────
        if absolute_path and os.path.isfile(absolute_path):
            self.logAdded.emit(f"Strategy 1: Found file locally at: {absolute_path}")
            try:
                shutil.copy2(absolute_path, local_path)
                self.logAdded.emit(f"Copied to temp path: {local_path}")
                model = GLBModel(local_path)
                if model.faces:
                    self.last_downloaded_path = local_path
                    self.modelReady.emit(model)
                    return
                else:
                    self.logAdded.emit("Warning: Local file loaded but has no faces. Trying /view...")
            except Exception as e:
                self.logAdded.emit(f"Strategy 1 failed: {e}. Trying /view endpoint...")
        elif absolute_path:
            self.logAdded.emit(f"Strategy 1: Absolute path not found on disk: {absolute_path}")
        
        # ── Strategy 2: /view endpoint ──────────────────────────────────
        params = {'filename': filename, 'type': folder_type}
        if subfolder:
            params['subfolder'] = subfolder
        
        url = f"{self.server_address}/view?{urllib.parse.urlencode(params)}"
        
        self.logAdded.emit(f"Strategy 2: Downloading via /view endpoint")
        self.logAdded.emit(f"  filename: {filename}")
        self.logAdded.emit(f"  subfolder: {subfolder}")
        self.logAdded.emit(f"  type: {folder_type}")
        self.logAdded.emit(f"  URL: {url}")
        
        try:
            urllib.request.urlretrieve(url, local_path)
            
            # Validate the downloaded file is actually a GLB (starts with "glTF")
            with open(local_path, 'rb') as f:
                magic = f.read(4)
            
            if magic != b'glTF':
                self.logAdded.emit(f"Warning: Downloaded file does not start with glTF magic. "
                                  f"Got: {magic!r}. Server may have returned an error page.")
                # Try reading as text to see if it's an error message
                try:
                    with open(local_path, 'r', encoding='utf-8', errors='replace') as f:
                        error_text = f.read(500)
                    self.logAdded.emit(f"Server response: {error_text[:200]}")
                except Exception:
                    pass
                raise Exception("Downloaded file is not a valid GLB.")
            
            self.logAdded.emit(f"Downloaded and validated GLB ({os.path.getsize(local_path)} bytes)")
            model = GLBModel(local_path)
            self.last_downloaded_path = local_path
            self.modelReady.emit(model)
            return
            
        except Exception as e:
            self.logAdded.emit(f"Strategy 2 failed: {e}")
        
        # ── Strategy 3: Directory scan fallback ──────────────────────────
        if self._scan_output_dir_for_prefix():
            return
            
        # ── All strategies exhausted ────────────────────────────────────
        self.logAdded.emit(f"All download strategies failed for: {filename}")
        self.errorOccurred.emit(f"Failed to download/load GLB: {filename}")

    def _scan_output_dir_for_prefix(self):
        """Scan the server output directory recursively for .glb files matching our prefix.
        This is the ultimate fallback — doesn't depend on history JSON at all.
        """
        if not self.server_output_path or not self._output_prefix:
            return False
        
        # Normalize to OS path
        base_dir = self.server_output_path.replace('/', os.sep)
        if not os.path.isdir(base_dir):
            return False
        
        self.logAdded.emit(f"Scanning directory for prefix '{self._output_prefix}'...")
        self.logAdded.emit(f"  Target Path: {base_dir}")

        
        matches = []
        try:
            for root, dirs, files in os.walk(base_dir):
                for fname in files:
                    if fname.lower().endswith('.glb') and self._output_prefix in fname:
                        full_path = os.path.join(root, fname)
                        mtime = os.path.getmtime(full_path)
                        matches.append((full_path, mtime))
        except Exception as e:
            self.logAdded.emit(f"Directory scan error: {e}")
            return False
        
        if not matches:
            return False
        
        # Pick the most recently modified match
        matches.sort(key=lambda x: x[1], reverse=True)
        best_path = matches[0][0]
        
        self.logAdded.emit(f"Found match via scan: {os.path.basename(best_path)}")
        
        tmp_dir = tempfile.gettempdir()
        local_path = os.path.join(tmp_dir, "comfy_output.glb")
        
        try:
            shutil.copy2(best_path, local_path)
            model = GLBModel(local_path)
            self.last_downloaded_path = local_path
            self.modelReady.emit(model)
            return True
        except Exception:
            pass
            
        return False

