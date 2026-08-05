from qgis.core import QgsProject, QgsField
from qgis.PyQt.QtCore import QVariant

def populate_senate_district():
    # ==========================================
    # 1. CONFIGURATION
    # ==========================================
    POINT_LAYER_NAME = "conformed_layers — facilities"  
    TABLE_LAYER_NAME = "lga_senatorial_crosswalk"        
    
    JOIN_FIELD = "state_code"
    TARGET_FIELD = "state_name"

    project = QgsProject.instance()
    point_layers = project.mapLayersByName(POINT_LAYER_NAME)
    table_layers = project.mapLayersByName(TABLE_LAYER_NAME)

    if not point_layers or not table_layers:
        print("\n[!] Error: Could not find one or both layers. Please verify the layer names.")
        return

    point_layer = point_layers[0]
    table_layer = table_layers[0]

    # ==========================================
    # 2. BUILD JOIN DICTIONARY
    # ==========================================
    print(f"\nReading '{TABLE_LAYER_NAME}' into memory...")
    
    join_mapping = {}
    for feat in table_layer.getFeatures():
        if feat[JOIN_FIELD]: 
            join_mapping[feat[JOIN_FIELD]] = feat[TARGET_FIELD]

    # ==========================================
    # 3. UPDATE POINT LAYER
    # ==========================================
    print(f"Starting edit session on '{POINT_LAYER_NAME}'...")
    point_layer.startEditing()

    field_idx = point_layer.fields().indexOf(TARGET_FIELD)
    if field_idx == -1:
        print(f"  -> Adding missing field '{TARGET_FIELD}' to point layer...")
        point_layer.addAttribute(QgsField(TARGET_FIELD, QVariant.String, len=100))
        point_layer.updateFields()
        field_idx = point_layer.fields().indexOf(TARGET_FIELD)

    print("Populating attributes based on matching sen_code...")
    update_count = 0
    
    for point_feat in point_layer.getFeatures():
        code = point_feat[JOIN_FIELD]
        
        if code in join_mapping:
            point_layer.changeAttributeValue(point_feat.id(), field_idx, join_mapping[code])
            update_count += 1

    # ==========================================
    # 4. SAVE EDITS
    # ==========================================
    print("Committing changes to the layer...")
    point_layer.commitChanges()
    
    print(f"\nSuccess! Populated {update_count} point features with '{TARGET_FIELD}' data.")

# Call the function directly so QGIS actually runs it
populate_senate_district()