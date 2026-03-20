# Bringel Cylinder Detection Strategy

**Version**: 1.0
**Target Hardware**: Raspberry Pi 5 + Hailo-8 AI Accelerator
**Camera Input**: Stationary 1080p HD Camera (1920x1080)

---

## 1. Core Challenge & Approach

The primary challenge in counting tightly packed gas cylinders is extreme visual occlusion and dense bounding box overlap. If an AI is trained to detect the "whole cylinder," the NMS (Non-Maximum Suppression) algorithm will misinterpret overlapping background cylinders as duplicate detections and aggressively delete them, leading to severe under-counting.

**The Solution: Feature-Specific Detection**
Instead of the whole cylinder, the model will be trained exclusively to detect the **silver valve and cap assembly** at the top of the cylinder. 
- The physical shoulders of the cylinders enforce separation between the valves.
- Even in densely packed rows, the valves remain distinctly visible to a downward-angled camera.
- Standard YOLO object detection paired with standard NMS thresholding works exceptionally well on small, separated features like these.

---

## 2. Stationary 1080p Camera Advantages

A stationary 1080p camera solves multiple computer vision problems simultaneously:

1. **Consistent Scale/Perspective**: 
   The AI learns exactly how large a front-row valve is compared to a back-row valve. This geometrical consistency eliminates false positives from background clutter.
2. **High-Resolution Detail**: 
   The 1080p resolution (1920x1080) ensures that even the smallest valves in the furthest rows contain enough pixels for YOLO to extract features confidently.
3. **Static Region of Interest (ROI) Cropping**: 
   Because the camera does not move, the top non-relevant portion of the frame (walls, ceiling) can be statically cropped out *before* inference. This guarantees that background pipes or fixtures are never mistakenly counted as valves, while significantly reducing the computational load on the NPU.

---

## 3. Data Collection & Labeling Guidelines

To train an accurate model for the Hailo-8, follow these strict labeling rules:

- **Angle**: All training images must be captured from the exact stationary mount point where the camera will be deployed.
- **Lighting**: Capture diverse lighting conditions (e.g., morning glare, artificial lights, dim ambient light) to ensure model resilience.
- **Bounding Boxes**: 
  - Draw tight boxes *only* around the metallic valve and top handles.
  - **IGNORE** the brown cylinder body and the white protective netting.
- **Class Label**: Use a single class named `cylinder_valve`.

---

## 4. Hardware Deployment & Compilation (Hailo-8)

1. **Model Selection**: YOLOv8 or YOLOv11 (both supported by the Hailo Dataflow Compiler).
2. **Inference Resolution**: Train and compile the model at a higher internal resolution (e.g., `640x640` or `960x960`) rather than a smaller default size. The Hailo-8 has 26 TOPS and can handle `640x640` in real-time. This prevents the small back-row valves from being downsampled and lost in the convolution layers.
3. **Implementation Pipeline**:
   - `camera_agent` captures 1080p frame.
   - Pre-process: Crop out the top 20-30% of the image (static wall/ceiling).
   - `inference_agent` runs the compiled `.hef` file on the Hailo-8.
   - The total cylinder count is the length of the returned bounding box array.
