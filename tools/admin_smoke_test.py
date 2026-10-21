"""End-to-end check of the admin: store edit + history + undo, photo upload
with thumbnails, the store-update review queue (auto-apply, approve with an
edit, reject, undo), category rename, backup + restore.

Everything runs inside one transaction that is rolled back at the end, so
the database is left unchanged. (Uploaded test photos and backup files are
removed too.)  Run from the repo root:  python tools/admin_smoke_test.py
"""
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "django_app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.contrib.auth.models import User  # noqa: E402
from django.core.files.storage import storages  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from PIL import Image  # noqa: E402

from directory import services  # noqa: E402
from directory.models import Category, ChangeProposal, MenuGroup, Store, StoreRevision  # noqa: E402
from directory.storage import media  # noqa: E402

setup_test_environment()
results = []


def check(label, ok):
    results.append((label, bool(ok)))
    print(("PASS " if ok else "FAIL ") + label)


def public(url):
    c = Client(HTTP_HOST="mynameisho.com")
    c.cookies["happy_mobile"] = "off"
    return c.get(url).content.decode("utf-8")


created_files = []

with transaction.atomic():
    admin_user = User.objects.create_superuser("smoke-test-admin", "smoke@example.com", None)
    c = Client(HTTP_HOST="mynameisho.com")
    c.force_login(admin_user)

    # every admin screen opens
    for url in ["/admin/", "/admin/directory/store/", "/admin/directory/store/3666/change/",
                "/admin/directory/store/add/", "/admin/directory/category/", "/admin/directory/review/",
                "/admin/directory/storerevision/", "/admin/directory/changeproposal/",
                "/admin/directory/backup/", "/admin/accounts/member/",
                "/admin/directory/store/?status=closed", "/admin/directory/store/?q=golf"]:
        r = c.get(url)
        check(f"opens {url}", r.status_code == 200)

    # ── edit a store through the admin form ──
    store = Store.all_objects.get(pk=3666)
    old_title = store.raw["title"]
    form = c.get("/admin/directory/store/3666/change/").context["adminform"].form
    data = {k: v for k, v in form.initial.items() if v is not None and not k.startswith("photo_")}
    data["suburb_ref"] = form.initial.get("suburb_ref") or ""
    data["use_period"] = str(form.initial["use_period"])
    data["title"] = old_title + " (테스트)"
    img = io.BytesIO()
    Image.new("RGB", (1200, 900), (200, 60, 40)).save(img, "JPEG")
    img.seek(0)
    img.name = "test.jpg"
    data["photo_0"] = img
    r = c.post("/admin/directory/store/3666/change/", data)
    check("store form saves (redirect)", r.status_code == 302)
    store.refresh_from_db()
    check("title changed in raw + derived column", store.raw["title"].endswith("(테스트)") and store.title.endswith("(테스트)"))
    image = store.raw["img_0"]
    check("photo stored with CMS-style name", image.startswith("/mallimg/") and media().exists(f"skin/lifeb{image}"))
    for w, h, pos, q in services.THUMB_SIZES:
        name = services.thumb_name(image, w, h, pos, q)
        created_files.append(name)
        ok = media().exists(name)
        if ok:
            with media().open(name) as fh:
                ok = Image.open(fh).size == (w, h)
        check(f"thumbnail {w}x{h} generated", ok)
    created_files.append(f"skin/lifeb{image}")
    page = public("/detail.php?number=3666")
    check("detail page shows new title", old_title + " (테스트)" in page)
    check("detail page uses the new photo thumbnail", image.rsplit(".", 1)[0] in page)
    rev = StoreRevision.objects.filter(store_id=3666).first()
    check("change recorded in history", rev and rev.action == "update" and rev.before["raw"]["title"] == old_title)
    r = c.get(f"/admin/directory/storerevision/{rev.pk}/change/")
    check("history page shows the diff", r.status_code == 200 and old_title in r.content.decode())
    r = c.post(f"/admin/directory/storerevision/{rev.pk}/undo/")
    store.refresh_from_db()
    check("undo restores the store", store.raw["title"] == old_title and store.raw["img_0"] == rev.before["raw"]["img_0"])

    # ── close via bulk action hides it from the site ──
    r = c.post("/admin/directory/store/", {"action": "close_selected", "_selected_action": ["3666"]})
    check("close action hides the detail page", "등록된 정보가 없습니다" in public("/detail.php?number=3666"))
    services.save_store(Store.all_objects.get(pk=3666), Store.all_objects.get(pk=3666).raw, closed=False)
    check("reopen shows it again", old_title in public("/detail.php?number=3666"))

    # ── store-update tool: hybrid review ──
    cat = Category.objects.get(pk=986).legacy_path
    auto_new = services.submit_proposal(
        ChangeProposal.NEW, title="테스트 신규 식당", confidence=0.95,
        changes={"title": "테스트 신규 식당", "category": cat, "si": "35", "gu": "1491",
                 "address": "1 George St", "phone": "02 0000 0000"},
        evidence={"google_verified": True, "maps_url": "https://www.google.com/maps/place/test"})
    check("confident + verified proposal auto-applied", auto_new.status == ChangeProposal.AUTO_APPLIED)
    new_id = auto_new.store_id
    check("auto-added store is live on the site", "테스트 신규 식당" in public(f"/detail.php?number={new_id}"))
    pending = services.submit_proposal(
        ChangeProposal.UPDATE, store_id=1734, title="kz sushi", confidence=0.6,
        changes={"phone": "02 1111 2222"}, evidence={"google_verified": True})
    check("low-confidence proposal waits for review", pending.status == ChangeProposal.PENDING)
    r = c.get(f"/admin/directory/changeproposal/{pending.pk}/change/")
    check("review screen opens", r.status_code == 200 and "02 1111 2222" in r.content.decode())
    r = c.post(f"/admin/directory/changeproposal/{pending.pk}/change/",
               {"decision": "approve", "value_phone": "02 3333 4444"})
    pending.refresh_from_db()
    check("approve with an edited value", pending.status == ChangeProposal.APPROVED
          and Store.all_objects.get(pk=1734).raw["phone"] == "02 3333 4444")
    close = services.submit_proposal(
        ChangeProposal.CLOSE, store_id=1734, title="kz sushi", confidence=0.99,
        evidence={"google_verified": True, "business_status": "CLOSED_TEMPORARILY"})
    check("temporary closure is NOT auto-applied", close.status == ChangeProposal.PENDING)
    c.post(f"/admin/directory/changeproposal/{close.pk}/change/", {"decision": "reject"})
    close.refresh_from_db()
    check("reject", close.status == ChangeProposal.REJECTED and not Store.all_objects.get(pk=1734).closed)
    c.post(f"/admin/directory/changeproposal/{auto_new.pk}/change/", {"decision": "undo"})
    auto_new.refresh_from_db()
    check("undo an auto-applied new store removes it",
          auto_new.status == ChangeProposal.UNDONE and not Store.all_objects.filter(pk=new_id).exists())

    # ── category rename cascades to stores ──
    target = Category.objects.get(pk=988)
    old_path = target.legacy_path
    n_before = Store.all_objects.filter(category_path=old_path).count()
    services.rename_category(target, "중식테스트")
    new_path = old_path.rsplit("/", 1)[0] + "/중식테스트"
    check("category rename rewrites store paths",
          n_before > 0 and Store.all_objects.filter(category_path=new_path).count() == n_before)

    # ── left menu follows the admin ──
    menu_html = public("/category.php")
    check("menu shows renamed category", "중식테스트" in menu_html)
    child = services.add_category(Category.objects.get(pk=986).parent, "메뉴테스트")
    # Empty categories stay out of the public menu until a store is filed in them.
    check("new category joins the menu, hidden while empty",
          child.menu_items.exists() and f'category.php?category={child.pk}"' not in public("/category.php"))
    Category.objects.filter(pk=child.pk).update(links_count=1)
    check("new category appears in the menu once it has stores",
          f'category.php?category={child.pk}">메뉴테스트<' in public("/category.php"))

    # ── 구인구직 · 후기 · 광고 (member pages) ──
    from accounts.models import Member
    from community.models import Ad, JobPost, Story
    member = Member.objects.create(login_id="smoke@example.com", email="smoke@example.com",
                                   password="!", nickname="스모크")
    mc = Client(HTTP_HOST="mynameisho.com")
    mc.cookies["happy_mobile"] = "off"
    s = mc.session
    s["kp_member_id"] = member.pk
    s.save()
    mc.cookies["sessionid"] = s.session_key
    for url in ["/job_list.php", "/story_list.php", "/ad_list.php"]:
        check(f"public page {url}", Client(HTTP_HOST="mynameisho.com").get(url).status_code == 200)
    check("writing needs login", Client(HTTP_HOST="mynameisho.com").get("/story_write.php").status_code == 302)
    img = io.BytesIO()
    Image.new("RGB", (1600, 1000), (40, 120, 200)).save(img, "JPEG")
    img.seek(0)
    img.name = "cover.jpg"
    r = mc.post("/story_write.php", {"category": "visa", "title": "스모크 변호사 상담 후기", "tags": "시드니, 변호사",
                                     "content": "변호사 상담 경험", "cover_upload": img})
    story = Story.objects.filter(title="스모크 변호사 상담 후기").first()
    check("member writes a 후기", r.status_code == 302 and story and story.cover and story.author_name == "스모크")
    if story:
        created_files += [story.cover, story.cover[:-4] + "_card.jpg"]
    search = public("/all_search.php?action=search&search_word=%EB%B3%80%ED%98%B8%EC%82%AC")
    check("후기 section on the search page", "변호사 관련 호주 후기" in search and "스모크 변호사 상담 후기" in search)
    check("후기 section sits above the store results",
          search.find("kp-story-search") < search.find('id="kp-search-card-grid"'))
    check("no 후기 section when nothing matches",
          "kp-story-search" not in public("/all_search.php?action=search&search_word=zzqqxx"))
    r = mc.post("/job_write.php", {"kind": "hiring", "job_type": "food", "employment": "part", "state": "35",
                                   "suburb_name": "Strathfield", "pay": "$28", "title": "스모크 주방 보조",
                                   "content": "내용", "contact": "0400 000 000"})
    job = JobPost.objects.filter(title="스모크 주방 보조").first()
    check("member writes a 구인구직 post", r.status_code == 302 and job and job.suburb and job.suburb.name == "Strathfield")
    check("job contact shown to visitors", "0400 000 000" in Client(HTTP_HOST="mynameisho.com").get(
        f"/job_view.php?number={job.pk}").content.decode())
    r = mc.post("/ad_request.php", {"title": "스모크 광고", "summary": "소개", "contact": "0400", "store_id": "3666"})
    ad = Ad.objects.filter(title="스모크 광고").first()
    check("ad request received", ad and ad.status == Ad.REQUESTED)
    check("requested ad not shown yet", "스모크 광고" not in public("/ad_list.php"))
    c.post("/admin/community/ad/", {"action": "approve", "_selected_action": [str(ad.pk)]})
    check("approved ad is shown", "스모크 광고" in public("/ad_list.php"))
    other = Client(HTTP_HOST="mynameisho.com")
    check("others cannot delete a post", other.post("/story_delete.php", {"number": story.pk}).status_code == 404)
    for url in ["/admin/community/story/", "/admin/community/jobpost/", "/admin/community/ad/",
                "/admin/community/sitepage/", "/admin/directory/menugroup/", f"/admin/community/story/{story.pk}/change/",
                f"/admin/directory/menugroup/{MenuGroup.objects.first().pk}/change/"]:
        check(f"opens {url}", c.get(url).status_code == 200)

    # ── backup + restore ──
    backup = services.create_backup(note="smoke test")
    check("backup created", storages["backups"].exists(backup.file) and backup.size > 0)
    Store.all_objects.filter(pk=3666).update(title="CHANGED")
    safety = services.restore_backup(backup)
    check("restore brings data back", Store.all_objects.get(pk=3666).title != "CHANGED")
    for b in (backup, safety):
        services.delete_backup(b)

    transaction.set_rollback(True)

for name in created_files:
    media().delete(name)

failed = [label for label, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
