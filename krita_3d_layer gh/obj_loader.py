import os

class OBJModel:
    """Parsed OBJ model with vertices, normals, and face indices."""

    def __init__(self, filename):
        self.vertices = []   # list of [x, y, z]
        self.normals = []    # list of [nx, ny, nz]
        self.texcoords = []  # list of [u, v]
        self.faces = []      # list of [(v, vt, vn), ...]
        self.texture_path = None  # path to diffuse texture image
        self.mtl_filename = None
        self.base_dir = os.path.dirname(filename)

        try:
            self._parse(filename)
            self._center_and_scale()
        except Exception as e:
            print(f"[3D Layer] Failed to load OBJ '{filename}': {e}")

    # ──────────────────────────────────────────────────────────────────────

    def _parse(self, filename):
        with open(filename, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split()
                key = parts[0]

                if key == "v" and len(parts) >= 4:
                    self.vertices.append(list(map(float, parts[1:4])))

                elif key == "vn" and len(parts) >= 4:
                    self.normals.append(list(map(float, parts[1:4])))

                elif key == "vt" and len(parts) >= 3:
                    self.texcoords.append(list(map(float, parts[1:3])))

                elif key == "f":
                    face = []
                    for token in parts[1:]:
                        w = token.split("/")
                        v_idx = int(w[0])
                        t_idx = int(w[1]) if len(w) > 1 and w[1] else 0
                        n_idx = int(w[2]) if len(w) > 2 and w[2] else 0
                        face.append((v_idx, t_idx, n_idx))
                    if face:
                        self.faces.append(face)

                elif key == "mtllib" and len(parts) >= 2:
                    self.mtl_filename = parts[1]
                    self._parse_mtl(os.path.join(self.base_dir, self.mtl_filename))

    def _parse_mtl(self, mtl_path):
        if not os.path.exists(mtl_path):
            return
        with open(mtl_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if parts[0] == "map_Kd" and len(parts) >= 2:
                    # Found diffuse texture map
                    tex_rel = parts[1]
                    # Handle paths with spaces
                    if len(parts) > 2:
                        tex_rel = " ".join(parts[1:])
                    
                    full_path = os.path.join(self.base_dir, tex_rel)
                    if os.path.exists(full_path):
                        self.texture_path = full_path
                        print(f"[3D Layer] Found texture: {self.texture_path}")

    def _center_and_scale(self):
        """Normalise the model to the range [-1, 1] on all axes."""
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
