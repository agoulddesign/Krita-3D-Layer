# ComfyUI Workflows for Krita 3D Layer

This directory contains example workflows optimized for use with the **Krita 3D Layer** plugin. These workflows allow you to generate 3D models (`.glb`) directly from Krita using Image-to-3D models.

### ⚠️ IMPORTANT: Image Preparation

For the best 3D generation results, please note the following:

- **Alpha Channel Required**: The plugin sends the active layer. For local generation and most API workflows, the image **MUST** have a transparent background.
- **Centering & Lighting**: Ensure your subject is centered in the layer and well-lit.
- **Model Limitations**: Current image-to-3D models struggle with thin/small parts (like antennae), fine hair, or transparent materials.

#### Automatic Background Removal Tools:
- **Remote (Low CPU/RAM)**: [Krita Background Remove (Bria)](https://github.com/agoulddesign/krita-bg-remove-bria) - Highest quality/accuracy, recommended for machines with low specs, requires API key.
- **Local (GPU recommended, slow with CPU)**: [Krita Vision Tools](https://github.com/Acly/krita-vision-tools) - High-quality local background removal.

## 🚀 Setup Requirements

To use these workflows successfully, you must have ComfyUI installed and running.

### 1. Export in API Format
The plugin requires workflows in **API Format** (a machine-readable JSON).

**Recent ComfyUI Versions:**
1. Open the **Menu** (top-left).
2. Go to **File** -> **Export (API)**.

**Older Versions (Developer Mode):**
1. Click the **Settings** (gear icon).
2. Enable **Enable Dev mode**.
3. Use the **Save (API Format)** button that appears in the main menu.

### 2. Node Naming Conventions
The plugin identifies where to inject the Krita image and where to find the 3D model based on node titles and types.

#### **Input (The Image)**
The plugin looks for a "Load Image" node to inject the active Krita layer/selection.
- **Requirement**: You must have the [comfyui-tooling-nodes](https://github.com/Acly/comfyui-tooling-nodes) (External Tooling Nodes) extension installed in ComfyUI. This provides the custom nodes needed for Base64 image injection.
- **Recommended**: Change the Title of your Load Image node to **`Krita_Input`** (Right-click node -> Title).
- **Fallback**: If no node is titled `Krita_Input`, the plugin will attempt to find any node of type `LoadImage` or `ETN_LoadImageBase64`.

#### **Output (The 3D Model)**
The plugin automatically scans the execution history for nodes that output a `.glb` file.
- **Improved Selection**: if your workflow generates multiple meshes (previews, low-poly, etc.), you can explicitly tell the plugin which one to import.
- **Recommended**: Change the Title of your final save/output node to **`Krita_Output`** (Right-click node -> Title).
- **Suffix Handling**: If no `Krita_Output` is found, or if a node outputs multiple files, the plugin will automatically pick the one with the highest numeric suffix (e.g., `model_00002.glb` over `model_00001.glb`).
- **Fallback**: As a last resort, the plugin picks the GLB from the node that was executed last in the workflow.

### 3. Server Address
By default, the plugin connects to `http://127.0.0.1:8188`. You can change this in the **Generate** tab of the Krita 3D Docker if your ComfyUI is running on a different port or machine.

---

## 📦 Included Example Workflows

### 1. `Krita_3D_Layer_api_tripo_image_to_model.json`
- **Model**: Uses the [Tripo AI API](https://www.tripo3d.ai/).
- **Requirements**: You can use the [ComfyUI Partner Nodes](https://github.com/Comfy-Org/comfyui-partner-nodes) (specifically Tripo nodes) and must be logged into your ComfyUI account, as well as sufficient credits on your account. This can also be configured with any API nodes (it's recommended to start with an exiting workflow and replace the input with the "Krita_Input" node mentioned above)
- **Speed**: Relatively fast (Cloud-based generation).

### 2. `Krita_3D_Layer_local_SF3D.json`
- **Model**: StableFast3D (SF3D).
- **Requirements**: Requires a GPU with at least 8GB-12GB VRAM. You need the `ComfyUI-StableFast3D` custom nodes. This is currently very difficult to configure and API generation is recommended unless you are very familiar with configuring ComfyUI. If you are adventurous and feel like getting your hands dirty, see https://github.com/MrForExample/ComfyUI-3D-Pack or https://github.com/YanWenKun/Comfy3D-WinPortable for fast but low quality 3D models, and for higher quality results but slow generation time: https://github.com/visualbruno/ComfyUI-Trellis2
- **Speed**: Moderate to very slow (Local generation).

---

## 🛠 Troubleshooting

- **"JSON is in 'Workflow' format"**: This means you saved using the regular "Save" or "Export" button. Please use the **Export(API)** button.
- **"Could not find 'Krita_Input'"**: Ensure your input node is titled exactly `Krita_Input`.
- **"No .glb references found"**: Ensure your workflow actually produces a `.glb` file. Some nodes might output `.obj` or `.ply`, which are currently not supported for automatic import via the ComfyUI bridge.
