import pandas as pd
import os

BASE = '/Users/peterjiang/Library/Mobile Documents/com~apple~CloudDocs/Documents/trae_projects/TrueTemp'

for fname in ['50D6_Data_241147.xlsx', '50D6_Data_241229.xlsx', '50D6_Data_241355.xlsx', '50D6_Data_241702.xlsx']:
    fp = os.path.join(BASE, 'data', fname)
    xl = pd.ExcelFile(fp)
    print(f'=== {fname} ===')
    print(f'  Sheets: {xl.sheet_names}')
    for s in xl.sheet_names:
        df = pd.read_excel(fp, sheet_name=s)
        print(f'  Sheet [{s}]: shape={df.shape}, cols={df.columns.tolist()}')
        print(f'    head: {df.head(2).to_dict(orient="records")}')
        print(f'    tail: {df.tail(2).to_dict(orient="records")}')
    print()
