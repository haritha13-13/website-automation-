"""Create examples/behance_sample.xlsx: one Behance project with a backlink (run from the project root).

Edit the file before using it: the images must exist, `blog_url` is where the backlink points, and `anchor_text`
must appear word-for-word inside `description`. publish_mode=publish means the agent fills everything and WAITS for you
to click Publish yourself in the browser."""
import base64
from datetime import datetime
from pathlib import Path

import pandas as pd

root = Path(__file__).resolve().parent
img = root / "images"
img.mkdir(exist_ok=True)
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
for name in ("project1.png", "cover.png"):
    (img / name).write_bytes(png)  # placeholders: replace with your real images
stamp = datetime.now().strftime("%d %b %H:%M")
row = {
    "website_url": "https://www.behance.net/",
    "title": f"Brand Identity Case Study ({stamp})",
    "description": "A look at how we designed a new brand identity. Read the full case study on our blog for the process.",
    "anchor_text": "full case study",
    "blog_url": "https://example.com/blog/brand-identity-case-study",
    "images": "images/project1.png",
    "cover_image": "images/cover.png",
    "category": "Graphic Design",
    "tags": "branding, identity, design",
    "publish_mode": "publish",
}
out = root / "behance_sample.xlsx"
pd.DataFrame([row]).to_excel(out, index=False)
print(out)
