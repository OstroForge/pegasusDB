import io
from pathlib import Path

import qrcode
from django.urls import reverse
from PIL import Image, ImageDraw
from urllib.parse import urlencode

_LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "training" / "img" / "logo.png"
_LOGO_MARK = None


def course_register_short_path(course_code: str) -> str:
    return reverse("public_register_short", kwargs={"code": course_code})


def course_register_short_url(request, course_code: str) -> str:
    return request.build_absolute_uri(course_register_short_path(course_code))


def course_register_full_url(request, course_code: str) -> str:
    query = urlencode({"ct": course_code})
    return request.build_absolute_uri(f"{reverse('public_delegate_register')}?{query}")


def _company_logo_mark():
    """Unicorn artwork with the black page background removed. Cached."""
    global _LOGO_MARK
    if _LOGO_MARK is not None:
        return _LOGO_MARK
    if not _LOGO_PATH.is_file():
        _LOGO_MARK = False
        return _LOGO_MARK

    image = Image.open(_LOGO_PATH).convert("RGBA")
    image.thumbnail((700, 700), Image.Resampling.LANCZOS)
    rgb = image.convert("RGB")
    # Flood the black page from the corners so the unicorn's own linework stays.
    sentinel = (1, 2, 3)
    for point in (
        (0, 0),
        (rgb.width - 1, 0),
        (0, rgb.height - 1),
        (rgb.width - 1, rgb.height - 1),
    ):
        ImageDraw.floodfill(rgb, point, sentinel, thresh=28)
    src = rgb.load()
    out = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if src[x, y] == sentinel:
                red, green, blue, _alpha = out[x, y]
                out[x, y] = (red, green, blue, 0)
    bounds = image.getbbox()
    if bounds:
        image = image.crop(bounds)
    _LOGO_MARK = image
    return _LOGO_MARK


def _stamp_logo(qr_image: Image.Image) -> Image.Image:
    logo = _company_logo_mark()
    if not logo:
        return qr_image

    canvas = qr_image.convert("RGBA")
    side = max(36, int(min(canvas.size) * 0.22))
    pad = max(4, side // 8)
    artwork = logo.copy()
    artwork.thumbnail((side - pad * 2, side - pad * 2), Image.Resampling.LANCZOS)

    badge = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    plate = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 6, fill=255)
    badge.paste(plate, (0, 0))
    badge.putalpha(mask)

    origin = ((side - artwork.width) // 2, (side - artwork.height) // 2)
    badge.paste(artwork, origin, artwork)
    position = ((canvas.width - side) // 2, (canvas.height - side) // 2)
    canvas.paste(badge, position, badge)
    return canvas


def qr_png_bytes(data: str, *, box_size: int = 8, border: int = 2) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = _stamp_logo(qr.make_image(fill_color="black", back_color="white").get_image())
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
