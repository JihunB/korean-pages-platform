"""Every change to the site's data goes through here, whether it comes from
the admin, the store-update tool or a backup restore. That keeps three
guarantees in one place:

* each store change is recorded in StoreRevision (before/after), so it can
  be reviewed and undone;
* the derived columns and the categories' store counts stay in step with
  `raw` (the values the pages print);
* photos get the same thumbnails, at the same paths, as the CMS made.
"""
import datetime
import gzip
import io
import json
import random
import time

from django.conf import settings
from django.core import serializers
from django.core.files.base import ContentFile, File
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from PIL import Image

from .models import (Backup, Category, ChangeProposal, Review, State, Store,
                     StoreRevision, Suburb)
from .storage import SAFE_FORMATS, media

SKIN = "skin/lifeb"
PHOTO_SLOTS = 10  # img_0 (card photo) .. img_9

# Legacy columns every store row carries (info_upso2_links). A new store is
# created with all of them so the page templates find every value they
# print, exactly as for imported stores.
LEGACY_COLUMNS = (
    "number", "title", "address", "phone", "mail", "category", "category2", "category3",
    "review", "short_comment", "build_date", "homepage", "si", "gu", "dong", "area",
    "date", "new", "pop", "pick", "spec", "inquire", "wait", "admin_point", "use_period",
    "option1", "option2", "detail_link", "id", "related", "first_out", "happy_schedule",
    "hompi_end_date", "coopon", "order_use", "order_day", "order_time_start", "order_time_end",
    "x_point", "y_point", "x_point2", "y_point2", "x_do", "x_min", "x_sec", "y_do", "y_min", "y_sec",
    "daum_Pan", "daum_PanoId", "daum_Tilt", "daum_Zoom",
    "minihome0", "minihome1", "minihome2", "minihome3", "minihome4", "minihome5",
    "minihome_default", "minihome_id", "minihome_link3", "minihome_main",
    "mini_img1", "mini_img2", "mini_img3", "mini_img4",
    *(f"etc{i}" for i in range(1, 11)), *(f"img_{i}" for i in range(11)),
)
# Values the CMS gave a newly registered, published store.
NEW_STORE_DEFAULTS = {"wait": "0", "use_period": "2050-01-02", "inquire": "0", "admin_point": "0"}


def now_text():
    return timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")


# ── categories ────────────────────────────────────────────────────────
def visible_stores():
    """Stores shown in the listings: published, unexpired and not closed."""
    return Store.objects.filter(wait="0", use_period__gte=datetime.date.today())


def category_counts(stores, categories=None):
    """{category id: stores filed in it or below it}. A store counts once
    per category, whichever of category, category2 or category3 puts it
    there (the 가나다 categories are usually category2)."""
    rows = stores.values_list("category_path", "category2_path", "category3_path")
    tally = {}
    for row in rows:
        seen = set()
        for path in row:
            while path and path not in seen:
                seen.add(path)
                path = path.rpartition("/")[0]
        for path in seen:
            tally[path] = tally.get(path, 0) + 1
    cats = categories if categories is not None else Category.objects.all()
    return {cat.pk: tally.get(cat.legacy_path, 0) for cat in cats if cat.legacy_path}


def recount_categories():
    """category_recounting() from the CMS: each category's links_count is
    the number of published, unexpired (and here: not closed) stores filed
    in it or below it through category, category2 or category3."""
    cats = list(Category.objects.all())
    counts = category_counts(visible_stores(), cats)
    changed = []
    for cat in cats:
        n = counts.get(cat.pk, 0)
        if cat.links_count != n:
            cat.links_count = n
            changed.append(cat)
    Category.objects.bulk_update(changed, ["links_count"])
    return len(changed)


# ── store changes, with history ───────────────────────────────────────
def _record(store_id, source, action, before, after, note=""):
    return StoreRevision.objects.create(store_id=store_id, source=source, action=action,
                                        before=before, after=after, note=note[:200])


