import json
import random
import os

CLASSES = [
    "Overholt Clamp", "Metz. Scissor", "Sur. Scissor", 
    "Needle Holder", "Sur. Forceps", "Atr. Forceps", 
    "Scalpel", "Retractor", "Hook", "Lig. Clamp", 
    "Peri. Clamp", "Bowl", "Tong"
]

OUTPUT_DIR = "test_data/surgeonet_presets"
os.makedirs(OUTPUT_DIR, exist_ok=True)

for i in range(1, 21):
    job_id = f"TEST-SURGEONET-{i:03d}"
    
    # Pick 2 to 6 random tool types for this simulated surgical tray
    num_tool_types = random.randint(2, 6)
    selected_tools = random.sample(CLASSES, num_tool_types)
    
    target_counts = {}
    for tool in selected_tools:
        # Assign a random required quantity (e.g., 1 to 4) for each selected tool
        target_counts[tool] = random.randint(1, 4) 
        
    payload = {
        "job_id": job_id,
        "target": target_counts
    }
    
    file_path = os.path.join(OUTPUT_DIR, f"{job_id}.json")
    with open(file_path, "w") as f:
        json.dump(payload, f, indent=2)

print(f"Successfully generated 20 test sets in the '{OUTPUT_DIR}' directory.")
