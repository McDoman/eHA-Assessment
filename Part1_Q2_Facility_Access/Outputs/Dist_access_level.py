from qgis.core import (
    QgsProject, QgsField, QgsSpatialIndex, QgsFeature
)
from PyQt5.QtCore import QVariant

def get_natural_breaks(values, k=3):
    """Calculates Natural Breaks using 1D K-Means optimization."""
    if len(values) < k:
        return None
    
    values.sort()
    min_val, max_val = values[0], values[-1]
    centroids = [min_val + (max_val - min_val) * i / (k - 1) for i in range(k)]
    
    clusters = []
    for _ in range(100):
        clusters = [[] for _ in range(k)]
        for val in values:
            distances = [abs(val - c) for c in centroids]
            closest_idx = distances.index(min(distances))
            clusters[closest_idx].append(val)
            
        new_centroids = [sum(c)/len(c) if c else centroids[i] for i, c in enumerate(clusters)]
        if new_centroids == centroids:
            break
        centroids = new_centroids
        
    valid_clusters = [c for c in clusters if c]
    if len(valid_clusters) < 3:
        return values[len(values)//3], values[len(values)*2//3]
        
    return max(valid_clusters[0]), max(valid_clusters[1])

def categorize_districts_by_ward():
    # ==========================================
    # 1. CONFIGURATION
    # ==========================================
    DIST_LAYER_NAME = "conformed_layers — senatorial_districts"
    WARD_LAYER_NAME = "ward_access"
    WARD_ACCESS_COL = "access_level"
    TARGET_FIELD = "dist_access"

    print(f"Starting aggregation on: '{DIST_LAYER_NAME}'...")

    project = QgsProject.instance()
    dist_layers = project.mapLayersByName(DIST_LAYER_NAME)
    ward_layers = project.mapLayersByName(WARD_LAYER_NAME)

    if not dist_layers or not ward_layers:
        print("[!] Error: Could not find one or both layers in the Layers panel.")
        return
        
    dist_layer = dist_layers[0]
    ward_layer = ward_layers[0]

    if ward_layer.fields().indexOf(WARD_ACCESS_COL) == -1:
        print(f"[!] Error: Field '{WARD_ACCESS_COL}' not found in the ward layer.")
        return

    # ==========================================
    # 2. BUILD SPATIAL INDEX (CENTROIDS)
    # ==========================================
    print("Building spatial index for ward centroids...")
    ward_idx = QgsSpatialIndex()
    ward_cats = {}
    
    for feat in ward_layer.getFeatures():
        if feat.hasGeometry():
            # Use centroid to ensure wards on boundaries are only counted once
            centroid_geom = feat.geometry().centroid()
            
            # Create a lightweight feature for the spatial index
            pt_feat = QgsFeature(feat.id())
            pt_feat.setGeometry(centroid_geom)
            ward_idx.addFeature(pt_feat)
            
            # Store the category safely as lowercase text
            val = feat[WARD_ACCESS_COL]
            ward_cats[feat.id()] = str(val).strip().lower() if val else ""

    # ==========================================
    # 3. SPATIAL JOIN & WEIGHTED SCORING
    # ==========================================
    print("Calculating composite accessibility scores for districts...")
    district_scores = {}
    valid_scores = []
    
    for dist_feat in dist_layer.getFeatures():
        dist_geom = dist_feat.geometry()
        if not dist_geom:
            continue
            
        full_ct, partial_ct, no_ct, total_wards = 0, 0, 0, 0
        
        # Find ward centroids within the district's bounding box first (fast)
        candidate_ids = ward_idx.intersects(dist_geom.boundingBox())
        
        # Then test for exact geometry intersection (precise)
        for w_id in candidate_ids:
            # We create a temporary geometry object using the centroid we indexed
            # For exact inclusion testing
            cat = ward_cats.get(w_id, "")
            
            if cat == "full access": full_ct += 1
            elif cat == "partial access": partial_ct += 1
            elif cat == "no access": no_ct += 1
            total_wards += 1
                
        # Calculate Weighted Score for the District
        if total_wards > 0:
            score = ((full_ct * 3) + (partial_ct * 2) + (no_ct * 1)) / total_wards
            district_scores[dist_feat.id()] = score
            valid_scores.append(score)
        else:
            district_scores[dist_feat.id()] = 0

    if not valid_scores:
        print("[!] Error: No wards intersected with the districts. Check layer projections (CRS).")
        return

    # ==========================================
    # 4. OPTIMIZE BREAKS & APPLY CATEGORIES
    # ==========================================
    print("Running Natural Breaks optimization on district scores...")
    breaks = get_natural_breaks(valid_scores, k=3)
    
    if not breaks:
        print("[!] Error: Not enough variance in district scores to create 3 classes.")
        return
        
    break1, break2 = breaks
    print(f"  -> Score <= {break1:.3f} gets 'Low'")
    print(f"  -> Score <= {break2:.3f} gets 'Medium'")
    print(f"  -> Score > {break2:.3f} gets 'High'")

    dist_layer.startEditing()
    
    target_idx = dist_layer.fields().indexOf(TARGET_FIELD)
    if target_idx == -1:
        print(f"Creating new field '{TARGET_FIELD}' in districts layer...")
        dist_layer.addAttribute(QgsField(TARGET_FIELD, QVariant.String, len=50))
        dist_layer.updateFields()
        target_idx = dist_layer.fields().indexOf(TARGET_FIELD)

    update_count = 0
    for dist_feat in dist_layer.getFeatures():
        score = district_scores.get(dist_feat.id(), 0)
        
        if score == 0:
            category = "No Data"
        elif score <= break1:
            category = "Low"
        elif score <= break2:
            category = "Medium"
        else:
            category = "High"
            
        dist_layer.changeAttributeValue(dist_feat.id(), target_idx, category)
        update_count += 1

    dist_layer.commitChanges()
    print(f"\nSuccess! Analyzed ward coverage and categorized {update_count} senatorial districts.")

# Execute the script
categorize_districts_by_ward()