@transaction.atomic
def save_store(store, raw, *, closed=None, source=StoreRevision.MANUAL, note="", action=None, recount=True):
    """Create or update a store from its legacy column values."""
    is_new = store.pk is None or not Store.all_objects.filter(pk=store.pk).exists()
    before = None if is_new else Store.all_objects.get(pk=store.pk).snapshot()
    if is_new and store.pk is None:
        store.pk = next_store_id()
    raw = dict(raw)
    raw["number"] = str(store.pk)
    store.raw = raw
    if closed is not None:
        store.closed = closed
    store.sync_from_raw()
    store.save()
    after = store.snapshot()
    if before == after:
        return None
    if action is None:
        action = "create" if is_new else (
            "close" if after["closed"] and not before["closed"] else
            "reopen" if before["closed"] and not after["closed"] else "update")
    revision = _record(store.pk, source, action, before, after, note)
    if recount:  # bulk callers recount once at the end instead
        recount_categories()
    return revision


@transaction.atomic
def delete_store(store, *, source=StoreRevision.MANUAL, note=""):
    revision = _record(store.pk, source, "delete", store.snapshot(), None, note)
    Store.all_objects.filter(pk=store.pk).delete()
    recount_categories()
    return revision


@transaction.atomic
def undo(revision, note="", recount=True):
    """Put the store back exactly as it was before `revision`."""
    if revision.undone:
        raise ValueError("이미 되돌린 변경입니다.")
    current = Store.all_objects.filter(pk=revision.store_id).first()
    before_now = current.snapshot() if current else None
    if revision.before is None:           # the change created the store
        if current:
            Store.all_objects.filter(pk=revision.store_id).delete()
    else:
        store = current or Store(pk=revision.store_id)
        store.raw = dict(revision.before["raw"])
        store.closed = revision.before["closed"]
        store.sync_from_raw()
        store.save()
    revision.undone = True
    revision.save(update_fields=["undone"])
    ChangeProposal.objects.filter(revision=revision).update(
        status=ChangeProposal.UNDONE, decided_at=timezone.now())
    if recount:  # bulk callers recount once at the end
        recount_categories()
    return _record(revision.store_id, StoreRevision.UNDO, "undo", before_now, revision.before,
                   note or f"변경 #{revision.pk} 되돌림")


def next_store_id():
    return (Store.all_objects.aggregate(m=Max("id"))["m"] or 0) + 1


def new_store_raw(**values):
    raw = {col: "" for col in LEGACY_COLUMNS}
    raw.update(NEW_STORE_DEFAULTS)
    raw["date"] = now_text()
    raw.update(values)
    return raw


# ── photos ────────────────────────────────────────────────────────────
# The thumbnails the page templates reference (see legacy/listing.py):
# (width, height, position, quality) — card, detail gallery, og:image.
# Every (width, height, position, quality) the page templates ask for; the
# mobile cards use 800x500 at position 5.
THUMB_SIZES = ((800, 500, 7, 97), (800, 500, 5, 97), (800, 600, 5, 97), (1000, 1200, 7, 100))


def thumb_name(image, width, height, position, quality):
    stem, _, ext = f"{SKIN}{image}".rpartition(".")
    return (f"{SKIN}/upload/happy_thumb/{width}x{height}_{quality}/"
            f"{stem}_N_{position}_{width}x{height}_{quality}_2_1.{ext}")


