# Krita 3D Layer Plugin ALPHA

A simple Krita plugin that integrates 3D models (`.obj` and `.glb`) into your 2D painting workflow. It provides a hardware-accelerated 3D viewport, allowing you to manipulate and link 3D models directly to your Krita layers. Additionally, it features direct integration with ComfyUI for generating 3D models straight from your Krita canvas!

## ✨ Features

- **Layer-Linked 3D State:** Import and position your 3D model and click "Apply Layer" button and the plugin automatically bakes the 3D state (model path, camera angles, rotations) into the `.kra` document registry.
- **Direct Layer Editing:** Tweak a previously baked layer and sync it to overwrite your changes instantly, without cluttering the layer stack. Let the plugin remember your exact camera angles for each model!
- **Walk and Orbit Camera Modes:** Freely inspect models using standard orbit rotations or immersive first-person "Walk" controls (using the mouse to look, strafe, and zoom).
- **In-Canvas Sync:** Projects your 3D view into a dedicated layer within Krita, keeping the layer name bound to the model.
- **Generative 3D (ComfyUI Integration):** Send your active Krita drawing directly to ComfyUI image-to-3D workflows (like StableFast3D or Tripo AI API). Once generated, the `.glb` model is automatically downloaded and loaded into your Krita 3D viewport.

## 🚀 Installation

1. Download the latest release `.zip` file.
2. Open Krita and go to **Tools -> Scripts -> Import Python Plugin from File...**
3. Select the downloaded `.zip` file and click **OK**.
4. Restart Krita.
5. Go to **Settings -> Configure Krita... -> Python Plugin Manager** and ensure **Krita 3D Layer** is enabled.
6. Restart Krita to load the docker. You can then find it under **Settings -> Dockers -> 3D Layer**.

## 🎮 Usage

### 3D Models (File Tab)
1. Open the **3D Layer** Docker and navigate to the **File** tab.
2. Click **Load Model** to select a `.glb` or `.obj` file.
3. Use your mouse to rotate (Left Click), pan (Middle Click or Shift+Left Click), and zoom (Scroll Wheel) the model.
4. Switch to **Walk** mode to explore large architectural scenes or environments in first-person.
5. The plugin should automatically sync the layer to canvas, but if it doesn't click **Sync to Canvas** to apply the current view to your active Krita layer.
6. Click on previously baked layer to restore the model and camera angles. Select a non-baked layer before importing a new model or the plugin will overwrite the previous model already baked into that layer

** If your model is imported in at an unexpected orientation, you can A) rotate to the desired position and click "Set Axis" or B) change the model offset X/Y/Z rotation in options. (I had to re-map the OpenGL Y-axis up to achive a standard Z-axis up, and my logic may not be 100% correct)
** The current model import function only supports one mesh, so if your mesh is segmented, or contains many individual objects, it's recommended to separate them and import individually, or do something like "Join" all the objects in Blender before exporting. This is still hit or miss and I plan on addressing the issue in future releases.

### ComfyUI Generating (Generate Tab)
*Please see the `example_comfyui_workflows/readme.md` for specific ComfyUI setup instructions.*
1. In the **Generate** tab of the docker, ensure your ComfyUI server address is correct (default: `http://127.0.0.1:8188`).
2. Click **Load Workflow** to select a ComfyUI workflow JSON (Must be in API format).
3. Draw an object on your active Krita layer, ensuring it has a transparent background (alpha channel), and is clear and well-lit.
4. Click **Generate 3D Model**. The plugin will inject the layer image into your ComfyUI workflow.
5. Once generation completes, the `.glb` will be downloaded and loaded into the viewport automatically! Click **Save Generated Model...** to store it on your disk.

## 📦 Dependencies

**If you are installing via `git clone`, you will need to manually ensure OpenGL libraries (e.g., PyOpenGL) are available in your environment. For the best experience, it is highly recommended to use the latest release ZIP which comes with all necessary libraries pre-bundled.**

## Compatability

Currently only tested on Windows 10/11 and Krita versions 5.2.11 and 5.2.14. Please report any incompatibility, bugs or odd behavior. THIS IS STILL AN ALPHA RELEASE, so don't expect perfection.

## 🤝 Acknowledgements & Credits

This plugin is made possible by several incredible open-source projects:

- **[OpenGL](https://www.opengl.org/)**: The cross-platform graphics API used for our hardware-accelerated 3D viewport.
- **[comfyui-tooling-nodes](https://github.com/Acly/comfyui-tooling-nodes)**: Created by Acly, essential for high-speed image injection.

## 📝 License

This project is licensed under the **GNU General Public License v3.0 (GPLv3)** - see the [LICENSE](LICENSE) file for details.
