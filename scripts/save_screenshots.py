from PIL import Image, ImageDraw, ImageFilter
import os

SRC = r"C:\Users\azfar\.claude\uploads\5ffe64dc-1012-4061-94ec-6b2463a9129e"
DST = r"C:\Users\azfar\metamcp-local\assets\screenshots"

FILES = {
    "01-home.jpg":                  "3095ba00-1000371237.jpg",
    "02-docker.jpg":                "7a651cbc-1000371238.jpg",
    "03-processes.jpg":             "14c6424f-1000371239.jpg",
    "04-http-endpoints.jpg":        "606ce027-1000371240.jpg",
    "05-updates.jpg":               "0e5df02b-1000371241.jpg",
    "06-memories.jpg":              "2dd02219-1000371242.jpg",
    "07-reminders.jpg":             "72516a44-1000371243.jpg",
    "08-shortcuts.jpg":             "57effe30-1000371244.jpg",
    "09-shortcuts-finance.jpg":     "e3f73dc0-1000371245.jpg",
    "10-settings.jpg":              "d9774d0a-1000371246.jpg",
    "11-settings-bottom.jpg":       "14458c34-1000371247.jpg",
    "12-openclaw-config.jpg":       "7ff26e92-1000371248.jpg",
    "13-openclaw-config-bottom.jpg":"40059578-1000371249.jpg",
    "14-skills.jpg":                "4b358d79-1000371250.jpg",
    "15-skills-credentials.jpg":    "26be13ea-1000371251.jpg",
    "16-active-sessions.jpg":       "618f56f0-1000371252.jpg",
    "17-select-model.jpg":          "8d8ed8b8-1000371253.jpg",
}

BG = (18, 20, 30)  # app background colour

def redact(img, box):
    """Draw a solid dark rectangle over sensitive text."""
    draw = ImageDraw.Draw(img)
    draw.rectangle(box, fill=BG)

def blur_region(img, box):
    """Pixelate a region (resize-down then resize-up) — guaranteed to work."""
    region = img.crop(box)
    rw, rh = region.size
    small = region.resize((max(1, rw // 15), max(1, rh // 15)), Image.NEAREST)
    pixelated = small.resize((rw, rh), Image.NEAREST)
    img.paste(pixelated, (box[0], box[1]))

os.makedirs(DST, exist_ok=True)

for name, src_file in FILES.items():
    path = os.path.join(SRC, src_file)
    img = Image.open(path).convert("RGB")
    w, h = img.size
    print(f"{name}  ({w}x{h})")

    if name == "16-active-sessions.jpg":
        # Redact entire session card — IPs, device names, timestamps
        redact(img, (0, 280, w, 1300))

    elif name == "06-memories.jpg":
        # Redact everything below GitHub card to bottom of image
        redact(img, (0, 900, w, h))

    img.save(os.path.join(DST, name), "JPEG", quality=88)
    print(f"  -> saved")

print("\nDone.")
