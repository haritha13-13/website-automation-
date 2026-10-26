"""Create examples/sample_content.xlsx pointing at the local mock CMS (run from the project root).
Titles get a timestamp so each new file is not treated as a duplicate of an earlier run."""
from datetime import datetime
from pathlib import Path

import pandas as pd

root = Path(__file__).resolve().parent.parent
url = (root / "tests" / "fixtures" / "mock_cms_publish.html").as_uri()
img = root / "examples" / "images"
img.mkdir(exist_ok=True)
(img / "automation.png").write_bytes(__import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
stamp = datetime.now().strftime("%d %b %H:%M")  # makes every generated file unique
rows = [
    {"website_url": url, "title": f"Healthy Habits for Busy Professionals ({stamp})",
     "content": "Short walks, regular water breaks and a fixed sleep schedule improve focus.",
     "category": "News", "tags": "health, habits", "images": "images/automation.png", "publish_mode": "draft"},
    {"website_url": url, "title": f"Five Python Tricks Every Developer Should Know ({stamp})",
     "content": "List comprehensions, f-strings and dataclasses make Python code shorter and clearer.\nTry them in your next project.",
     "category": "Technology", "tags": "python, coding", "images": "", "publish_mode": "draft"},
    {"website_url": url, "title": f"City Marathon Registration Opens ({stamp})",
     "content": "Registration for the annual city marathon opens on Monday.\nSpaces are limited.",
     "category": "News", "tags": "sports, marathon", "images": "", "publish_mode": "publish"},
    {"website_url": url, "title": "", "content": "A row with no title", "category": "", "tags": "", "images": "",
     "publish_mode": "draft"},
]
out = root / "examples" / "sample_content.xlsx"
pd.DataFrame(rows).to_excel(out, index=False)
print(out)
