"""Render a compact, local, non-destructive raster preview for vision analysis."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .schemas import FloorPlan

WIDTH, HEIGHT = 1600, 1200
MARGIN = 64
MAX_LABELS = 240


def render_drawing_preview(plan: FloorPlan) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    bounds = plan.bounds
    if bounds:
        min_x, min_y = bounds[0].x, bounds[0].y
        max_x, max_y = bounds[1].x, bounds[1].y
    elif plan.drawing_paths:
        points = [point for path in plan.drawing_paths for point in path.points]
        min_x, max_x = min(p.x for p in points), max(p.x for p in points)
        min_y, max_y = min(p.y for p in points), max(p.y for p in points)
    else:
        raise ValueError("图纸没有可预览的二维几何")
    width, height = max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6)
    scale = min((WIDTH - 2 * MARGIN) / width, (HEIGHT - 2 * MARGIN) / height)
    offset_x = (WIDTH - width * scale) / 2
    offset_y = (HEIGHT - height * scale) / 2

    def xy(point):
        return round(offset_x + (point.x - min_x) * scale), round(HEIGHT - offset_y - (point.y - min_y) * scale)

    # Draw full-resolution source geometry, never the lossy preview polygon.
    for path in plan.drawing_paths:
        coordinates = [xy(point) for point in path.points]
        if path.closed and len(coordinates) > 2:
            coordinates.append(coordinates[0])
        if len(coordinates) >= 2:
            draw.line(coordinates, fill=(86, 101, 95), width=1, joint="curve")
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except OSError:
        font = ImageFont.load_default()
    for label in plan.drawing_labels[:MAX_LABELS]:
        if label.text.strip():
            x, y = xy(label.position)
            draw.text((x + 2, y - 14), label.text[:100], font=font, fill=(35, 47, 43), stroke_width=2, stroke_fill="white")

    if plan.area_candidates:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        layer = ImageDraw.Draw(overlay)
        for candidate in plan.area_candidates:
            coordinates = [xy(point) for point in candidate.boundary]
            if len(coordinates) >= 3:
                layer.polygon(coordinates, fill=(14, 139, 114, 30), outline=(8, 112, 94, 230), width=2)
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
