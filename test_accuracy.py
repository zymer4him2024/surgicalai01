"""Quick PT vs HEF accuracy comparison."""
import os

frame = "test_frame.jpg"
print(f"Frame exists: {os.path.exists(frame)}, size: {os.path.getsize(frame) if os.path.exists(frame) else 0}")

from ultralytics import YOLO

model = YOLO("SurgeoNet/yolo/runs/pose/m_640/weights/best.pt")
print(f"Model loaded — task: {model.task}, classes: {len(model.names)}")
print(f"Class names: {list(model.names.values())}")

results = model.predict(frame, conf=0.1, verbose=False)
print(f"\nDetections (conf >= 0.1): {len(results[0].boxes)}")
for box in results[0].boxes:
    cls = int(box.cls[0])
    name = model.names[cls]
    conf = float(box.conf[0])
    bbox = box.xyxy[0].tolist()
    print(f"  {name}: {conf:.1%}  bbox={[round(x) for x in bbox]}")

if len(results[0].boxes) == 0:
    print("\nNo detections — possible causes:")
    print("  1. test_frame.jpg is empty or corrupt")
    print("  2. Model was trained on different image domain")
    print("  3. Frame resolution mismatch")
