"""Photos for 후기 posts and 광고: saved to the media storage (S3 in
production) as a web-sized JPEG plus a card-sized thumbnail, so pages stay
fast however many photos are uploaded."""
import io
import uuid

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageOps

from directory.storage import SAFE_FORMATS, media

MAX_SIDE = 1600          # web-size photo
CARD = (640, 400)        # card thumbnail (16:10, like the store cards)
ALLOWED = ("jpg", "jpeg", "png", "gif", "webp", "heic")
MAX_BYTES = 15 * 1024 * 1024


class ImageError(ValueError):
    pass


def _jpeg(img, quality=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return ContentFile(buf.getvalue())


def save_image(upload, folder):
    """Returns the media path of the saved photo ("community/…/x.jpg")."""
    ext = (upload.name.rsplit(".", 1)[-1] or "").lower()
    if ext not in ALLOWED:
        raise ImageError("사진은 jpg, png, gif, webp 파일만 올릴 수 있습니다.")
    if upload.size > MAX_BYTES:
        raise ImageError("사진 한 장의 크기는 15MB 이하여야 합니다.")
    try:
        img = Image.open(upload, formats=SAFE_FORMATS)
        img = ImageOps.exif_transpose(img)  # phone photos: apply the rotation flag
    except Exception:
        raise ImageError("사진 파일을 읽을 수 없습니다.")
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    stem = f"community/{folder}/{timezone.localtime():%Y/%m}/{uuid.uuid4().hex[:16]}"
    media().save(f"{stem}.jpg", _jpeg(img))
    card = ImageOps.fit(img, CARD, Image.LANCZOS, centering=(0.5, 0.5))
    media().save(f"{stem}_card.jpg", _jpeg(card, 82))
    return f"{stem}.jpg"


def url(path, card=False):
    if not path:
        return ""
    if card and path.endswith(".jpg"):
        path = path[:-4] + "_card.jpg"
    return "/" + path


def delete_image(path):
    for name in (path, path[:-4] + "_card.jpg" if path.endswith(".jpg") else None):
        if name and media().exists(name):
            media().delete(name)
