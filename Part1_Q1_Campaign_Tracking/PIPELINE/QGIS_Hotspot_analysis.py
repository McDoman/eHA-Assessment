import os
import math
from PyQt5.QtGui import QColor
from PyQt5.QtCore import QVariant
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsSpatialIndex,
    QgsVectorFileWriter, QgsRuleBasedRenderer, QgsSymbol
)
from qgis.utils import iface

def run_qgis_hotspot_analysis():
    # ==========================================
    # 1. CONFIGURATION & FILE PATHS
    # ==========================================
    INPUT_SHP = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\settlement_visitation.shp"
    
    WORKSPACE = os.path.dirname(INPUT_SHP)
    OUTPUT_SHP = os.path.join(WORKSPACE, "missed_settlements_hotspots_qgis.shp")
    
    print(f"Starting QGIS Hot Spot Analysis...")
    print(f"Reading: {INPUT_SHP}")
    
    # Load the input layer in the background
    input_layer = QgsVectorLayer(INPUT_SHP, "Original Settlements", "ogr")
    if not input_layer.isValid():
        print("[!] Error: Could not load the input shapefile. Please check the path.")
        return

    # ==========================================
    # 2. CREATE OUTPUT COPY
    # ==========================================
    print("Creating output shapefile...")
    # Use V2 write options for compatibility with QGIS 3.x
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile"
    options.fileEncoding = "UTF-8"
    
    QgsVectorFileWriter.writeAsVectorFormatV2(
        input_layer, 
        OUTPUT_SHP, 
        QgsProject.instance().transformContext(), 
        options
    )
    
    # Load the newly created layer into the current QGIS Project Map
    layer = QgsVectorLayer(OUTPUT_SHP, "Missed Settlements Hot Spots", "ogr")
    if not layer.isValid():
        print("[!] Error: Could not load the exported output shapefile.")
        return
        
    QgsProject.instance().addMapLayer(layer)

    # ==========================================
    # 3. SPATIAL & STATISTICAL PREPARATION
    # ==========================================
    print("Building spatial index and computing global statistics...")
    k_neighbors = 8  # Fixed nearest neighbors to consider
    W = k_neighbors
    
    features = {f.id(): f for f in layer.getFeatures()}
    n = len(features)
    
    if n <= W:
        print(f"[!] Not enough features ({n}) to calculate neighbors ({W}).")
        return
        
    spatial_index = QgsSpatialIndex(layer.getFeatures())
    
    sum_x = 0
    sum_x2 = 0
    x_values = {}
    
    for fid, feat in features.items():
        # Convert categorical to Binary: Missed (NV) = 1, Visited (V) = 0
        val = 1 if feat["Vis_Status"] == 'NV' else 0
        x_values[fid] = val
        sum_x += val
        sum_x2 += (val * val)
        
    mean_x = sum_x / n
    variance = (sum_x2 / n) - (mean_x * mean_x)
    S = math.sqrt(variance) if variance > 0 else 0

    denominator = 0
    if S > 0:
        radicand = (n * W - (W * W)) / (n - 1)
        if radicand > 0:
            denominator = S * math.sqrt(radicand)

    # ==========================================
    # 4. ADD FIELDS & CALCULATE Gi* Z-SCORE
    # ==========================================
    print("Calculating Getis-Ord Gi* Z-Scores...")
    layer.startEditing()
    
    fields_to_add = [
        QgsField("Missed_Num", QVariant.Int),
        QgsField("Gi_ZScore", QVariant.Double),
        QgsField("Hotspot", QVariant.String)
    ]
    
    for field in fields_to_add:
        if layer.fields().lookupField(field.name()) == -1:
            layer.addAttribute(field)
            
    layer.updateFields()
    
    idx_num = layer.fields().lookupField("Missed_Num")
    idx_z = layer.fields().lookupField("Gi_ZScore")
    idx_hot = layer.fields().lookupField("Hotspot")

    for fid, feat in features.items():
        geom = feat.geometry()
        
        # nearestNeighbor fetches the closest polygons based on centroids
        neighbors = spatial_index.nearestNeighbor(geom.centroid().asPoint(), k_neighbors)
        
        sum_neighbor_x = sum([x_values.get(nid, 0) for nid in neighbors])
        
        z_score = 0
        if denominator > 0:
            numerator = sum_neighbor_x - (mean_x * W)
            z_score = numerator / denominator
            
        # Standard Normal Distribution confidence tag (95% threshold)
        if z_score > 1.96:
            hotspot_tag = "Hot Spot (Missed)"
        elif z_score < -1.96:
            hotspot_tag = "Cold Spot (Visited)"
        else:
            hotspot_tag = "Not Significant"
            
        layer.changeAttributeValue(fid, idx_num, x_values[fid])
        layer.changeAttributeValue(fid, idx_z, round(z_score, 4))
        layer.changeAttributeValue(fid, idx_hot, hotspot_tag)

    layer.commitChanges()

    # ==========================================
    # 5. AUTOMATED SYMBOLOGY
    # ==========================================
    print("Applying symbology...")
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    renderer = QgsRuleBasedRenderer(symbol)
    root_rule = renderer.rootRule()
    root_rule.children()[0].delete() # Clear the initial blank rule

    def add_rule(label, expression, color_hex):
        rule = QgsRuleBasedRenderer.Rule(QgsSymbol.defaultSymbol(layer.geometryType()))
        rule.setLabel(label)
        rule.setFilterExpression(expression)
        rule.symbol().setColor(QColor(color_hex))
        # Remove geometry borders for a cleaner cluster map appearance
        rule.symbol().symbolLayer(0).setStrokeColor(QColor("transparent"))
        root_rule.appendChild(rule)

    add_rule("Hot Spot (Missed Clusters) [Z > 1.96]", '"Gi_ZScore" > 1.96', "#d7191c")
    add_rule("Not Significant", '"Gi_ZScore" >= -1.96 AND "Gi_ZScore" <= 1.96', "#e0e0e0")
    add_rule("Cold Spot (Visited Clusters) [Z < -1.96]", '"Gi_ZScore" < -1.96', "#2c7bb6")

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    
    if iface:
        iface.layerTreeView().refreshLayerSymbology(layer.id())

    print(f"\nSuccess! Analysis complete.")
    print(f"Hot Spot Results saved to:\n  {OUTPUT_SHP}")

# Execute the script
run_qgis_hotspot_analysis()