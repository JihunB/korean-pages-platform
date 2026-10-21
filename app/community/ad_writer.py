"""광고: turns a member's ad request into a finished ad.

The background worker (manage.py store_update_worker) calls
process_pending() between store-update runs. For each new request Claude
writes the title, one-line summary and body from what the member sent (and
the store's own page, when the ad is for a listed store); when no image was
attached, Stability AI on Bedrock makes one. The ad then waits in 관리자 ›
광고 for approval like before, with the member's original text kept beside
it. Every call counts toward the monthly Claude budget.
"""
import base64
import json
import re
import traceback

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import connection
from django.utils import timezone

from directory import store_writer
from directory.models import Store

from . import images
from .models import Ad

AD_PROMPT = """당신은 호주 한인 업체 디렉터리 "코리안 페이지"의 광고 카피라이터입니다. 업체가 보낸 광고 신청 내용과
(있으면) 코리안 페이지에 등록된 업체 정보를 보고, 광고 목록에 실릴 광고를 씁니다.
- 신청 내용에 있는 사실만 씁니다. 가격·할인·영업시간·연락처 같은 정보를 지어내지 않습니다.
- title: 업체명이 들어간 짧고 눈에 띄는 제목 (40자 이내).
- summary: 무엇을 알리는 광고인지 한 줄로 (70자 이내).
- content: 광고 페이지 본문. 손님이 알아야 할 내용을 3~6문장으로 (400자 이내). 자연스러운 한국어로, 과장된 광고 문구나 이모지는 쓰지 않습니다.
- image_prompt: 광고 이미지를 만들 영어 프롬프트. 업체의 업종과 분위기가 드러나는 사진 같은 장면을 묘사하고,
  글자·간판·로고·사람 얼굴 클로즈업은 넣지 않습니다 (예: "bright modern Korean hair salon interior, styling chairs,
  natural light, professional photography").
- 신청 내용이 광고로 내보내기 어려운 경우(의미 없는 글, 불법·성인 내용 등)에는 ok를 false로 하고 notes에 이유를 씁니다."""

AD_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "content": {"type": "string"},
        "image_prompt": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["ok", "title", "summary", "content", "image_prompt", "notes"],
    "additionalProperties": False,
}

IMAGE_PROMPT = """광고 문구를 보고, 광고 이미지를 만들 영어 프롬프트 하나만 씁니다. 업체의 업종과 광고 내용이 드러나는
사진 같은 장면을 묘사하고, 글자·간판·로고·사람 얼굴 클로즈업은 넣지 않습니다
(예: "professional auto body spray painting workshop, freshly painted glossy car door panel, spray gun, bright clean booth,
professional photography")."""

IMAGE_SCHEMA = {
    "type": "object",
    "properties": {"image_prompt": {"type": "string"}},
    "required": ["image_prompt"],
    "additionalProperties": False,
}

IMAGE_PRICE = 0.04  # US$ per Stable Image Core picture


def digits(value):
    d = re.sub(r"\D", "", value or "")
    return "0" + d[2:] if d.startswith("61") else d


def find_store(ad):
    """The listed store this ad is for: the number the member gave, else a
    shown store with the same phone number or the same name."""
    if ad.store_id:
        return Store.objects.filter(pk=ad.store_id).first()
    phone = digits(ad.contact)
    if len(phone) >= 8:
        for store in Store.objects.filter(raw__phone__icontains=phone[-4:]):
            if digits(store.raw.get("phone"))[-8:] == phone[-8:]:
                return store
    name = (ad.requested_title or ad.title or "").strip()
    if len(name) >= 3:
        return Store.objects.filter(raw__title__iexact=name).first()
    return None


def store_facts(store):
    raw = store.raw
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.get("review", ""))).strip()
    return {"title": raw.get("title", ""), "category": raw.get("category", ""), "address": raw.get("address", ""),
            "intro": raw.get("short_comment", ""), "description": text[:1500]}


def generate_image(prompt, usage):
    import boto3

    client = boto3.client("bedrock-runtime", region_name=settings.AD_IMAGE_REGION)
    body = {"prompt": prompt[:1500], "negative_prompt": "text, letters, words, watermark, logo, signage",
            "aspect_ratio": "16:9", "output_format": "jpeg"}
    resp = client.invoke_model(modelId=settings.AD_IMAGE_MODEL, body=json.dumps(body))
    out = json.loads(resp["body"].read())
    key = store_writer.month_key()
    usage[key] = round(usage.get(key, 0) + IMAGE_PRICE, 6)
    usage["image_usd"] = round(usage.get("image_usd", 0) + IMAGE_PRICE, 6)
    if not out.get("images") or (out.get("finish_reasons") or [None])[0]:
        raise store_writer.WriterError(f"이미지를 만들지 못했습니다 ({out.get('finish_reasons')}).")
    return base64.b64decode(out["images"][0])


