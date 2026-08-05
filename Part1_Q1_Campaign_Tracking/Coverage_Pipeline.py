import pandas as pd
import os

def calculate_ward_coverage():
    # ==========================================
    # 1. CONFIGURATION
    # ==========================================
    # Input CSV file path
    INPUT_CSV = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\settlement_visitation.csv"
    
    # Output Excel file path
    WORKSPACE = os.path.dirname(INPUT_CSV)
    OUTPUT_EXCEL = os.path.join(WORKSPACE, "Ward_Coverage_Summary.xlsx")
    
    # Column names
    WARD_COLUMN = "ward_name"       # UPDATE THIS to your actual ward column name (e.g., 'Ward', 'ward_id')
    VIS_STATUS_COL = "Vis_Status"   # The column containing the visitation status
    VISITED_FLAG = "V"              # The exact text denoting a visited settlement
    
    print(f"Reading data from: {INPUT_CSV}")

    # ==========================================
    # 2. LOAD THE DATA
    # ==========================================
    try:
        df = pd.read_csv(INPUT_CSV)
    except Exception as e:
        print(f"[!] Error loading CSV file: {e}")
        return

    # Verify required columns exist before proceeding
    if WARD_COLUMN not in df.columns or VIS_STATUS_COL not in df.columns:
        print(f"[!] Error: Missing required columns.")
        print(f"Available columns in your CSV: {list(df.columns)}")
        print(f"Please update 'WARD_COLUMN' in the script to perfectly match your data.")
        return

    # ==========================================
    # 3. DATA AGGREGATION
    # ==========================================
    print("Calculating ward-level summaries...")
    
    # Create a binary column: 1 if Visited, 0 otherwise
    # .strip() is used to catch any accidental spaces like "V " in the CSV
    df['Is_Visited'] = df[VIS_STATUS_COL].apply(lambda x: 1 if str(x).strip() == VISITED_FLAG else 0)

    # Group by Ward and aggregate
    summary_df = df.groupby(WARD_COLUMN).agg(
        Total_Settlements=(VIS_STATUS_COL, 'count'),
        Visited_Settlements=('Is_Visited', 'sum')
    ).reset_index()

    # ==========================================
    # 4. CALCULATE PERCENTAGE
    # ==========================================
    # Calculate percentage and round to 2 decimal places
    summary_df['Coverage_Percentage'] = (summary_df['Visited_Settlements'] / summary_df['Total_Settlements']) * 100
    summary_df['Coverage_Percentage'] = summary_df['Coverage_Percentage'].round(2)

    # Sort descending by coverage for a cleaner final report
    summary_df = summary_df.sort_values(by='Coverage_Percentage', ascending=False)

    # ==========================================
    # 5. EXPORT TO EXCEL
    # ==========================================
    print(f"Exporting summary to Excel...")
    try:
        summary_df.to_excel(OUTPUT_EXCEL, index=False, sheet_name="Ward Coverage")
        print(f"\nSuccess! Summary saved to:\n  {OUTPUT_EXCEL}")
        
        # Print a quick preview to the terminal
        print("\n--- Output Preview ---")
        print(summary_df.head())
        
    except Exception as e:
        print(f"[!] Error saving Excel file: {e}")

if __name__ == "__main__":
    calculate_ward_coverage()