def make_thumbnails(image):
    """Generate the CMS thumbnails for an uploaded photo. Measured against
    the live site's own thumbnails: the photo is scaled to cover the box
    (small photos are enlarged too) and cropped from the top-left corner."""
    with media().open(f"{SKIN}{image}".lstrip("/")) as fh:
        src = Image.open(fh, formats=SAFE_FORMATS)
        src.load()
    ext = image.lower().rsplit(".", 1)[-1]
    fmt = {"png": "PNG", "webp": "WEBP"}.get(ext, "JPEG")
    for width, height, position, quality in THUMB_SIZES:
        scale = max(width / src.width, height / src.height)
        size = (max(width, round(src.width * scale)), max(height, round(src.height * scale)))
        img = src.convert("RGBA" if fmt == "PNG" else "RGB").resize(size, Image.LANCZOS)
        img = img.crop((0, 0, width, height))
        buf = io.BytesIO()
        # WebP thumbnails stay WebP (smaller files, and the file matches its name).
        img.save(buf, fmt, **({"quality": quality} if fmt == "JPEG" else {"quality": 82} if fmt == "WEBP" else {}))
        name = thumb_name(image, width, height, position, quality)
        if media().exists(name):
            media().delete(name)
        media().save(name, ContentFile(buf.getvalue()))