def write_ad(ad):
    usage = dict(ad.ai_usage or {})
    usage["ad_id"] = ad.pk
    store = find_store(ad)
    if store and not ad.store_id:
        ad.store_id = store.pk  # the ad then opens the store's page
    facts = {"request": {"title": ad.requested_title or ad.title, "summary": ad.requested_summary or ad.summary,
                         "content": ad.requested_content or ad.content, "link": ad.link,
                         "has_image": bool(ad.image)},
             "store": store_facts(store) if store else None}
    notes = [f"{timezone.localtime():%Y-%m-%d %H:%M}"]
    if usage.get("keep_text"):
        # The ad's text stays exactly as written (the admin's 글은 그대로 ·
        # 이미지만 만들기): Claude only describes a picture for it.
        writer = store_writer.StoreWriter(usage=usage, system_prompt=IMAGE_PROMPT, schema=IMAGE_SCHEMA, effort="low")
        written = {"ok": True, "title": ad.title, "summary": ad.summary, "content": ad.content,
                   "image_prompt": writer.write({"ad": {"title": ad.title, "summary": ad.summary,
                                                        "content": ad.content}})["image_prompt"]}
    else:
        writer = store_writer.StoreWriter(usage=usage, system_prompt=AD_PROMPT, schema=AD_SCHEMA, effort="low")
        written = writer.write(facts)
    if not written.get("ok"):
        ad.ai_status = Ad.AI_FAILED
        notes.append(f"광고로 쓰기 어려움: {written.get('notes', '')}")
    else:
        ad.title = written["title"][:80] or ad.title
        ad.summary = written["summary"][:160] or ad.summary
        ad.content = written["content"] or ad.content
        if not ad.image and written.get("image_prompt"):
            store_writer.check_budget(usage, upcoming=IMAGE_PRICE)
            data = generate_image(written["image_prompt"], usage)
            ad.image = images.save_image(ContentFile(data, name="ad.jpg"), "ads")
            ad.image_generated = True
            notes.append("이미지를 새로 만들었습니다.")
        if store:
            notes.append(f"업체 #{store.pk} {store.raw.get('title', '')} 상세 페이지로 연결")
        ad.ai_status = Ad.AI_DONE
    usage.pop("ad_id", None)
    ad.ai_usage = usage
    ad.ai_note = (ad.ai_note + "\n" if ad.ai_note else "") + " · ".join(notes)
    ad.save()


def claim(pk):
    """Take one waiting ad (the request page and the worker may both try)."""
    return Ad.objects.filter(pk=pk, ai_status=Ad.AI_PENDING).update(ai_status=Ad.AI_WORKING) == 1


def process_one(pk):
    """Write one ad now, if it is still waiting. Returns False to stop for a while."""
    if not claim(pk):
        return True
    ad = Ad.objects.get(pk=pk)
    try:
        write_ad(ad)
    except (store_writer.BudgetError, store_writer.DailyLimitError) as e:
        Ad.objects.filter(pk=pk).update(ai_status=Ad.AI_PENDING, ai_note=f"Claude 한도로 대기 중: {e}"[:500])
        return False
    except Exception as e:  # one bad request must not stop the others
        traceback.print_exc()
        Ad.objects.filter(pk=pk).update(ai_status=Ad.AI_FAILED, ai_note=f"오류: {type(e).__name__}: {e}"[:500])
    return True


def start_in_background(pk):
    """Called right after a member sends a request: write it on a thread so
    the ad is ready within a minute (the worker picks up any it misses)."""
    import threading

    def run():
        try:
            process_one(pk)
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True).start()


def process_pending(limit=5):
    """The worker's pass over ads still waiting."""
    for pk in list(Ad.objects.filter(ai_status=Ad.AI_PENDING).order_by("id").values_list("pk", flat=True)[:limit]):
        try:
            if not process_one(pk):
                return  # Claude's limit: try again on a later pass
        finally:
            connection.close()


def reset_unfinished():
    """At worker start: ads left half-written by a restart go back in the queue."""
    Ad.objects.filter(ai_status=Ad.AI_WORKING).update(ai_status=Ad.AI_PENDING)
