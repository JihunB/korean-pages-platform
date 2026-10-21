"""Media storage for store photos and their thumbnails.

LayeredStorage writes to one folder and reads from it first, then from any
number of read-only fallback folders. In development the fallback is the
local copy of the old site, so existing photos show up while nothing in
that copy is ever modified. In production it is replaced by S3 (the paths
stay the same: skin/lifeb/mallimg/..., skin/lifeb/upload/happy_thumb/...).
"""
import os

from django.core.files.storage import FileSystemStorage, storages


class LayeredStorage(FileSystemStorage):
    def __init__(self, location=None, fallbacks=(), **kwargs):
        super().__init__(location=location, **kwargs)
        self.fallbacks = [FileSystemStorage(location=f) for f in fallbacks]

    def _own(self, name):
        # FileSystemStorage.exists() calls self.path(), which is overridden
        # below to consult the layers, so check this folder directly.
        return os.path.exists(super().path(name))

    def _source(self, name):
        if self._own(name):
            return self
        for fb in self.fallbacks:
            if fb.exists(name):
                return fb
        return None

    def exists(self, name):
        return self._source(name) is not None

    def open(self, name, mode="rb"):
        src = self._source(name)
        if src is None or src is self:
            return super().open(name, mode)
        return src.open(name, mode)

    def path(self, name):
        src = self._source(name)
        if src is None or src is self:
            return super().path(name)
        return src.path(name)

    def delete(self, name):
        # Only files written here can be deleted; the fallbacks are read-only.
        if self._own(name):
            super().delete(name)

    def get_available_name(self, name, max_length=None):
        # Uploads get unique CMS-style names already; never rename silently.
        return name


def media():
    return storages["legacy_media"]


def media_path(name):
    """Filesystem path of a media file (development only)."""
    return os.path.normpath(media().path(name))


try:
    from storages.backends.s3 import S3Storage
except ImportError:  # development without django-storages
    S3Storage = None

if S3Storage is not None:
    class S3MediaStorage(S3Storage):
        """Production media storage: the S3 bucket CloudFront serves the
        photos from, at the same paths as the live site. Photo names are
        unique (the CMS naming), so files are written as named, never
        renamed; browsers may cache them for a week."""
        file_overwrite = True
        object_parameters = {"CacheControl": "public, max-age=604800"}

        def get_available_name(self, name, max_length=None):
            return name


# The image decoders Pillow may use for any photo, whatever its file name says:
# a TIFF, EPS or other file renamed to .jpg never reaches the less-hardened decoders.
SAFE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")
