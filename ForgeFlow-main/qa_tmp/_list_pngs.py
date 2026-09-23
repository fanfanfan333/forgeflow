import pathlib

d = pathlib.Path("docs/sop/shots")
files = sorted(d.glob("*.png"))
print("shots dir:", d.resolve(), "| exists:", d.exists(), "| png count:", len(files))
for f in files:
    print(f"{f.name:28s} {f.stat().st_size:>9d} bytes  {f.stat().st_mtime}")
