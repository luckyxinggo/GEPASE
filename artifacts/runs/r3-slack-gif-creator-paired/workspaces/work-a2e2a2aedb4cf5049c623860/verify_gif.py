from PIL import Image, ImageDraw, ImageFont


gif = Image.open("deployment_status.gif")
durations = []
sizes = []
frames = []
index = 0
while True:
    sizes.append(gif.size)
    durations.append(gif.info.get("duration"))
    if index in (0, 8, 16):
        frames.append(gif.convert("RGB"))
    index += 1
    try:
        gif.seek(index)
    except EOFError:
        break

assert gif.format == "GIF"
assert index == 24
assert set(sizes) == {(480, 480)}
assert set(durations) == {100}
assert sum(durations) == 2400
assert gif.info.get("loop") == 0

sheet = Image.new("RGB", (3 * 480, 520), (12, 15, 21))
for i, (label, image) in enumerate(zip(("WAITING", "PROCESSING", "COMPLETE"), frames)):
    sheet.paste(image, (i * 480, 0))
    d = ImageDraw.Draw(sheet)
    d.text((i * 480 + 14, 490), label, font=ImageFont.load_default(size=18), fill=(240, 244, 252))
sheet.save("verification_contact_sheet.png")

print("PASS: GIF 480x480, 24 frames, 100ms/frame, 2400ms total, loop=0")
