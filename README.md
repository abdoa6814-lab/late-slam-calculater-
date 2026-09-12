# LS Deep Dive Tool v3
Standalone web app matching the supplied reference workbook's six sheet names and column structures.

Inputs:
- Late Slam Raw Data CSV
- Inventory Audit CSV
- Store Code (optional)

Output:
LS_DeepDive_<store>_<date>.xlsx

Sheets:
1. All Orders Raw
2. Late Slams Only
3. Hourly Breakdown
4. Slow Picks Detail
5. Aisle Contribution
6. Aisle Raw Data

Render:
Build command: pip install -r requirements.txt
Start command: gunicorn app:app
