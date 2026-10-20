import sys, glob, os
from PIL import Image, ImageDraw
files = sorted(glob.glob(sys.argv[1]))
cols = int(sys.argv[3]) if len(sys.argv) > 3 else 4
w, h = 480, 270
rows = (len(files) + cols - 1) // cols
S = Image.new('RGB', (cols * w, rows * h))
d = ImageDraw.Draw(S)
for i, f in enumerate(files):
    im = Image.open(f).convert('RGB').resize((w, h))
    x, y = (i % cols) * w, (i // cols) * h
    S.paste(im, (x, y)); d.text((x + 5, y + 4), os.path.basename(f), fill=(255, 80, 80))
S.save(sys.argv[2])
