import os
import json
import struct
import tempfile

class GLBModel:
    """Parsed GLB model with vertices, normals, and face indices."""

    def __init__(self, filename):
        self.vertices = []   # list of [x, y, z]
        self.normals = []    # list of [nx, ny, nz]
        self.texcoords = []  # list of [u, v]
        self.faces = []      # list of [(v, vt, vn), ...]
        self.texture_path = None  # path to diffuse texture image
        
        try:
            self._parse(filename)
            self._center_and_scale()
        except Exception as e:
            print(f"[3D Layer] Failed to load GLB '{filename}': {e}")
            import traceback
            traceback.print_exc()

    def _parse(self, filename):
        with open(filename, 'rb') as f:
            magic = f.read(4)
            if magic != b'glTF':
                raise ValueError("Not a valid GLB file")
            
            version, length = struct.unpack('<II', f.read(8))
            
            # Read JSON chunk
            json_chunk_len, json_chunk_type = struct.unpack('<II', f.read(8))
            if json_chunk_type != 0x4E4F534A: # 'JSON'
                raise ValueError("First chunk is not JSON")
                
            json_data = f.read(json_chunk_len)
            gltf = json.loads(json_data.decode('utf-8'))
            
            # Read BIN chunk (optional but usually present for meshes)
            bin_chunk_header = f.read(8)
            if not bin_chunk_header or len(bin_chunk_header) < 8:
                bin_data = b''
            else:
                bin_chunk_len, bin_chunk_type = struct.unpack('<II', bin_chunk_header)
                if bin_chunk_type != 0x004E4942: # 'BIN\0'
                    raise ValueError("Second chunk is not BIN")
                bin_data = f.read(bin_chunk_len)
                
        self._extract_mesh(gltf, bin_data)
        self._extract_texture(gltf, bin_data, filename)

    def _get_buffer_view_data(self, gltf, bin_data, accessor_idx):
        accessor = gltf['accessors'][accessor_idx]
        buffer_view_idx = accessor.get('bufferView')
        if buffer_view_idx is None:
            return None, accessor, 0
            
        buffer_view = gltf['bufferViews'][buffer_view_idx]
        byte_offset = buffer_view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
        byte_length = buffer_view.get('byteLength', 0)
        byte_stride = buffer_view.get('byteStride', 0)
        
        return bin_data[byte_offset:byte_offset + byte_length], accessor, byte_stride

    def _extract_mesh(self, gltf, bin_data):
        if 'meshes' not in gltf:
            return
            
        # Just grab the first mesh and its first primitive
        mesh = gltf['meshes'][0]
        primitive = mesh['primitives'][0]
        attributes = primitive['attributes']
        
        # Get positions
        if 'POSITION' in attributes:
            data, accessor, stride = self._get_buffer_view_data(gltf, bin_data, attributes['POSITION'])
            if data is not None:
                count = accessor['count']
                if stride == 0: stride = 12
                for i in range(count):
                    x, y, z = struct.unpack_from('<fff', data, i * stride)
                    self.vertices.append([x, y, z])
                
        # Get normals
        if 'NORMAL' in attributes:
            data, accessor, stride = self._get_buffer_view_data(gltf, bin_data, attributes['NORMAL'])
            if data is not None:
                count = accessor['count']
                if stride == 0: stride = 12
                for i in range(count):
                    nx, ny, nz = struct.unpack_from('<fff', data, i * stride)
                    self.normals.append([nx, ny, nz])
                
        # Get texcoords
        if 'TEXCOORD_0' in attributes:
            data, accessor, stride = self._get_buffer_view_data(gltf, bin_data, attributes['TEXCOORD_0'])
            if data is not None:
                count = accessor['count']
                component_type = accessor['componentType']
                
                if component_type == 5126: # FLOAT
                    if stride == 0: stride = 8
                    fmt = '<ff'
                elif component_type == 5123: # UNSIGNED_SHORT (normalized)
                    if stride == 0: stride = 4
                    fmt = '<HH'
                elif component_type == 5121: # UNSIGNED_BYTE (normalized)
                    if stride == 0: stride = 2
                    fmt = '<BB'
                else:
                    return
                    
                for i in range(count):
                    u, v = struct.unpack_from(fmt, data, int(i * stride))
                    if component_type == 5123:
                        u, v = u / 65535.0, v / 65535.0
                    elif component_type == 5121:
                        u, v = u / 255.0, v / 255.0
                    # For GLTF/GLB, texture coordinates are top-left origin (v goes down).
                    # OpenGL expects bottom-left origin (v goes up), so we invert V.
                    v = 1.0 - v
                    self.texcoords.append([u, v])
                
        # Get indices
        if 'indices' in primitive:
            data, accessor, stride = self._get_buffer_view_data(gltf, bin_data, primitive['indices'])
            if data is not None:
                count = accessor['count']
                component_type = accessor['componentType']
                
                indices = []
                if component_type == 5123: # UNSIGNED_SHORT
                    if stride == 0: stride = 2
                    fmt = '<H'
                elif component_type == 5125: # UNSIGNED_INT
                    if stride == 0: stride = 4
                    fmt = '<I'
                elif component_type == 5121: # UNSIGNED_BYTE
                    if stride == 0: stride = 1
                    fmt = '<B'
                else:
                    return
                    
                for i in range(count):
                    idx = struct.unpack_from(fmt, data, i * stride)[0]
                    indices.append(idx)
                    
                # Convert triangles to faces
                has_normals = len(self.normals) > 0
                has_texcoords = len(self.texcoords) > 0
                
                for i in range(0, len(indices), 3):
                    face = []
                    for j in range(3):
                        idx = indices[i+j]
                        # OBJ/our renderer uses 1-based indexing for vertices
                        v_idx = idx + 1
                        t_idx = v_idx if has_texcoords else 0
                        n_idx = v_idx if has_normals else 0
                        face.append((v_idx, t_idx, n_idx))
                    self.faces.append(face)
        else:
            # Non-indexed drawing
            has_normals = len(self.normals) > 0
            has_texcoords = len(self.texcoords) > 0
            for i in range(0, len(self.vertices), 3):
                face = []
                for j in range(3):
                    v_idx = i + j + 1
                    t_idx = v_idx if has_texcoords else 0
                    n_idx = v_idx if has_normals else 0
                    face.append((v_idx, t_idx, n_idx))
                self.faces.append(face)

    def _extract_texture(self, gltf, bin_data, filename):
        if 'images' not in gltf:
            return
            
        # Find the correct image index from the material's baseColorTexture
        image_idx = 0
        if 'materials' in gltf:
            for mat in gltf['materials']:
                if 'pbrMetallicRoughness' in mat and 'baseColorTexture' in mat['pbrMetallicRoughness']:
                    tex_idx = mat['pbrMetallicRoughness']['baseColorTexture']['index']
                    if 'textures' in gltf and tex_idx < len(gltf['textures']):
                        tex = gltf['textures'][tex_idx]
                        if 'source' in tex:
                            image_idx = tex['source']
                            break
                            
        if image_idx >= len(gltf['images']):
            return
            
        image = gltf['images'][image_idx]
        if 'bufferView' in image:
            buffer_view_idx = image['bufferView']
            buffer_view = gltf['bufferViews'][buffer_view_idx]
            byte_offset = buffer_view.get('byteOffset', 0)
            byte_length = buffer_view['byteLength']
            img_data = bin_data[byte_offset:byte_offset + byte_length]
            
            # Determine extension
            mime_type = image.get('mimeType', '')
            ext = '.png' if 'png' in mime_type else '.jpg'
            
            # Write to temp file
            base_name = os.path.splitext(os.path.basename(filename))[0]
            temp_dir = tempfile.gettempdir()
            out_path = os.path.join(temp_dir, f"krita_3d_extracted_{base_name}{ext}")
            
            with open(out_path, 'wb') as f:
                f.write(img_data)
                
            self.texture_path = out_path

    def _center_and_scale(self):
        if not self.vertices:
            return

        min_v = [min(v[i] for v in self.vertices) for i in range(3)]
        max_v = [max(v[i] for v in self.vertices) for i in range(3)]
        center = [(min_v[i] + max_v[i]) / 2.0 for i in range(3)]
        max_ext = max(max_v[i] - min_v[i] for i in range(3))
        scale = 2.0 / max_ext if max_ext > 0 else 1.0

        for v in self.vertices:
            v[0] = (v[0] - center[0]) * scale
            v[1] = (v[1] - center[1]) * scale
            v[2] = (v[2] - center[2]) * scale