def save_photo(upload):
    """Store an uploaded photo the way the CMS named them
    (/mallimg/YYYY/MM/DD/<unix time>-<random>.<ext>) and make its
    thumbnails. Returns the value for the img_N column."""
    ext = (upload.name.rsplit(".", 1)[-1] or "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        raise ValueError("사진은 jpg, png, gif, webp 파일만 올릴 수 있습니다.")
    Image.open(upload, formats=SAFE_FORMATS).verify()  # reject anything that isn't a web image
    upload.seek(0)
    day = timezone.localtime().strftime("%Y/%m/%d")
    image = f"/mallimg/{day}/{int(time.time())}-{random.randint(0, 9999)}.{ext}"
    media().save(f"{SKIN}{image}".lstrip("/"), upload)
    make_thumbnails(image)
    from legacy.listing import media_exists
    media_exists.cache_clear()
    return image


# ── store-update tool: hybrid review ──────────────────────────────────
def submit_proposal(kind, *, title, changes=None, store_id=None, evidence=None, confidence=0.0,
                    allow_auto=True, recount=True):
    """Entry point for the store-update tool. Records the proposed change
    and applies it immediately when the hybrid policy allows (and the run
    permits it: allow_auto=False keeps everything for review); otherwise it
    waits in 관리자 › 자동 업데이트 검토."""
    proposal = ChangeProposal.objects.create(
        kind=kind, store_id=store_id, title=title, changes=changes or {},
        evidence=evidence or {}, confidence=confidence)
    if allow_auto and auto_apply_allowed(proposal):
        apply_proposal(proposal, auto=True, recount=recount)
    return proposal


def auto_apply_allowed(proposal):
    ev = proposal.evidence or {}
    if proposal.confidence < settings.AI_AUTO_APPLY_CONFIDENCE or not ev.get("google_verified"):
        return False
    if proposal.kind == ChangeProposal.CLOSE:
        # Only Google's own "permanently closed" status closes a store unattended.
        return ev.get("business_status") == "CLOSED_PERMANENTLY"
    return True


@transaction.atomic
def apply_proposal(proposal, *, overrides=None, auto=False, recount=True):
    changes = dict(proposal.changes)
    changes.update(overrides or {})
    note = f"자동 업데이트 #{proposal.pk}"
    if proposal.kind == ChangeProposal.NEW:
        store = Store()
        revision = save_store(store, new_store_raw(**changes), source=StoreRevision.TOOL, note=note, recount=recount)
        proposal.store_id = store.pk
    else:
        store = Store.all_objects.get(pk=proposal.store_id)
        raw = dict(store.raw)
        raw.update(changes)
        # A hidden store that discovery found open again is shown again.
        closed = True if proposal.kind == ChangeProposal.CLOSE else (
            False if (proposal.evidence or {}).get("reopen") else None)
        revision = save_store(store, raw, closed=closed, source=StoreRevision.TOOL, note=note, recount=recount)
    place_id = (proposal.evidence or {}).get("place_id")
    if place_id and proposal.kind != ChangeProposal.CLOSE:
        from .models import StorePlace
        StorePlace.objects.update_or_create(store_id=store.pk, defaults={
            "place_id": place_id, "business_status": (proposal.evidence or {}).get("business_status", "")})
    proposal.changes = changes
    proposal.revision = revision
    proposal.status = ChangeProposal.AUTO_APPLIED if auto else ChangeProposal.APPROVED
    proposal.decided_at = timezone.now()
    proposal.save()
    return revision


def reject_proposal(proposal):
    proposal.status = ChangeProposal.REJECTED
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_at"])


# ── backups ───────────────────────────────────────────────────────────
def _backup_models():
    from accounts.models import EmailCode, Member, PasswordResetToken, PendingRegistration
    from community.models import Ad, JobPost, SitePage, Story, StoryImage

    from .models import MenuGroup, MenuItem
    # Order matters for restore: referenced tables first.
    return [State, Suburb, Category, MenuGroup, MenuItem, Store, Review, StoreRevision, ChangeProposal,
            Member, PendingRegistration, EmailCode, PasswordResetToken,
            SitePage, JobPost, Story, StoryImage, Ad]


def create_backup(note="", backup=None):
    """Write every table to a gzipped JSON file, a few hundred rows at a
    time (the history tables are large), and store it in the backup
    storage. Run by the background worker (see run_backup_tasks)."""
    import tempfile

    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryFile() as tmp:
        with gzip.GzipFile(fileobj=tmp, mode="wb") as gz:
            gz.write(b"[")
            first = True
            for model in _backup_models():
                manager = getattr(model, "all_objects", model._default_manager)
                chunk = []
                for obj in manager.order_by("pk").iterator(chunk_size=500):
                    chunk.append(obj)
                    if len(chunk) == 500:
                        first = _write_rows(gz, chunk, first)
                        chunk = []
                if chunk:
                    first = _write_rows(gz, chunk, first)
            gz.write(b"]")
        size = tmp.tell()
        tmp.seek(0)
        name = storages["backups"].save(f"kp-backup-{stamp}.json.gz", File(tmp))
    if backup is None:
        backup = Backup(name=f"백업 {stamp}", note=note[:200])
    backup.file, backup.size, backup.status = name, size, Backup.READY
    backup.save()
    return backup


def _write_rows(gz, objs, first):
    text = serializers.serialize("json", objs, ensure_ascii=False).strip()[1:-1].strip()
    if text:
        gz.write(("" if first else ",").encode() + text.encode("utf-8"))
        first = False
    return first


def run_backup_tasks():
    """The worker's turn: make queued backups, then do a requested restore."""
    for backup in Backup.objects.filter(status=Backup.QUEUED).order_by("id"):
        if not Backup.objects.filter(pk=backup.pk, status=Backup.QUEUED).update(status=Backup.WORKING):
            continue
        try:
            create_backup(backup=backup)
        except Exception as e:
            Backup.objects.filter(pk=backup.pk).update(status=Backup.FAILED, message=f"{type(e).__name__}: {e}"[:500])
    for backup in Backup.objects.filter(restore_status=Backup.QUEUED).order_by("id"):
        if not Backup.objects.filter(pk=backup.pk, restore_status=Backup.QUEUED).update(restore_status=Backup.WORKING):
            continue
        try:
            safety = restore_backup(backup)
            Backup.objects.filter(pk=backup.pk).update(
                restore_status=Backup.READY,
                message=f"{timezone.localtime():%Y-%m-%d %H:%M} 복원 완료. 복원 전 데이터: '{safety.name}'")
        except Exception as e:
            Backup.objects.filter(pk=backup.pk).update(restore_status=Backup.FAILED,
                                                       message=f"복원 오류: {type(e).__name__}: {e}"[:500])


def reset_backup_tasks():
    """At worker start: work cut short by a restart is queued again."""
    Backup.objects.filter(status=Backup.WORKING).update(status=Backup.QUEUED)
    Backup.objects.filter(restore_status=Backup.WORKING).update(restore_status=Backup.QUEUED)


@transaction.atomic
def restore_backup(backup):
    """Replace all site data with the backup's. A safety backup of the
    current data is taken first, so a restore can itself be reversed."""
    safety = create_backup(note=f"'{backup.name}' 복원 직전 자동 백업")
    with storages["backups"].open(backup.file) as fh:
        rows = json.loads(gzip.decompress(fh.read()).decode("utf-8"))
    models = _backup_models()
    for model in reversed(models):
        getattr(model, "all_objects", model._default_manager).all().delete()
    for obj in serializers.deserialize("json", json.dumps(rows)):
        obj.save()
    recount_categories()
    return safety


def delete_backup(backup):
    if storages["backups"].exists(backup.file):
        storages["backups"].delete(backup.file)
    backup.delete()


# ── categories: rename / add ──────────────────────────────────────────
@transaction.atomic
def rename_category(category, new_name):
    """Rename a category. Stores reference categories by their full path
    string, so the path is rewritten on the category, its sub-categories
    and every store filed under them (recorded in each store's history)."""
    new_name = new_name.strip()
    if not new_name or "/" in new_name:
        raise ValueError("카테고리 이름에는 '/'를 쓸 수 없습니다.")
    old = category.legacy_path
    new = "/".join(old.split("/")[:-1] + [new_name])
    if old == new:
        return
    for cat in Category.objects.filter(Q(legacy_path=old) | Q(legacy_path__startswith=old + "/")):
        cat.legacy_path = new + cat.legacy_path[len(old):]
        if cat.pk == category.pk:
            cat.name = new_name
        cat.raw["category_name"] = cat.legacy_path
        cat.save()
    q = Q()
    for col in ("category_path", "category2_path", "category3_path"):
        q |= Q(**{col: old}) | Q(**{f"{col}__startswith": old + "/"})
    for store in Store.all_objects.filter(q):
        raw = dict(store.raw)
        for key in ("category", "category2", "category3"):
            value = raw.get(key) or ""
            if value == old or value.startswith(old + "/"):
                raw[key] = new + value[len(old):]
        save_store(store, raw, note=f"카테고리 이름 변경: {old} → {new}")


def add_category(parent, name, sort_order=None):
    name = name.strip()
    if not name or "/" in name:
        raise ValueError("카테고리 이름에는 '/'를 쓸 수 없습니다.")
    path = f"{parent.legacy_path}/{name}" if parent else name
    if Category.objects.filter(legacy_path=path).exists():
        raise ValueError("같은 이름의 카테고리가 이미 있습니다.")
    cid = (Category.objects.aggregate(m=Max("id"))["m"] or 0) + 1
    depth = parent.depth + 1 if parent else 0
    siblings = Category.objects.filter(parent=parent)
    order = sort_order or (siblings.aggregate(m=Max("sort_order"))["m"] or 0) + 1
    templet = parent.templet if parent else ""
    category = Category.objects.create(
        id=cid, name=name, legacy_path=path, parent=parent, depth=depth, sort_order=order,
        display=True, templet=templet,
        raw={"number": str(cid), "category_name": path, "category_depth": str(depth),
             "category_sort": str(order), "category_templet": templet, "display": "Y",
             "category_printStyle": "", "category_default_tpl": "", "links_count": "0"})
    from .menu import add_to_menu
    add_to_menu(category)
    return category


def category_in_use(category):
    path = category.legacy_path
    q = Q()
    for col in ("category_path", "category2_path", "category3_path"):
        q |= Q(**{col: path}) | Q(**{f"{col}__startswith": path + "/"})
    return Store.all_objects.filter(q).exists() or Category.objects.filter(parent=category).exists()


def suburb_choice(suburb_id):
    """(si, gu) legacy column values for a suburb."""
    sub = Suburb.objects.select_related("state").get(pk=suburb_id)
    return str(sub.state_id), str(sub.pk)
