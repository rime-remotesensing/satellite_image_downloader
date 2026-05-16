import rasterio
import os

# Files in F: directory (Windows mount appears as "F:" in Linux container)
file1 = "F:/Aso_region05_Sentinel_2024-02-20_composite.tif"
file2 = "F:/sugimoto/Aso/Sentinel-2/region05/2024/snowmasked/S2C_20240220_snowmasked.tif"

# Check if F: exists
if os.path.exists("F:"):
    print(f"\nF: directory exists. Contents: {os.listdir('F:')[:10]}")
else:
    print("\nF: directory NOT found. Trying with different path...")

labels = ["Get_Sentinel2.py", "satellite_image_downloader"]
files = [file1, file2]

print("\n" + "="*70)
print("METADATA COMPARISON: Get_Sentinel2.py vs satellite_image_downloader")
print("="*70)

for label, filepath in zip(labels, files):
    print(f"\n{label}:")
    print("-" * 70)
    try:
        with rasterio.open(filepath) as src:
            print(f"  CRS:        {src.crs}")
            print(f"  Width:      {src.width}")
            print(f"  Height:     {src.height}")
            print(f"  Resolution: X={src.transform.a:.1f}, Y={src.transform.e:.1f}")
            print(f"  Origin (L,T): X={src.transform.c:.2f}, Y={src.transform.f:.2f}")
            print(f"  Bounds:     {src.bounds}")
    except FileNotFoundError as e:
        print(f"  ERROR: File not found")
        print(f"  Path: {filepath}")
    except Exception as e:
        print(f"  ERROR: {e}")

# Compare
print("\n" + "="*70)
print("COMPARISON RESULTS:")
print("="*70)

try:
    with rasterio.open(file1) as src1, rasterio.open(file2) as src2:
        crs_match = src1.crs == src2.crs
        transform_match = src1.transform == src2.transform
        size_match = (src1.width == src2.width) and (src1.height == src2.height)
        
        print(f"\nCRS Match:       {crs_match}")
        print(f"Transform Match: {transform_match}")
        print(f"Size Match:      {size_match}")
        
        if not crs_match:
            print(f"  CRS1: {src1.crs}")
            print(f"  CRS2: {src2.crs}")
        
        if not transform_match:
            print(f"  Transform1: {src1.transform}")
            print(f"  Transform2: {src2.transform}")
            
        if not size_match:
            print(f"  Size1: {src1.width}x{src1.height}")
            print(f"  Size2: {src2.width}x{src2.height}")
        
        if crs_match and transform_match and size_match:
            print("\n✓ Both files have IDENTICAL geospatial metadata")
        else:
            print("\n✗ Files have DIFFERENT geospatial metadata")

except Exception as e:
    print(f"ERROR during comparison: {e}")

