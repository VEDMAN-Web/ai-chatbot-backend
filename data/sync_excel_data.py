import openpyxl
import json
import sys

# Load existing seed
with open('data/properties_seed.json', 'r', encoding='utf-8') as f:
    properties = json.load(f)

# Load excel data
wb = openpyxl.load_workbook('data/tajlandia-com-2026-09-22.xlsx', data_only=True)
sheet = wb['Sheet1']
rows = list(sheet.iter_rows(values_only=True))
headers = rows[0]

excel_items = []
for r in rows[1:]:
    item = dict(zip(headers, r))
    excel_items.append(item)

excel_map = {item['data'].strip().lower(): item for item in excel_items}

updated_count = 0
for p in properties:
    name_key = p['property_name'].strip().lower()
    match = None
    for k in excel_map:
        if k in name_key or name_key in k:
            match = excel_map[k]
            break
    if match:
        img_url = match.get('image')
        if img_url and isinstance(img_url, str) and img_url.strip():
            p['images'] = [img_url.strip()]
            p['image_count'] = 1
            p['image_host'] = 'tajlandia.com'
        else:
            p['images'] = []
            p['image_count'] = 0
            p['image_host'] = ''
        
        if match.get('data4'):
            p['property_details']['area'] = f"{match['data4']} sqm"
        if match.get('data5'):
            p['property_details']['bedrooms'] = str(match['data5'])
        if match.get('data6'):
            p['status'] = str(match['data6'])
        if match.get('data8'):
            p['property_type'] = str(match['data8'])
        if match.get('data2'):
            p['description'] = str(match['data2'])
        updated_count += 1

with open('data/properties_seed.json', 'w', encoding='utf-8') as f:
    json.dump(properties, f, indent=2, ensure_ascii=False)

print(f"Successfully updated {updated_count} properties.")
for p in properties:
    has_img = len(p['images']) > 0
    img_preview = p['images'][0] if has_img else "No image published (Photos coming soon)"
    print(f"[{p['property_id']}] {p['property_name']} -> {img_preview}")
