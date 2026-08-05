from qgis.core import QgsProject, QgsField
from qgis.PyQt.QtCore import QVariant

def categorize_accessibility():
    # ==========================================
    # 1. CONFIGURATION
    # ==========================================
    LAYER_NAME = "ward_access"
    SOURCE_FIELD = "accessibility_2sfca"
    TARGET_FIELD = "access_level"  # Name of the new column to be created

    print(f"Starting classification on layer: '{LAYER_NAME}'...")

    # Fetch the layer
    layers = QgsProject.instance().mapLayersByName(LAYER_NAME)
    if not layers:
        print(f"[!] Error: Layer '{LAYER_NAME}' not found in the Layers panel.")
        return
    layer = layers[0]

    if layer.fields().indexOf(SOURCE_FIELD) == -1:
        print(f"[!] Error: Field '{SOURCE_FIELD}' not found in the layer.")
        return

    # ==========================================
    # 2. FIND MIN & MAX FOR EQUAL INTERVALS
    # ==========================================
    print("Calculating equal intervals...")
    min_val = float('inf')
    max_val = float('-inf')

    # Iterate once to find the absolute min and max values
    for feat in layer.getFeatures():
        val = feat[SOURCE_FIELD]
        if val is not None and str(val).strip() != "":
            try:
                num_val = float(val)
                if num_val < min_val: min_val = num_val
                if num_val > max_val: max_val = num_val
            except ValueError:
                pass

    if min_val == float('inf'):
        print("[!] Error: No valid numeric data found in the source column.")
        return

    # Calculate the three equal interval breakpoints
    interval = (max_val - min_val) / 3.0
    break1 = min_val + interval
    break2 = break1 + interval

    print(f"  -> Range Found: {min_val:.4f} to {max_val:.4f}")
    print(f"  -> Threshold 1 [No Access]: <= {break1:.4f}")
    print(f"  -> Threshold 2 [Partial Access]: <= {break2:.4f}")
    print(f"  -> Threshold 3 [Full Access]: > {break2:.4f}")

    # ==========================================
    # 3. ADD FIELD AND CATEGORIZE
    # ==========================================
    layer.startEditing()
    
    # Create the target field if it doesn't already exist
    target_idx = layer.fields().indexOf(TARGET_FIELD)
    if target_idx == -1:
        print(f"Creating new field '{TARGET_FIELD}'...")
        layer.addAttribute(QgsField(TARGET_FIELD, QVariant.String, len=50))
        layer.updateFields()
        target_idx = layer.fields().indexOf(TARGET_FIELD)

    print("Populating categories...")
    update_count = 0

    # Iterate again to assign categories based on the calculated intervals
    for feat in layer.getFeatures():
        val = feat[SOURCE_FIELD]
        
        if val is not None and str(val).strip() != "":
            try:
                num_val = float(val)
                
                # Apply categorization logic
                if num_val <= break1:
                    category = "No Access"
                elif num_val <= break2:
                    category = "Partial Access"
                else:
                    category = "Full Access"
                    
                layer.changeAttributeValue(feat.id(), target_idx, category)
                update_count += 1
            except ValueError:
                pass

    # ==========================================
    # 4. SAVE CHANGES
    # ==========================================
    layer.commitChanges()
    print(f"\nSuccess! Categorized {update_count} features into the '{TARGET_FIELD}' column.")

# Execute the tool directly
categorize_accessibility()