"""The store-update tool.

verify   — for each existing store: find its Google Maps place (by name near
           its map position, confirmed by phone/name/distance and by Claude),
           then propose the updated listing: name, phone, address, suburb,
           map link, hours, amenities, SNS, and a description written in the
           site's standard format. Permanently closed places are proposed
           for closing.
discover — search an area for Korean and Japanese businesses that are not
           listed yet and propose them as new stores (reviewed, or added at
           once when the run allows it and the listing is complete).
check    — the monthly check of listed stores: one Google lookup per store
           already matched to a place (no Claude) to catch new hours, closures,
           renames, moves, phone numbers and review counts.

Every result goes through services.submit_proposal, so it lands in
관리자 › 자동 업데이트 검토 (or is applied at once when the job allows it and
the hybrid policy agrees) and every change can be undone.
"""
import base64
import collections
import datetime
import io
import math
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlparse

from PIL import Image

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from . import google_places, services, store_batch, store_writer
from .models import (ChangeProposal, DiscoveredPlace, Franchise, Review, Store, StoreBatchItem, StorePlace,
                     StoreRevision, StoreUpdateJob, Suburb)
from .storage import SAFE_FORMATS

# How long a job waits before trying again after Claude's daily limit.
LIMIT_RETRY = datetime.timedelta(hours=1)

# Places looked at in detail per store. Two keeps the Google bill down
# (Jihun's choice); the first result of each search is checked.
MAX_CANDIDATES = 2

EMPTY = ("", "&nbsp;", "<p>&nbsp;</p>", "0")

# ── converting Google's data into the page's formats ──────────────────
DAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]  # Google: 0 = Sunday
ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def hours_value(opening):
    """regularOpeningHours → the page's per-day format, e.g.
    "mon=0900-2100;tue=closed;...;sun=24h". A day with a break time keeps
    each range, comma-separated ("mon=1130-1430,1730-2130"); the store page
    shows them on separate lines and the admin editor edits them."""
    periods = (opening or {}).get("periods") or []
    if not periods:
        return None
    if len(periods) == 1 and "close" not in periods[0]:
        return ";".join(f"{d}=24h" for d in ORDER)
    days = collections.defaultdict(list)
    for p in periods:
        o, c = p.get("open") or {}, p.get("close") or {}
        if "day" not in o:
            continue
        days[DAY_KEYS[o["day"]]].append((o.get("hour", 0), o.get("minute", 0), c.get("hour", 0), c.get("minute", 0)))
    parts = []
    for d in ORDER:
        spans = sorted(days.get(d) or [])
        if not spans:
            parts.append(f"{d}=closed")
            continue
        parts.append(f"{d}=" + ",".join(f"{oh:02d}{om:02d}-{ch:02d}{cm:02d}" for oh, om, ch, cm in spans))
    return ";".join(parts)


def phone_value(place):
    phone = place.get("nationalPhoneNumber") or ""
    return re.sub(r"[()]", "", phone).strip()


def digits(value):
    d = re.sub(r"\D", "", value or "")
    if d.startswith("61"):
        d = "0" + d[2:]
    return d


def address_value(place):
    return re.sub(r",\s*Australia$", "", place.get("formattedAddress") or "").strip()


def suburb_for(place):
    comps = {t: c for c in place.get("addressComponents") or [] for t in c.get("types", [])}
    locality = (comps.get("locality") or {}).get("longText") or ""
    state = (comps.get("administrative_area_level_1") or {}).get("shortText") or ""
    postcode = (comps.get("postal_code") or {}).get("longText") or ""
    if not locality or not state:
        return None
    qs = Suburb.objects.filter(state__code=state, name__iexact=locality)
    return qs.filter(postcode=postcode).first() or qs.first()


AMENITIES = [
    ("reservable", "예약 가능"), ("delivery", "배달"), ("takeout", "포장"), ("dineIn", "매장 식사"),
    ("outdoorSeating", "야외 좌석"), ("goodForGroups", "단체 가능"), ("goodForChildren", "아이 동반"),
    ("menuForChildren", "어린이 메뉴"), ("servesVegetarianFood", "채식 메뉴"), ("allowsDogs", "반려견 동반"),
    ("liveMusic", "라이브 음악"), ("restroom", "화장실"),
]


def amenities_value(place):
    chips = []
    if any((place.get("parkingOptions") or {}).values()):
        chips.append("주차 가능")
    chips += [label for key, label in AMENITIES if place.get(key) is True]
    if (place.get("accessibilityOptions") or {}).get("wheelchairAccessibleEntrance"):
        chips.append("휠체어 출입")
    pay = place.get("paymentOptions") or {}
    if pay.get("acceptsCreditCards") or pay.get("acceptsDebitCards"):
        chips.append("카드 결제")
    if pay.get("acceptsNfc"):
        chips.append("간편결제")
    return ", ".join(chips)


SOCIAL_HOSTS = ("instagram.com", "facebook.com", "fb.com", "tiktok.com", "youtube.com", "youtu.be",
                "pf.kakao.com", "blog.naver.com", "linktr.ee", "x.com", "twitter.com", "threads.net")


def is_social(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def maps_link(name, address, place_id):
    """Google's official Maps URL for the place: opens the store's own
    listing. The store page (detail_info.html) embeds a map from the same
    link by searching the name and address, which pins the same store; the
    store cards' map buttons use the link as it is."""
    return (f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name}, {address}')}"
            f"&query_place_id={place_id}")


# ── discovery: where and what to search ───────────────────────────────
# (name, lat, lng, search radius m, use every search?) — the Korean
# communities and every capital; smaller places get the short list.
AREAS = [
    ("Strathfield NSW", -33.8730, 151.0940, 4000, True), ("Eastwood NSW", -33.7920, 151.0820, 4000, True),
    ("Lidcombe NSW", -33.8640, 151.0450, 4000, True), ("Campsie NSW", -33.9120, 151.1030, 4000, True),
    ("Sydney CBD NSW", -33.8730, 151.2070, 4000, True), ("Chatswood NSW", -33.7960, 151.1830, 4000, True),
    ("Parramatta NSW", -33.8150, 151.0010, 5000, True), ("Epping NSW", -33.7720, 151.0820, 4000, True),
    ("Burwood NSW", -33.8770, 151.1040, 3000, True), ("North Sydney NSW", -33.8390, 151.2070, 4000, True),
    ("Rhodes NSW", -33.8300, 151.0870, 3000, True), ("Macquarie Park NSW", -33.7770, 151.1210, 4000, True),
    ("Hurstville NSW", -33.9670, 151.1020, 5000, True), ("Castle Hill NSW", -33.7300, 151.0040, 6000, True),
    ("Blacktown NSW", -33.7710, 150.9060, 7000, False), ("Liverpool NSW", -33.9200, 150.9230, 7000, False),
    ("Hornsby NSW", -33.7030, 151.0990, 6000, False), ("Bankstown NSW", -33.9180, 151.0350, 5000, False),
    ("Penrith NSW", -33.7510, 150.6940, 10000, False), ("Melbourne CBD VIC", -37.8136, 144.9631, 5000, True),
    ("Box Hill VIC", -37.8190, 145.1220, 5000, True), ("Glen Waverley VIC", -37.8780, 145.1650, 5000, True),
    ("Clayton VIC", -37.9250, 145.1200, 5000, True), ("Doncaster VIC", -37.7880, 145.1260, 5000, False),
    ("Brisbane CBD QLD", -27.4698, 153.0251, 6000, True), ("Sunnybank QLD", -27.5800, 153.0600, 6000, True),
    ("Surfers Paradise QLD", -28.0023, 153.4310, 8000, True), ("Southport QLD", -27.9670, 153.4000, 6000, False),
    ("Perth WA", -31.9505, 115.8605, 8000, True), ("Adelaide SA", -34.9285, 138.6007, 8000, True),
    ("Canberra ACT", -35.2809, 149.1300, 10000, True), ("Hobart TAS", -42.8821, 147.3272, 10000, False),
    ("Darwin NT", -12.4634, 130.8456, 10000, False), ("Newcastle NSW", -32.9283, 151.7817, 10000, False),
    ("Wollongong NSW", -34.4278, 150.8931, 10000, False), ("Cairns QLD", -16.9186, 145.7781, 10000, False),
]
DISCOVER_QUERIES = [  # the first SHORT_QUERIES are used everywhere, the rest in the main areas
    "Korean restaurant", "Korean grocery", "Korean church", "Korean hair salon", "Korean BBQ", "한인",
    "Korean fried chicken", "Korean cafe bakery", "Korean beauty skin clinic", "Korean dentist",
    "Korean doctor clinic", "Korean acupuncture herbal medicine", "Korean accountant", "Korean lawyer",
    "Korean real estate agent", "Korean migration agent", "Korean education agency 유학원",
    "Korean tutoring academy", "Korean travel agency", "Korean karaoke", "Korean car mechanic", "한식", "한인 마트",
]
SHORT_QUERIES = 6

# Sweeps (일식당 전체, 프랜차이즈): every place of a kind, found by searching
# rectangles and splitting any rectangle that hits Google's 60-result cap.
AUSTRALIA = (-44.0, 112.5, -10.0, 154.0)
MIN_CELL = 0.004  # about 400 m: never split smaller


def area_rect(lat, lng, radius):
    dlat = radius / 111000
    dlng = radius / (111000 * math.cos(math.radians(lat)))
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


# Where many Koreans live (the main discovery areas): 일식당 and 프랜차이즈 are
# looked for only here, which keeps their number (and cost) down.
KOREAN_AREAS = [(name, area_rect(lat, lng, radius)) for name, lat, lng, radius, main in AREAS if main]
JAPANESE_SEARCHES = [("Japanese restaurant", "japanese_restaurant"), ("Sushi", "sushi_restaurant"),
                     ("Ramen", "ramen_restaurant")]
# 인기 중식당: only places rated this well by this many people; the run keeps the best `limit` (default 100).
CHINESE_MIN_RATING, CHINESE_MIN_REVIEWS = 4.3, 100
GRID = 0.002  # about 200 m: map cells for matching discovered places to listings


# Why a place was found, for Claude's is_relevant judgement (store_writer §5).
DISCOVERY_REASONS = {
    "job_employer": "job_employer: 한인 구인 게시판에 구인 글을 올린 고용 업체",
    "admin": "admin: 관리자가 구글 지도 링크로 직접 추가한 업체",
    "japanese": "japanese: 일식당 찾기 (한인 운영 여부와 관계없이 등록)",
    "chinese": "chinese: 평점과 리뷰가 좋은 인기 중식당 (한인 운영 여부와 관계없이 등록)",
}


MAPS_HOST = re.compile(r"(?:^|\.)(?:google\.(?:com|com\.au|co\.kr|co\.nz)|goo\.gl)$")


def is_maps_link(url):
    """A Google Maps link: https or http, on a Google or goo.gl host. The host
    is checked itself, so a path that only mentions google.com/maps on
    another site does not pass."""
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("https", "http") and bool(MAPS_HOST.search(host)) and (
        host.endswith("goo.gl") or "/maps" in parts.path or host.startswith("maps."))


def _fetch_maps_url(url):
    """Follow a maps link's redirects and return where it ends, without ever
    leaving Google's hosts (the server must not fetch arbitrary or internal
    addresses on behalf of a pasted link)."""
    import urllib.parse
    import urllib.request

    class GoogleOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parts = urllib.parse.urlsplit(newurl)
            if parts.scheme not in ("https", "http") or not MAPS_HOST.search((parts.hostname or "").lower()):
                raise ValueError(f"redirect outside Google: {newurl[:80]}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if not is_maps_link(url):
        return url
    opener = urllib.request.build_opener(GoogleOnly)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KoreanPagesBot)"})
    with opener.open(req, timeout=20) as resp:  # nosec B310 - host allow-listed above, every hop checked
        return resp.geturl()


def maps_link_place(url, places):
    """The Google place ID of a Google Maps link (short maps.app.goo.gl links are
    followed, on Google's hosts only): from the link itself, else its name
    near its map position."""
    import urllib.parse

    try:
        final = _fetch_maps_url(url)
    except Exception:
        final = url
    final = urllib.parse.unquote(final)
    m = re.search(r"(?:query_place_id|place_id)[=:](ChIJ[\w-]+)", final) or re.search(r"\b(ChIJ[\w-]{20,})", final)
    if m:
        return m.group(1)
    name = re.search(r"/maps/place/([^/@?]+)", final)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query).get("q", [""])[0]
    text = (name.group(1).replace("+", " ") if name else q).strip()
    at = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", final) or re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final)
    if not text:
        return ""
    if at:
        ids = places.search_ids(text, lat=float(at.group(1)), lng=float(at.group(2)), radius=300, max_results=1)
    else:
        ids = places.search_ids(text + " Australia", max_results=1)
    return ids[0] if ids else ""


def korean_signal(place):
    """Some sign that a place is Korean: Hangul or "Korea" in its name,
    type, summary or website, or reviews written in Korean. Places without
    one are not sent to Claude (keeps discovery affordable)."""
    texts = [(place.get("displayName") or {}).get("text", ""),
             (place.get("primaryTypeDisplayName") or {}).get("text", ""),
             (place.get("editorialSummary") or {}).get("text", ""),
             place.get("websiteUri", ""), " ".join(place.get("types") or [])]
    for review in place.get("reviews") or []:
        original = review.get("originalText") or {}
        if original.get("languageCode") == "ko":
            return True
        texts.append(original.get("text", ""))
    blob = " ".join(texts)
    return bool(re.search(r"[가-힣]", blob)) or "korea" in blob.lower() or ".kr" in blob.lower()


def pending_facts(proposal, store):
    """Claude's input for re-judging a waiting Google suggestion, built from
    what the proposal already holds (Google's name, address, phone, rating,
    hours and the draft description written from Google's data)."""
    ev, ch, raw = proposal.evidence or {}, proposal.changes or {}, store.raw
    return {
        "mode": "replace",
        "site_listing": {"title": raw.get("title", ""), "short_comment": raw.get("short_comment", ""),
                         "category": raw.get("category", ""), "phone": raw.get("phone", ""),
                         "address": raw.get("address", ""), "hours_text": raw.get("build_date", ""),
                         "existing_description": plain(raw.get("review", ""))[:1500]},
        "google_candidates": [{
            "index": 0, "name": ev.get("google_name", ""), "address": ev.get("google_address") or ch.get("address", ""),
            "phone": ev.get("google_phone") or ch.get("phone", ""), "website": ev.get("google_website", ""),
            "business_status": ev.get("business_status", ""),
            "opening_hours": [ch["build_date"]] if ch.get("build_date") else [],
            "rating": ev.get("rating"), "rating_count": ev.get("rating_count"), "amenities": ch.get("etc1", ""),
            "draft_description_from_google_data": plain(ch.get("review", ""))[:1500],
        }],
    }


def apply_business_change(values, written):
    """When Google shows a different business at this listing (renamed or a
    different trade), its own one-line intro and category replace the old
    ones; the title and description already describe it (store_writer)."""
    if not (written.get("name_changed") or written.get("category_changed")):
        return
    if written.get("short_comment"):
        values["short_comment"] = written["short_comment"]
    theme = written.get("category", "")
    pairs = theme_pairs()
    if written.get("category_changed") and theme in pairs:
        values["category"], values["category2"], values["category3"] = theme, pairs[theme], ""


def has_both_scripts(title):
    return bool(re.search(r"[가-힣]", title or "")) and bool(re.search(r"[A-Za-z]{2,}", title or ""))


def cleanup_note(job_pk):
    return f"정리 작업 #{job_pk}:"


# ── matching a store to a Google place ────────────────────────────────
GENERIC = {"the", "and", "restaurant", "cafe", "korean", "japanese", "sydney", "melbourne", "brisbane",
           "pty", "ltd", "shop", "store", "co", "group", "australia", "bbq", "church", "of"}


def latin_tokens(text):
    return {t for t in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if t not in GENERIC}


def hangul(text):
    return re.sub(r"[^가-힣]", "", text or "")


def distance_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def store_coords(raw):
    try:
        lat, lng = float(raw.get("x_point") or 0), float(raw.get("y_point") or 0)
    except ValueError:
        return None
    if not lat or not lng or not (-45 < lat < -9 and 110 < lng < 155):
        return None
    return lat, lng


def hangul_similarity(a, b):
    """Share of the shorter name's syllable pairs found in the other (Korean names)."""
    ba = {a[i:i + 2] for i in range(len(a) - 1)}
    bb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(ba & bb) / min(len(ba), len(bb)) if ba and bb else 0


def match_score(raw, place):
    """0-1 score plus the parts behind it (shown as evidence)."""
    parts = {}
    ours = digits(raw.get("phone"))
    theirs = digits(place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"))
    parts["phone_match"] = bool(ours and theirs and len(ours) >= 8 and ours[-8:] == theirs[-8:])
    coords, loc = store_coords(raw), place.get("location") or {}
    if coords and loc:
        parts["distance_m"] = round(distance_m(coords[0], coords[1], loc["latitude"], loc["longitude"]))
    name = (place.get("displayName") or {}).get("text", "")
    a, b = latin_tokens(raw.get("title")), latin_tokens(name)
    parts["name_overlap"] = round(len(a & b) / len(a), 2) if a else 0
    parts["hangul_match"] = hangul_similarity(hangul(raw.get("title")), hangul(name)) >= 0.5

    score = 0.55 if parts["phone_match"] else 0
    d = parts.get("distance_m")
    if d is not None:
        score += 0.3 if d <= 150 else 0.2 if d <= 500 else 0.05 if d <= 2000 else -0.4 if d > 5000 else 0
    score += min(0.3, (0.3 if parts["name_overlap"] >= 0.5 else 0.15 if parts["name_overlap"] > 0 else 0)
                 + (0.2 if parts["hangul_match"] else 0))
    parts["verified"] = parts["phone_match"] or (d is not None and d <= 300 and
                                                 (parts["name_overlap"] >= 0.5 or parts["hangul_match"]))
    return max(0.0, min(1.0, round(score, 2))), parts


def phone_queries(raw):
    """The listing's phone number written the way Google Maps shows it —
    "(02) 9809 1588", "0412 345 678", "1300 123 456". Text Search finds a
    place by its number in this form (about three times in four when the
    number is on Google, and never a different place); other spellings
    such as +61… mostly return nothing."""
    d = re.sub(r"\D", "", re.split(r"[~/,]", raw.get("phone") or "")[0])
    if d.startswith("61"):
        d = "0" + d[2:]
    if len(d) != 10:
        return []
    if d[:2] in ("02", "03", "07", "08"):
        return [f"({d[:2]}) {d[2:6]} {d[6:]}", f"{d[:2]} {d[2:6]} {d[6:]}"]
    if d.startswith("04") or d[:4] in ("1300", "1800"):
        return [f"{d[:4]} {d[4:7]} {d[7:]}"]
    return []


def search_query(raw, suburb_name):
    title = re.sub(r"\s+", " ", raw.get("title") or "").strip()
    return f"{title} {suburb_name}".strip()


def find_places(places, store, raw, *, phone_only=False):
    """Google candidates for a store as [(place, score, parts)], best first.

    Searches by phone number first (many old listings have only a Korean
    name and a number), then by name near the store's map position and by
    name + street. phone_only skips the name searches (the retry pass for
    stores those already failed to find).
    At most MAX_CANDIDATES places are looked at in detail (the paid
    call); a near-certain match (score >= 0.8) is returned on its own."""
    gu = str(raw.get("gu", ""))
    suburb = Suburb.objects.filter(pk=int(gu)).select_related("state").first() if gu.isdigit() else None
    coords = store_coords(raw)
    area = f"{suburb.name} {suburb.state.code}" if suburb else ""
    if coords:
        lat, lng = coords
    elif suburb and suburb.lat:
        lat, lng = float(suburb.lat), float(suburb.lng)
    else:
        lat, lng = None, None
    known = StorePlace.objects.filter(store_id=store.pk).exclude(place_id="").first()
    if known:
        # Already matched (by an earlier run or the admin): look that place
        # up directly, no searching.
        place = places.details(known.place_id)
        score, parts = match_score(raw, place)
        return [(place, score, {**parts, "known": True})]
    results = []
    for query in phone_queries(raw):
        ids = places.search_ids(query, max_results=3)
        if ids:
            results.append(ids)
            break
    queries = []
    if not phone_only:
        queries.append(search_query(raw, area))
        street = (raw.get("address") or "").strip()
        if street:
            queries.append(f"{raw.get('title', '').strip()} {street} {area}".strip())
    for query in queries:
        results.append(places.search_ids(query, lat=lat, lng=lng, radius=1500 if coords else 5000, max_results=5))
    # Take the searches' results in turn (1st of each, then 2nd of each ...).
    order = []
    for rank in range(5):
        for ids in results:
            if rank < len(ids) and ids[rank] not in order:
                order.append(ids[rank])
    found = []
    for pid in order[:MAX_CANDIDATES]:
        place = places.details(pid)
        score, parts = match_score(raw, place)
        if score >= 0.8:
            return [(place, score, parts)]
        found.append((place, score, parts))
    return sorted(found, key=lambda c: -c[1])


def worth_checking(score, parts):
    """Candidates Claude should see: anything scoring as a likely match, or
    anything not ruled out by distance (a store that moved keeps its
    name; a Google name may be the English translation of ours)."""
    if score >= 0.5 or parts.get("name_overlap", 0) >= 0.5 or parts.get("hangul_match"):
        return True
    d = parts.get("distance_m")
    return d is None or d <= 50000


# ── building the proposed listing ─────────────────────────────────────
def plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", html or "")).strip()


def google_facts(place, index):
    suburb = suburb_for(place)
    reviews = []
    for r in (place.get("reviews") or [])[:4]:  # enough for a summary; keeps each request small
        text = (r.get("text") or {}).get("text") or (r.get("originalText") or {}).get("text") or ""
        if text:
            reviews.append({"rating": r.get("rating"), "text": text[:400]})
    return {
        "index": index,
        "name": (place.get("displayName") or {}).get("text", ""),
        "type": (place.get("primaryTypeDisplayName") or {}).get("text", ""),
        "types": place.get("types", [])[:8],
        "address": address_value(place),
        "suburb": f"{suburb.name}, {suburb.state.code}" if suburb else "",
        "phone": phone_value(place),
        "website": place.get("websiteUri", ""),
        "business_status": place.get("businessStatus", ""),
        "opening_hours": (place.get("regularOpeningHours") or {}).get("weekdayDescriptions", []),
        "rating": place.get("rating"), "rating_count": place.get("userRatingCount"),
        "price_level": place.get("priceLevel", ""),
        "editorial_summary": (place.get("editorialSummary") or {}).get("text", ""),
        "amenities": amenities_value(place),
        "reviews": reviews,
    }


def facts_for(raw, candidates, *, mode):
    """What Claude sees: our current listing and the Google candidates."""
    return {
        "mode": mode,
        "site_listing": None if mode == "new" else {
            "title": raw.get("title", ""), "short_comment": raw.get("short_comment", ""),
            "category": raw.get("category", ""), "phone": raw.get("phone", ""),
            "address": raw.get("address", ""), "hours_text": raw.get("build_date", ""),
            "existing_description": plain(raw.get("review", ""))[:1500],
        },
        "google_candidates": [google_facts(p, i) for i, p in enumerate(candidates)],
    }


def listing_values(raw, place, written, suburb):
    """Legacy column values for the store page."""
    loc = place.get("location") or {}
    address = address_value(place)
    name = written.get("title") or (place.get("displayName") or {}).get("text", "")
    values = {
        "title": name,
        "review": written.get("description_html", ""),
        "phone": phone_value(place),
        "address": address,
    }
    if (raw.get("short_comment") or "").strip() in EMPTY:
        values["short_comment"] = written.get("short_comment", "")
    if loc:
        values["x_point"] = f"{loc['latitude']:.9f}"
        values["y_point"] = f"{loc['longitude']:.9f}"
        values["homepage"] = maps_link((place.get("displayName") or {}).get("text", name), address, place["id"])
    hours = hours_value(place.get("regularOpeningHours"))
    if hours:
        values["build_date"] = hours
    amenities = amenities_value(place)
    if amenities:
        values["etc1"] = amenities
    website = place.get("websiteUri") or ""
    if website and is_social(website):
        values["etc4"] = website
    if suburb:
        values["si"], values["gu"] = str(suburb.state_id), str(suburb.pk)
    return {k: v for k, v in values.items() if v not in (None, "")}


RATING_TEXT = re.compile(r"((?:Google|구글)\s*평점\s*)([0-9.]+)(\s*점?\s*\(\s*리뷰\s*)([0-9,]+)(\s*개\s*\))")


def rating_in_description(html, rating, count):
    """The description with its "Google 평점 4.6점 (리뷰 132개)" line brought
    up to date, or None when it has no such line or the numbers still match."""
    m = RATING_TEXT.search(html)
    if not m or rating is None or count is None:
        return None
    new_rating = f"{float(rating):.1f}"
    new_count = f"{int(count):,}" if "," in m.group(4) or int(count) >= 10000 else str(int(count))
    if m.group(2) == new_rating and m.group(4).replace(",", "") == str(int(count)):
        return None
    return html[:m.start()] + m.group(1) + new_rating + m.group(3) + new_count + m.group(5) + html[m.end():]


def evidence_for(place, score, parts, written, job):
    return {
        "google_verified": bool(parts.get("verified")) and bool(written.get("same_business")),
        "place_id": place.get("id"),
        "maps_url": place.get("googleMapsUri", ""),
        "business_status": place.get("businessStatus", ""),
        "fetched_at": timezone.now().isoformat(timespec="seconds"),
        "google_name": (place.get("displayName") or {}).get("text", ""),
        "google_address": address_value(place),
        "google_phone": phone_value(place),
        "google_website": place.get("websiteUri", ""),
        "rating": place.get("rating"), "rating_count": place.get("userRatingCount"),
        "match": {"score": score, **parts},
        "claude": {"same_business": written.get("same_business"), "reason": written.get("match_reason"),
                   "notes": written.get("notes"), "model": settings.STORE_WRITER_MODEL},
        "job": job.pk if job else None,
    }


class Runner:
    def __init__(self, job):
        self.job = job
        self.lock = threading.Lock()  # stores are processed in parallel threads
        self.new_lock = threading.Lock()  # approving discovered stores one at a time
        self.usage = job.usage or {}
        self.usage["job_id"] = job.pk  # so the monthly budget counts this run's live spend once
        self.daily_limited = False
        self.budget_error = None
        self._places = None  # opened when first needed (hiding and restoring don't use Google)
        self._writer = None
        self._category_writer = None

    @property
    def places(self):
        if self._places is None:
            self._places = google_places.Places(usage=self.usage)
        return self._places

    @property
    def writer(self):
        if self._writer is None:
            self._writer = store_writer.StoreWriter(usage=self.usage)
        return self._writer

    def log(self, line):
        stamp = timezone.localtime().strftime("%H:%M:%S")
        with self.lock:
            self.job.log = (self.job.log + f"{stamp} {line}\n")[-20000:]

    def count(self, field):
        with self.lock:
            setattr(self.job, field, getattr(self.job, field) + 1)

    # ── verify ──
    def not_found(self, store):
        """Remember that the store was checked and is not on Google Maps."""
        StorePlace.objects.update_or_create(store_id=store.pk, defaults={
            "place_id": "", "match_score": 0, "business_status": StorePlace.NOT_FOUND})

    def prepare(self, store):
        """Google step for one store. Returns the candidates Claude has to
        judge, or None when the store is already dealt with (not on Google,
        or certainly closed)."""
        raw = store.raw
        found = find_places(self.places, store, raw, phone_only=self.job.recheck_not_found)
        candidates = [c for c in found if worth_checking(c[1], c[2])]
        if not candidates:
            self.not_found(store)
            self.log(f"#{store.pk} {raw.get('title')}: 구글 지도에서 일치하는 장소를 찾지 못함")
            return None
        best, best_score, best_parts = candidates[0]
        if best.get("businessStatus") == "CLOSED_PERMANENTLY" and best_parts.get("verified") and best_score >= 0.8:
            # Certain match and closed for good: no description needed.
            self.finish(store, candidates, {"same_business": True, "candidate_index": 0,
                                            "match_reason": "전화번호·위치 일치", "notes": "Google: 영구 폐업"})
            return None
        return candidates

    def verify_store(self, store):
        """On-demand: Google step, Claude now, then the result."""
        candidates = self.prepare(store)
        if candidates:
            written = self.writer.write(facts_for(store.raw, [c[0] for c in candidates], mode="verify"))
            self.finish(store, candidates, written)

    def finish(self, store, candidates, written):
        """Turn Claude's answer into a proposal (the same for both paths)."""
        raw = store.raw
        index = written.get("candidate_index", -1)
        if not written.get("same_business") or not 0 <= index < len(candidates):
            self.not_found(store)
            self.log(f"#{store.pk} {raw.get('title')}: 같은 업체 없음 - {written.get('match_reason')}")
            return
        place, score, parts = candidates[index]
        suburb = suburb_for(place)
        if written.get("name_changed"):
            # Same phone or place but a different business name: a rename or a
            # new owner. Never applied unattended.
            score = min(score, 0.6)
            parts = {**parts, "verified": False,
                     "note": "상호가 기존과 전혀 다름 - 상호 변경·인수인지 확인해 주세요"}
        if parts.get("known") and not written.get("name_changed"):
            parts = {**parts, "verified": True}  # the place this store was already matched to
        if parts.get("verified"):
            # Google hard-confirms it (same phone, or same name at the same
            # spot) and Claude agrees: confident enough for the hybrid policy.
            score = max(score, 0.9)
        if score < 0.5:
            score = 0.5  # weak signals, but Claude judged it the same business: always reviewed
            parts = {**parts, "verified": False,
                     "note": "이름·전화·위치가 기존 정보와 다름 - 이전·상호 변경인지 확인해 주세요"}
        self.count("matched")
        StorePlace.objects.update_or_create(store_id=store.pk, defaults={
            "place_id": place["id"], "match_score": score, "business_status": place.get("businessStatus", "")})
        evidence = evidence_for(place, score, parts, written, self.job)
        allow_auto = self.job.auto_apply
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            proposal = services.submit_proposal(
                ChangeProposal.CLOSE, title=raw.get("title", ""), store_id=store.pk, changes={},
                evidence=evidence, confidence=score, allow_auto=allow_auto, recount=False)
            self.count("proposals")
            self.log(f"#{store.pk} {raw.get('title')}: 폐업(구글) -> 제안 #{proposal.pk}")
            return
        values = listing_values(raw, place, written, suburb)
        apply_business_change(values, written)
        if self.job.mode == StoreUpdateJob.REFRESH:
            values.pop("title", None)  # a refresh rewrites description and hours, not the approved name
        changes = {k: v for k, v in values.items() if (raw.get(k) or "") != v}
        if not changes:
            self.log(f"#{store.pk} {raw.get('title')}: 변경 없음")
            return
        proposal = services.submit_proposal(
            ChangeProposal.UPDATE, title=values.get("title") or raw.get("title", ""), store_id=store.pk,
            changes=changes, evidence=evidence, confidence=score, allow_auto=allow_auto, recount=False)
        self.count("proposals")
        self.log(f"#{store.pk} {raw.get('title')}: {len(changes)}개 항목 변경 제안 #{proposal.pk}"
                 f"{' (바로 반영)' if proposal.status == ChangeProposal.AUTO_APPLIED else ''}")

    # ── check: has anything changed on Google for a listed store? ──
    def check_store(self, store):
        link = StorePlace.objects.filter(store_id=store.pk).exclude(place_id="").first()
        if link is None:
            return
        place = self.places.check_details(link.place_id)
        raw, title = store.raw, store.raw.get("title", "")
        self.count("matched")
        name = (place.get("displayName") or {}).get("text", "")
        seen = []
        if link.rating_count is not None and place.get("userRatingCount") != link.rating_count:
            seen.append(f"리뷰 수 {link.rating_count} → {place.get('userRatingCount') or 0}")
        old_name = link.google_name
        StorePlace.objects.filter(pk=link.pk).update(
            business_status=place.get("businessStatus", ""), google_name=name, rating=place.get("rating"),
            rating_count=place.get("userRatingCount"), checked_at=timezone.now())
        written = {"same_business": True, "match_reason": "연결된 구글 장소 다시 확인", "notes": "월간 변경 확인"}
        evidence = evidence_for(place, 1.0, {"verified": True, "known": True}, written, self.job)
        allow_auto = self.job.auto_apply
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            proposal = services.submit_proposal(
                ChangeProposal.CLOSE, title=title, store_id=store.pk, changes={}, evidence=evidence,
                confidence=1.0, allow_auto=allow_auto, recount=False)
            self.count("proposals")
            self.log(f"#{store.pk} {title}: 폐업(구글) -> 제안 #{proposal.pk}"
                     f"{' (바로 반영)' if proposal.status == ChangeProposal.AUTO_APPLIED else ''}")
            return
        if place.get("businessStatus") == "CLOSED_TEMPORARILY":
            seen.append("구글: 임시 휴업")
        if old_name and name and old_name != name:
            # A rename or a new owner: never applied unattended.
            note = f"구글 상호가 '{old_name}' → '{name}'(으)로 바뀜 - 새 업체명을 확인해 주세요"
            proposal = services.submit_proposal(
                ChangeProposal.UPDATE, title=title, store_id=store.pk, changes={"title": name},
                evidence={**evidence, "google_verified": False, "match": {"score": 0.6, "note": note}},
                confidence=0.6, allow_auto=False, recount=False)
            self.count("proposals")
            self.log(f"#{store.pk} {title}: {note} -> 제안 #{proposal.pk}")
        changes = {}
        hours = hours_value(place.get("regularOpeningHours"))
        if hours and hours != (raw.get("build_date") or ""):
            changes["build_date"] = hours
        phone = phone_value(place)
        if phone and digits(phone) != digits(raw.get("phone")):
            changes["phone"] = phone
        loc, coords = place.get("location") or {}, store_coords(raw)
        if loc and coords and distance_m(coords[0], coords[1], loc["latitude"], loc["longitude"]) > 150:
            address = address_value(place)
            changes.update({"address": address, "x_point": f"{loc['latitude']:.9f}",
                            "y_point": f"{loc['longitude']:.9f}",
                            "homepage": maps_link(name or title, address, place["id"])})
            suburb = suburb_for(place)
            if suburb:
                changes["si"], changes["gu"] = str(suburb.state_id), str(suburb.pk)
        review = rating_in_description(raw.get("review") or "", place.get("rating"), place.get("userRatingCount"))
        if review:
            changes["review"] = review
        changes = {k: v for k, v in changes.items() if (raw.get(k) or "") != v}
        labels = {"build_date": "영업시간", "phone": "전화번호", "address": "주소(이전)", "review": "평점·리뷰 수"}
        if changes:
            proposal = services.submit_proposal(
                ChangeProposal.UPDATE, title=title, store_id=store.pk, changes=changes, evidence=evidence,
                confidence=0.95, allow_auto=allow_auto, recount=False)
            self.count("proposals")
            seen.insert(0, ", ".join(labels[k] for k in labels if k in changes) + f" 변경 -> 제안 #{proposal.pk}"
                        + (" (바로 반영)" if proposal.status == ChangeProposal.AUTO_APPLIED else ""))
        if seen:
            self.log(f"#{store.pk} {title}: " + " · ".join(seen))

    # ── discover ──
    def store_index(self):
        """Every store (shown or hidden) by phone number and by map area, so a
        place found by discovery is matched to an existing listing quickly."""
        with self.lock:
            if getattr(self, "_index", None) is None:
                phones, grid = {}, collections.defaultdict(list)
                for store in Store.all_objects.all().only("id", "raw", "closed"):
                    d = digits(store.raw.get("phone"))
                    if len(d) >= 8:
                        phones.setdefault(d[-8:], store)
                    coords = store_coords(store.raw)
                    if coords:
                        grid[(round(coords[0] / GRID), round(coords[1] / GRID))].append(store)
                self._index = (phones, grid)
        return self._index

    def existing_store(self, place):
        """The listing this place already is (same phone, or same name within
        150 m), shown or hidden, or None."""
        phones, grid = self.store_index()
        d = digits(place.get("nationalPhoneNumber"))
        if len(d) >= 8 and d[-8:] in phones:
            return phones[d[-8:]]
        loc = place.get("location") or {}
        if not loc:
            return None
        cx, cy = round(loc["latitude"] / GRID), round(loc["longitude"] / GRID)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for store in grid.get((cx + dx, cy + dy), ()):
                    lat, lng = store_coords(store.raw)
                    if distance_m(lat, lng, loc["latitude"], loc["longitude"]) <= 150:
                        _, parts = match_score(store.raw, place)
                        if parts["name_overlap"] >= 0.5 or parts["hangul_match"]:
                            return store
        return None

    def gather_discover(self, item):
        """Discovery step 1 for one (area, search): Google now, Claude later."""
        try:
            if item.startswith("sweep|"):
                return self.sweep(item)
            if item.startswith("brand|"):
                return self.brand_sweep(item)
            if item.startswith("link:"):
                return self.from_link(item[5:])
            if item.startswith("place:"):
                # A business named in a Korean job post (콘텐츠 가져오기): it hires
                # in the Korean community, so no Korean-name check.
                self.consider(self.places.details(item[6:]), korean_known=True, reason="job_employer")
                return
            ai, qi = (int(x) for x in item.split("|"))
            name, lat, lng, radius, _ = (self.usage.get("areas") or AREAS)[ai]
            query = f"{DISCOVER_QUERIES[qi]} {name}"
            if self.job.skip_checked:
                return self.discover_unseen(query, lat, lng, radius)
            for place in self.places.search_places(query, lat=lat, lng=lng, radius=radius):
                self.consider(place)
        except google_places.PlacesError as e:
            self.count("errors")
            self.log(f"{item}: 오류 - {e}")
        except Exception as e:
            self.count("errors")
            self.log(f"{item}: 오류 - {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            connection.close()

    # ── sweeps: 일식당 전체 / 프랜차이즈 ──
    def sweep_rects(self):
        """Where a sweep looks: 전체 = the areas where many Koreans live; or one
        state's Korean areas; or around one suburb."""
        job = self.job
        if job.scope == StoreUpdateJob.SUBURB and job.suburb_id and job.suburb.lat:
            return [area_rect(float(job.suburb.lat), float(job.suburb.lng), 3000)]
        if job.scope == StoreUpdateJob.STATE and job.state_id:
            return [rect for name, rect in KOREAN_AREAS if name.endswith(" " + job.state.code)]
        return [rect for _, rect in KOREAN_AREAS]

    def sweep_work(self):
        job = self.job
        searches = {}
        if job.target == StoreUpdateJob.JAPANESE:
            for i, (text, kind) in enumerate(JAPANESE_SEARCHES):
                searches[f"j{i}"] = {"text": text, "type": kind}
        elif job.target == StoreUpdateJob.CHINESE:
            searches["c0"] = {"text": "Chinese restaurant", "type": "chinese_restaurant"}
        else:
            from . import franchise

            brands = Franchise.objects.filter(enabled=True)
            ids = [int(x) for x in re.findall(r"\d+", job.store_ids or "")]
            if ids:
                brands = Franchise.objects.filter(pk__in=ids)
            for brand in brands:
                franchise.brand_category(brand)
                franchise.ensure_intro(brand, self.usage)
                try:
                    franchise.ensure_logo(brand)
                except Exception as e:  # a missing logo must not stop the run
                    self.log(f"{brand.name}: 로고를 가져오지 못함 - {e}")
                searches[f"f{brand.pk}"] = {"text": brand.search, "brand": brand.pk}
        self.usage["sweep"] = searches
        rects = [",".join(f"{x:.5f}" for x in rect) for rect in self.sweep_rects()]
        kind = "brand" if job.target == StoreUpdateJob.FRANCHISE else "sweep"
        self.log(f"찾을 곳 {len(rects)}개 지역 × 검색 {len(searches)}개")
        return [f"{kind}|{key}|{rect}" for rect in rects for key in searches]

    def sweep(self, item):
        _, key, rect = item.split("|")
        rect = tuple(float(x) for x in rect.split(","))
        search = self.usage["sweep"][key]
        ids = list(dict.fromkeys(self.places.search_ids_rect(search["text"], rect, search.get("type"))))
        lat1, lng1, lat2, lng2 = rect
        if len(ids) >= 60 and (lat2 - lat1) > MIN_CELL:
            # Google stops at 60: look at the four quarters instead.
            mlat, mlng = (lat1 + lat2) / 2, (lng1 + lng2) / 2
            quarters = [(lat1, lng1, mlat, mlng), (lat1, mlng, mlat, lng2), (mlat, lng1, lat2, mlng), (mlat, mlng, lat2, lng2)]
            with self.lock:
                self.usage["work"].extend(f"sweep|{key}|" + ",".join(f"{x:.5f}" for x in q) for q in quarters)
                self.job.total = len(self.usage["work"])
            return
        brand = Franchise.objects.filter(pk=search["brand"]).first() if search.get("brand") else None
        listed = dict(StorePlace.objects.filter(place_id__in=ids).exclude(place_id="")
                      .values_list("place_id", "store_id"))
        if brand:
            from . import franchise

            for store in Store.objects.filter(pk__in=listed.values()):
                if franchise.matches(brand, store.raw.get("title")) or franchise.matches(brand, store.title):
                    with self.new_lock:
                        franchise.link_store(store, brand, note=f"프랜차이즈 {brand.name} 매장으로 분류")
        mark = "cn" if self.job.target == StoreUpdateJob.CHINESE else "jp"
        known = set(DiscoveredPlace.objects.filter(place_id__in=[f"{mark}:{pid}" for pid in ids])
                    .values_list("place_id", flat=True)) if not brand else set()
        with self.lock:
            seen = self.__dict__.setdefault("_sweep_seen", set())
            new = [pid for pid in ids if pid not in listed and (key, pid) not in seen and f"{mark}:{pid}" not in known]
            seen.update((key, pid) for pid in new)
        if not new:
            return
        price = google_places.PRICE
        pages = max(1, math.ceil(len(ids) / 20))
        if len(new) * price["details"] > pages * price["search_full"]:
            places = [p for p in self.places.search_places_rect(search["text"], rect, search.get("type"), pages=pages)
                      if p.get("id") in new]
        else:
            places = [self.places.details(pid) for pid in new]
        for place in places:
            if brand:
                self.franchise_place(place, brand)
            elif mark == "cn":
                # Only well-liked, popular places; the best ones are kept after the search.
                if (place.get("rating") or 0) >= CHINESE_MIN_RATING and (place.get("userRatingCount") or 0) >= CHINESE_MIN_REVIEWS:
                    self.consider(place, korean_known=True, reason="chinese")
            else:
                self.consider(place, korean_known=True, reason="japanese")
        if not brand:  # remembered, so the next run (신규 업체 크롤링) skips them for free
            DiscoveredPlace.objects.bulk_create([DiscoveredPlace(place_id=f"{mark}:{p['id']}") for p in places
                                                 if p.get("id")], ignore_conflicts=True)

    def brand_sweep(self, item):
        """One brand in one area: page through Google's results for the brand's
        name and keep the brand's own stores. Stops when a page is mostly
        other places (the brand has no more stores there)."""
        from . import franchise

        _, key, rect = item.split("|")
        rect = tuple(float(x) for x in rect.split(","))
        search = self.usage["sweep"][key]
        brand = Franchise.objects.filter(pk=search["brand"]).first()
        if brand is None:
            return
        # Free check: if every place Google lists here was looked at before, nothing to pay for.
        ids = self.places.search_ids_rect(search["text"], rect)
        prefix = f"fr{brand.pk}:"
        known = set(StorePlace.objects.filter(place_id__in=ids).values_list("place_id", flat=True))
        known |= {p[len(prefix):] for p in DiscoveredPlace.objects.filter(
            place_id__in=[prefix + i for i in ids]).values_list("place_id", flat=True)}
        if all(i in known for i in ids):  # (also when Google lists nothing here)
            return
        token = None
        for n in range(3):
            places, token = self.places.search_rect_page(search["text"], rect, token)
            DiscoveredPlace.objects.bulk_create([DiscoveredPlace(place_id=prefix + p["id"]) for p in places
                                                 if p.get("id")], ignore_conflicts=True)
            mine = [p for p in places if franchise.matches(brand, (p.get("displayName") or {}).get("text", ""))]
            for place in mine:
                with self.lock:
                    seen = self.__dict__.setdefault("_sweep_seen", set())
                    if place["id"] in seen:
                        continue
                    seen.add(place["id"])
                self.franchise_place(place, brand)
            if len(places) < 20 or len(mine) < 15 or (n < 2 and not token):
                return  # the brand has no more stores here
            if n < 2:
                time.sleep(2)  # the next page becomes valid after a short delay
        lat1, lng1, lat2, lng2 = rect
        if lat2 - lat1 > MIN_CELL:  # 60 of the brand's stores here: look at the quarters for the rest
            mlat, mlng = (lat1 + lat2) / 2, (lng1 + lng2) / 2
            quarters = [(lat1, lng1, mlat, mlng), (lat1, mlng, mlat, lng2), (mlat, lng1, lat2, mlng), (mlat, mlng, lat2, lng2)]
            with self.lock:
                self.usage["work"].extend(f"brand|{key}|" + ",".join(f"{x:.5f}" for x in q) for q in quarters)
                self.job.total = len(self.usage["work"])

    def franchise_place(self, place, brand):
        """One of the brand's stores: link it if listed, else add it (no Claude)."""
        from . import franchise

        name = (place.get("displayName") or {}).get("text", "")
        if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL" or not franchise.matches(brand, name):
            return
        linked = StorePlace.objects.filter(place_id=place["id"]).values_list("store_id", flat=True).first()
        store = Store.all_objects.filter(pk=linked).first() if linked else self.existing_store(place)
        with self.new_lock:
            if store is not None:
                if store.closed:
                    return  # a hidden listing (e.g. a merged duplicate): left alone
                StorePlace.objects.get_or_create(store_id=store.pk, defaults={
                    "place_id": place["id"], "business_status": place.get("businessStatus", "")})
                if franchise.link_store(store, brand, note=f"프랜차이즈 {brand.name} 매장으로 분류"):
                    self.count("matched")
                return
        suburb = suburb_for(place)
        values = franchise.store_values(brand, place, suburb, theme_pairs())
        evidence = evidence_for(place, 1.0, {"verified": True},
                                {"same_business": True, "match_reason": f"{brand.english} 매장"}, self.job)
        proposal = services.submit_proposal(
            ChangeProposal.NEW, title=values.get("title") or name, changes=values, evidence=evidence,
            confidence=0.0, allow_auto=False, recount=False)
        self.approve_new(proposal, count=False)
        proposal.refresh_from_db(fields=["status"])
        if proposal.status == ChangeProposal.APPROVED:
            self.count("proposals")
        else:
            self.log(f"{values.get('title') or name}: 정보가 부족해 '자동 업데이트 검토'로 보냄 (제안 #{proposal.pk})")

    def discover_unseen(self, query, lat, lng, radius):
        """One search, paying only for places no earlier run has looked at:
        the ID-only search is free, then the new places are fetched either
        one by one or with a full search, whichever costs less."""
        ids = list(dict.fromkeys(self.places.search_ids_all(query, lat=lat, lng=lng, radius=radius)))
        seen = set(DiscoveredPlace.objects.filter(place_id__in=ids).values_list("place_id", flat=True))
        seen |= set(StorePlace.objects.filter(place_id__in=ids).values_list("place_id", flat=True))
        new = [pid for pid in ids if pid not in seen]
        if not new:
            return
        pages = max(1, math.ceil(len(ids) / 20))
        price = google_places.PRICE
        if len(new) * price["details"] > pages * price["search_full"]:
            places = [p for p in self.places.search_places(query, lat=lat, lng=lng, radius=radius)
                      if p.get("id") not in seen]
        else:
            places = [self.places.details(pid) for pid in new]
        for place in places:
            self.consider(place)
        DiscoveredPlace.objects.bulk_create(
            [DiscoveredPlace(place_id=pid) for pid in {*new, *(p.get("id") for p in places if p.get("id"))}],
            ignore_conflicts=True)

    def from_link(self, url):
        """구글 지도 링크로 업체 추가: the link's place, added like a discovered one."""
        place_id = maps_link_place(url, self.places)
        if not place_id:
            self.log(f"{url}: 구글 지도에서 이 링크의 장소를 찾지 못했습니다")
            return
        listed = StorePlace.objects.filter(place_id=place_id).exclude(place_id="").values_list("store_id", flat=True).first()
        if listed and Store.objects.filter(pk=listed).exists():
            self.log(f"{url}: 이미 등록된 업체 #{listed}")
            return
        place = self.places.details(place_id)
        name = (place.get("displayName") or {}).get("text", "")
        store = self.existing_store(place)
        if store is not None and not store.closed:
            StorePlace.objects.get_or_create(store_id=store.pk, defaults={"place_id": place_id})
            self.log(f"{name}: 이미 등록된 업체 #{store.pk}")
            return
        self.consider(place, korean_known=True, reason="admin")
        self.log(f"{name}: 등록할 업체로 올림")

    def consider(self, place, korean_known=False, reason=""):
        """Keep a discovered place for Claude if it may be a Korean business
        the site does not list yet."""
        pid = place.get("id")
        if not pid or place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
            return
        tally = self.usage.setdefault("discover", {})
        if StorePlace.objects.filter(place_id=pid, store_id__in=Store.objects.values("id")).exists():
            return  # already listed and matched
        store = self.existing_store(place)
        if store is not None and not store.closed:
            StorePlace.objects.get_or_create(store_id=store.pk, defaults={
                "place_id": pid, "business_status": place.get("businessStatus", "")})
            return  # already listed: remember its place for later runs
        if not korean_known and not korean_signal(place):
            with self.lock:
                tally["no_korean_sign"] = tally.get("no_korean_sign", 0) + 1
            return
        try:
            with transaction.atomic():
                StoreBatchItem.objects.create(job=self.job, store_id=store.pk if store else 0, place_id=pid,
                                              candidates=[[place, 0, {"reason": reason} if reason else {}]])
            with self.lock:
                tally["candidates"] = tally.get("candidates", 0) + 1
        except IntegrityError:
            pass  # found by another search in this run

    def category_writer(self):
        if self._category_writer is None:
            listing = "\n".join(f"- {theme}" for theme in theme_pairs())
            self._category_writer = store_writer.StoreWriter(usage=self.usage, category_list=listing)
        return self._category_writer

    def finish_discover(self, item, written):
        """A discovered place → a 신규 업체 proposal (or, for a listing that
        was hidden, a proposal to show it again). Always reviewed."""
        place = item.candidates[0][0]
        name = (place.get("displayName") or {}).get("text", "")
        if not written.get("is_relevant"):
            self.log(f"{name}: 한인 업체 아님 - {written.get('relevance_reason')}")
            return
        suburb = suburb_for(place)
        pairs = theme_pairs()
        theme = written.get("category", "")
        reason = (item.candidates[0][2] or {}).get("reason") if len(item.candidates[0]) > 2 else ""
        if reason == "japanese" and "일식" not in theme:
            theme = next((t for t in pairs if t.endswith("/일식")), theme)
        if reason == "chinese" and "중식" not in theme:
            theme = next((t for t in pairs if t.endswith("/중식")), theme)
        evidence = evidence_for(place, 0.0, {"verified": False}, {**written, "same_business": True}, self.job)
        hidden = Store.all_objects.filter(pk=item.store_id).first() if item.store_id else None
        if hidden is not None:
            values = listing_values(hidden.raw, place, written, suburb)
            changes = {k: v for k, v in values.items() if (hidden.raw.get(k) or "") != v}
            evidence["reopen"] = True
            evidence["note"] = "숨김 처리했던 업체가 구글 지도에서 영업 중으로 확인됨 - 승인하면 다시 표시됩니다"
            proposal = services.submit_proposal(
                ChangeProposal.UPDATE, title=values.get("title") or name, store_id=hidden.pk, changes=changes,
                evidence=evidence, confidence=0.5, allow_auto=False, recount=False)
            self.count("proposals")
            self.log(f"{name}: 숨긴 업체 #{hidden.pk} 다시 표시 제안 #{proposal.pk}")
            return
        values = listing_values({}, place, written, suburb)
        values["short_comment"] = written.get("short_comment", "")
        if theme in pairs:
            values["category"], values["category2"] = theme, pairs[theme]
        evidence["category_note"] = "" if theme in pairs else f"카테고리를 직접 골라 주세요 (제안: {theme})"
        proposal = services.submit_proposal(
            ChangeProposal.NEW, title=values.get("title") or name, changes=values,
            evidence=evidence, confidence=0.0, allow_auto=False, recount=False)
        self.count("proposals")
        if self.job.auto_apply:
            # 확실한 변경은 바로 반영: a complete listing is added now, the rest waits for review.
            self.approve_new(proposal, count=False)
            proposal.refresh_from_db(fields=["status"])
        added = proposal.status == ChangeProposal.APPROVED
        self.log(f"{values.get('title') or name}: 신규 업체 {'등록' if added else '제안'} #{proposal.pk}")

    # ── pending proposals: approve ready new stores, rewrite Google suggestions ──
    def gather_pending(self, pk):
        try:
            proposal = ChangeProposal.objects.filter(pk=pk, status=ChangeProposal.PENDING).first()
            if proposal is None:
                return
            if proposal.kind == ChangeProposal.NEW:
                return self.approve_new(proposal)
            if not Store.all_objects.filter(pk=proposal.store_id).exists():
                self.log(f"제안 #{proposal.pk}: 업체가 없어 그대로 둠")
                return
            if (proposal.evidence or {}).get("business_status") == "CLOSED_PERMANENTLY":
                self.log(f"제안 #{proposal.pk} {proposal.title}: 구글에서 폐업으로 표시되어 그대로 둠")
                return
            # The proposal already holds Google's data: Claude rewrites from it, no new Google calls.
            StoreBatchItem.objects.create(job=self.job, store_id=proposal.pk, candidates=[])
        except google_places.PlacesError as e:
            self.count("errors")
            self.log(f"제안 #{pk}: 오류 - {e}")
        except Exception as e:
            self.count("errors")
            self.log(f"제안 #{pk}: 오류 - {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            connection.close()

    def approve_new(self, proposal, count=True):
        """A discovered store is added when Google showed it open and every
        piece the page needs is there."""
        ch, ev = proposal.changes or {}, proposal.evidence or {}
        missing = [label for key, label in (("title", "업체명"), ("address", "주소"), ("category", "카테고리"),
                                            ("category2", "가나다 카테고리"), ("review", "상세소개"))
                   if not ch.get(key)]
        if ev.get("business_status", "OPERATIONAL") != "OPERATIONAL":
            self.log(f"신규 #{proposal.pk} {proposal.title}: 구글에서 영업 중이 아님 - 그대로 둠")
            return
        if missing:
            self.log(f"신규 #{proposal.pk} {proposal.title}: {', '.join(missing)} 없음 - 직접 확인해 주세요")
            return
        with self.new_lock:  # one store per Google place, even with parallel threads
            if ev.get("place_id") and StorePlace.objects.filter(place_id=ev["place_id"]).exists():
                services.reject_proposal(proposal)
                self.log(f"신규 #{proposal.pk} {proposal.title}: 이미 등록된 장소 - 중복이라 거절")
                return
            services.apply_proposal(proposal, recount=False)
        if count:
            self.count("proposals")

    def finish_pending(self, item, written):
        """A Google suggestion rewritten by Jihun's rule, then applied."""
        proposal = ChangeProposal.objects.filter(pk=item.store_id, status=ChangeProposal.PENDING).first()
        store = Store.all_objects.filter(pk=proposal.store_id).first() if proposal else None
        if store is None:
            return
        if not (written.get("title") and written.get("description_html")):
            self.log(f"제안 #{proposal.pk} {proposal.title}: 새 이름·설명을 만들지 못해 그대로 둠 - 직접 확인해 주세요")
            return
        values = dict(proposal.changes or {})
        for key, field in (("title", "title"), ("review", "description_html")):
            if written.get(field):
                values[key] = written[field]
        apply_business_change(values, written)
        changes = {k: v for k, v in values.items() if (store.raw.get(k) or "") != v}
        old = store.raw.get("title", "")
        proposal.changes = changes
        proposal.title = values.get("title") or proposal.title
        proposal.evidence = {**(proposal.evidence or {}), "rewritten": {
            "name_changed": written.get("name_changed"), "category_changed": written.get("category_changed"),
            "reason": written.get("match_reason"), "job": self.job.pk}}
        proposal.save()
        services.apply_proposal(proposal, recount=False)
        self.count("proposals")
        kind = "업종 변경" if written.get("category_changed") else "상호 변경" if written.get("name_changed") else "같은 업체"
        self.log(f"#{store.pk} {old} → {proposal.title} ({kind})")

    # ── names: "English 한글" for every shown store ──
    def name_writer(self):
        if getattr(self, "_name_writer", None) is None:
            self._name_writer = store_writer.StoreWriter(usage=self.usage, system_prompt=store_writer.NAME_PROMPT,
                                                         schema=store_writer.NAME_SCHEMA, effort="low")
        return self._name_writer

    # ── photos: Google Maps photos for the store pages ──
    def photo_targets(self):
        """Shown stores linked to a Google place that still have room for photos
        (not franchise stores: they show their brand's logo)."""
        job = self.job
        places = StorePlace.objects.exclude(place_id="")
        checked = set(places.filter(photos_checked_at__isnull=False).values_list("store_id", flat=True))
        qs = (Store.objects.filter(id__in=places.values("store_id")).exclude(category3_path__startswith=FRANCHISE_ROOT)
              .order_by("id"))
        if job.scope == StoreUpdateJob.STORES:
            qs = qs.filter(pk__in=[int(x) for x in re.findall(r"\d+", job.store_ids)])
        elif job.scope == StoreUpdateJob.SUBURB and job.suburb_id:
            qs = qs.filter(suburb_id=job.suburb_id)
        elif job.scope == StoreUpdateJob.STATE and job.state_id:
            qs = qs.filter(state_id=job.state_id)
        ids = []
        for s in qs.only("id", "raw"):
            count = len(store_photos(s.raw))
            if job.skip_checked and s.pk in checked and count:
                continue  # checked before and has photos (checked stores still without any are tried again)
            if job.replace_photos or count < PHOTOS_PER_STORE:
                ids.append(s.pk)
        if job.scope == StoreUpdateJob.SAMPLE:
            ids = ids[: job.limit or 20]
        return ids[: job.limit] if job.limit else ids

    @property
    def photo_writer(self):
        if getattr(self, "_photo_writer", None) is None:
            self._photo_writer = store_writer.StoreWriter(
                usage=self.usage, model=store_writer.PHOTO_MODEL, system_prompt=store_writer.PHOTO_PROMPT,
                schema=store_writer.PHOTO_SCHEMA)
        return self._photo_writer

    def logo_only(self, store):
        """A franchise store shows only its brand's logo (no Google photos)."""
        from . import franchise

        brand = Franchise.objects.filter(category__legacy_path=store.raw.get("category3")).first()
        if brand is None:
            return
        logo = franchise.ensure_logo(brand)
        if not logo:
            return
        raw = dict(store.raw)
        wanted = [logo] + [""] * (services.PHOTO_SLOTS - 1)
        if [raw.get(f"img_{i}", "") for i in range(services.PHOTO_SLOTS)] == wanted:
            return
        for i in range(services.PHOTO_SLOTS):
            raw[f"img_{i}"] = wanted[i]
        services.save_store(store, raw, source=StoreRevision.TOOL, recount=False,
                            note=f"자동 업데이트 #{self.job.pk}: 프랜차이즈 매장 사진을 브랜드 로고로")
        self.count("proposals")

    def photo_store(self, store):
        """Download the place's photos, let Claude pick the ones to show,
        and save them as WebP in the store's empty photo slots."""
        if (store.raw.get("category3") or "").startswith(FRANCHISE_ROOT):
            return  # franchise stores keep their brand's logo (also skips them in runs started earlier)
        place = StorePlace.objects.filter(store_id=store.pk).exclude(place_id="").first()
        if place is None:
            return
        # Old photos whose file is gone would stay in front (the card shows "no image").
        kept = [] if self.job.replace_photos else [p for p in store_photos(store.raw) if photo_exists(p)]
        need = PHOTOS_PER_STORE - len(kept)
        listed = self.places.photo_list(place.place_id)[:PHOTOS_TO_CHECK] if need > 0 else []
        photos = []
        for info in listed:
            try:
                image = Image.open(io.BytesIO(self.places.photo(info["name"], max_px=1200)), formats=SAFE_FORMATS)
                image.load()
                photos.append((info, image))
            except google_places.PlacesError:
                raise
            except Exception as e:  # one unreadable photo must not stop the store
                self.log(f"#{store.pk}: 사진 하나를 읽지 못함 - {e}")
        chosen = []
        if photos:
            self.count("matched")
            answer = self.photo_writer.write(
                {"store": {"title": store.raw.get("title", ""), "category": store.raw.get("category", ""),
                           "category2": store.raw.get("category2", ""), "intro": store.raw.get("short_comment", "")},
                 "photo_count": len(photos), "need": need},
                images=[preview(image) for _, image in photos])
            usable = {p["index"] for p in answer.get("photos", []) if p.get("usable")}
            kinds = {p["index"]: p.get("kind") for p in answer.get("photos", [])}
            for i in answer.get("chosen", []):
                if 0 <= i < len(photos) and i not in chosen and (i in usable or kinds.get(i) not in ("people", "other")):
                    chosen.append(i)
            if not chosen and not kept:
                # Every store page should show a photo: take the best one
                # that is not mainly people or a screenshot/document.
                chosen = [i for i in range(len(photos)) if kinds.get(i) not in ("people", "other")][:1]
            chosen = chosen[:need]
            chosen = menu_second(chosen, kinds, usable, kept, need, store.raw)
        if chosen:
            names = [services.save_photo(ContentFile(webp(photos[i][1]), name="google.webp")) for i in chosen]
            raw = dict(store.raw)
            slots = kept + names
            for i in range(services.PHOTO_SLOTS):
                raw[f"img_{i}"] = slots[i] if i < len(slots) else ""
            services.save_store(store, raw, source=StoreRevision.TOOL, recount=False,
                                note=f"자동 업데이트 #{self.job.pk}: 구글 지도 사진 {len(names)}장")
            self.count("proposals")
            self.log(f"#{store.pk} {store.raw.get('title', '')}: 사진 {len(names)}장 추가 (구글 사진 {len(photos)}장 중)")
        else:
            self.log(f"#{store.pk} {store.raw.get('title', '')}: 쓸 만한 구글 사진 없음")
        StorePlace.objects.filter(pk=place.pk).update(photos_checked_at=timezone.now())

    def gather_names(self, pk):
        try:
            StoreBatchItem.objects.create(job=self.job, store_id=pk, candidates=[])
        finally:
            connection.close()

    def name_facts(self, store):
        raw = store.raw
        latest = (ChangeProposal.objects.filter(store_id=store.pk, status__in=[ChangeProposal.APPROVED,
                                                                                ChangeProposal.AUTO_APPLIED])
                  .order_by("-id").first())
        return {"title": raw.get("title", ""), "category": (raw.get("category") or "").split("/")[-1],
                "address": raw.get("address", ""),
                "google_name": ((latest.evidence or {}).get("google_name", "") if latest else "")}

    def finish_name(self, item, written):
        store = Store.objects.filter(pk=item.store_id).first()
        new = re.sub(r"\s+", " ", (written.get("title") or "")).strip()
        if store is None or not new or new == store.raw.get("title") or not has_both_scripts(new):
            return
        raw = dict(store.raw)
        old, raw["title"] = raw.get("title", ""), new
        services.save_store(store, raw, source=StoreRevision.TOOL, note=f"업체명 표기 #{self.job.pk}", recount=False)
        self.count("proposals")
        self.log(f"#{store.pk} {old} → {new}")

    # ── clean-up: hide stores Google cannot find ──
    def cleanup_targets(self):
        """Shown stores that no run could find on Google (by name or phone),
        or whose only proposals the admin rejected. Stores with a proposal
        still waiting for review are left alone."""
        pending = ChangeProposal.objects.filter(status=ChangeProposal.PENDING).values("store_id")
        accepted = ChangeProposal.objects.filter(
            status__in=[ChangeProposal.APPROVED, ChangeProposal.AUTO_APPLIED]).values("store_id")
        rejected = ChangeProposal.objects.filter(status=ChangeProposal.REJECTED).values("store_id")
        not_found = StorePlace.objects.filter(place_id="").values("store_id")
        qs = (Store.objects.filter(models.Q(id__in=not_found) | (models.Q(id__in=rejected) & ~models.Q(id__in=accepted)))
              .exclude(id__in=pending))
        return list(qs.order_by("id").values_list("id", flat=True))

    def run_cleanup(self):
        job = self.job
        work = self.usage["work"]
        while job.cursor < len(work):
            job.refresh_from_db(fields=["cancel_requested"])
            if job.cancel_requested:
                return self.finalize(cancelled=True)
            for pk in work[job.cursor:job.cursor + 100]:
                store = Store.objects.filter(pk=pk).first()
                if store is not None:
                    services.save_store(store, dict(store.raw), closed=True, source=StoreRevision.TOOL,
                                        note=f"{cleanup_note(job.pk)} 구글 지도에서 찾지 못함", recount=False)
                    self.count("proposals")
            job.cursor = min(len(work), job.cursor + 100)
            job.processed = job.cursor
            job.heartbeat = timezone.now()
            job.save()
        unchecked = Store.objects.exclude(id__in=StorePlace.objects.values("store_id")).count()
        self.log(f"숨긴 업체 {job.proposals}개. 아직 한 번도 확인하지 않은 업체 {unchecked}개는 그대로 두었습니다. "
                 f"이 작업 화면의 '숨기기 되돌리기'로 한 번에 되돌릴 수 있습니다.")
        return self.finalize()

    # ── photos whose file is missing ──
    def run_photofix(self):
        """Drop photo entries whose file no longer exists (old uploads) and move
        the real photos up, so the first one is the card photo."""
        job = self.job
        work = self.usage["work"]
        while job.cursor < len(work):
            job.refresh_from_db(fields=["cancel_requested"])
            if job.cancel_requested:
                return self.finalize(cancelled=True)
            store = Store.objects.filter(pk=work[job.cursor]).first()
            if store:
                photos = store_photos(store.raw)
                real = [p for p in photos if photo_exists(p)]
                slots = [store.raw.get(f"img_{i}") or "" for i in range(services.PHOTO_SLOTS)]
                wanted = real + [""] * (services.PHOTO_SLOTS - len(real))
                # Also an empty first slot with photos after it: the card shows the first slot.
                if slots != wanted:
                    raw = dict(store.raw)
                    for i in range(services.PHOTO_SLOTS):
                        raw[f"img_{i}"] = wanted[i]
                    services.save_store(store, raw, source=StoreRevision.TOOL, recount=False,
                                        note=f"{cleanup_note(job.pk)} 사진 정리 (없는 사진 {len(photos) - len(real)}장 뺌, 앞으로 당김)")
                    self.count("proposals")
            job.cursor += 1
            job.processed = job.cursor
            if job.cursor % 50 == 0:
                job.heartbeat = timezone.now()
                job.save()
        self.log(f"사진을 정리한 업체 {job.proposals}곳 (파일이 없는 사진을 빼고 남은 사진을 앞으로).")
        return self.finalize()

    # ── merging duplicate listings ──
    def merge_targets(self):
        """["keeper,dup,dup", ...]: listings of the same store. The old CMS
        often had one listing per category for the same shop."""
        groups, skipped = duplicate_groups(Store.objects.all())
        for line in skipped:
            self.log(line)
        return [",".join(str(pk) for pk in group) for group in groups]

    def run_merge(self):
        job = self.job
        work = self.usage["work"]
        moved = self.usage.setdefault("moved_reviews", {})
        while job.cursor < len(work):
            job.refresh_from_db(fields=["cancel_requested"])
            if job.cancel_requested:
                return self.finalize(cancelled=True)
            ids = [int(x) for x in work[job.cursor].split(",")]
            stores = [s for s in (Store.objects.filter(pk=pk).first() for pk in ids) if s]
            if len(stores) > 1:
                keeper, dups = stores[0], stores[1:]
                raw, dropped = merged_raw(keeper, dups)
                note = f"{cleanup_note(job.pk)} 중복 업체 합치기"
                services.save_store(keeper, raw, source=StoreRevision.TOOL, recount=False,
                                    note=f"{note} (#{', #'.join(str(d.pk) for d in dups)} 합침)")
                for dup in dups:
                    for rid in Review.objects.filter(store_id=dup.pk).values_list("id", flat=True):
                        moved[str(rid)] = dup.pk
                    Review.objects.filter(store_id=dup.pk).update(store_id=keeper.pk)
                    services.save_store(dup, {**dup.raw, "merged_into": str(keeper.pk)}, closed=True,
                                        source=StoreRevision.TOOL, recount=False, note=f"{note} (#{keeper.pk}에 합침)")
                self.count("proposals")
                self.log(f"#{keeper.pk} {keeper.raw.get('title', '')} ← "
                         + ", ".join(f"#{d.pk} {d.raw.get('title', '')}" for d in dups)
                         + (f" · 들어갈 자리가 없어 뺀 카테고리: {', '.join(dropped)}" if dropped else ""))
            job.cursor += 1
            job.processed = job.cursor
            if job.cursor % 20 == 0:
                job.usage = self.usage
                job.heartbeat = timezone.now()
                job.save()
        self.log(f"합친 업체 {job.proposals}곳. 이 작업 화면의 '되돌리기'로 한 번에 되돌릴 수 있습니다.")
        return self.finalize()

    def run_restore(self):
        """Undo every hide (or merge) made by one clean-up or merge job."""
        job = self.job
        source = int(re.sub(r"\D", "", job.store_ids) or 0)
        moved = (StoreUpdateJob.objects.filter(pk=source).values_list("usage", flat=True).first() or {}).get(
            "moved_reviews") or {}
        for rid, store_id in moved.items():
            Review.objects.filter(pk=int(rid)).update(store_id=store_id)
        revisions = list(StoreRevision.objects.filter(
            source=StoreRevision.TOOL, note__startswith=cleanup_note(source), undone=False).order_by("id"))
        job.total = len(revisions)
        for rev in revisions:
            job.refresh_from_db(fields=["cancel_requested"])
            if job.cancel_requested:
                return self.finalize(cancelled=True)
            services.undo(rev, note=f"정리 작업 #{source} 되돌리기", recount=False)
            job.processed += 1
            if job.processed % 100 == 0:
                job.heartbeat = timezone.now()
                job.save()
        self.log(f"정리 작업 #{source}에서 숨긴 업체 {job.processed}개를 다시 표시했습니다.")
        return self.finalize()

    # ── the work list ──
    def work_list(self):
        job = self.job
        if job.mode == StoreUpdateJob.DISCOVER and job.target:
            return self.sweep_work()
        if job.mode == StoreUpdateJob.DISCOVER:
            areas = AREAS
            if job.scope == StoreUpdateJob.SUBURB:
                if not (job.suburb_id and job.suburb.lat and job.suburb.lng):
                    # Never fall back to the whole country by accident.
                    raise ValueError("선택한 지역에 위치 정보(위도·경도)가 없어 검색할 수 없습니다.")
                areas = [(job.suburb.name, float(job.suburb.lat), float(job.suburb.lng), 4000, True)]
                self.usage["areas"] = areas  # kept with the job, for resuming
            return [f"{ai}|{qi}" for ai, area in enumerate(areas)
                    for qi in range(len(DISCOVER_QUERIES) if area[4] else SHORT_QUERIES)]
        if job.mode == StoreUpdateJob.CLEANUP:
            return self.cleanup_targets()
        if job.mode == StoreUpdateJob.MERGE:
            return self.merge_targets()
        if job.mode == StoreUpdateJob.PHOTOFIX:
            return [s.pk for s in Store.objects.order_by("id").only("id", "raw") if store_photos(s.raw)]
        if job.mode == StoreUpdateJob.PENDING:
            # New stores first (so a rewritten suggestion never duplicates one), then Google suggestions.
            pending = ChangeProposal.objects.filter(status=ChangeProposal.PENDING).exclude(kind=ChangeProposal.CLOSE)
            return (list(pending.filter(kind=ChangeProposal.NEW).order_by("id").values_list("id", flat=True))
                    + list(pending.filter(kind=ChangeProposal.UPDATE).order_by("id").values_list("id", flat=True)))
        if job.mode == StoreUpdateJob.NAMES:
            qs = Store.objects.order_by("id").only("id", "raw")
            if job.scope == StoreUpdateJob.STORES:
                qs = qs.filter(pk__in=[int(x) for x in re.findall(r"\d+", job.store_ids)])
            ids = [s.pk for s in qs if not has_both_scripts(s.raw.get("title"))]
            return ids[: job.limit] if job.limit else ids
        if job.mode == StoreUpdateJob.RESTORE:
            return []
        if job.mode == StoreUpdateJob.PHOTOS:
            return self.photo_targets()
        qs = Store.objects.all()
        if job.mode == StoreUpdateJob.LOGOS:
            return list(Store.all_objects.filter(category3_path__startswith=FRANCHISE_ROOT).order_by("id")
                        .values_list("id", flat=True))
        if job.mode == StoreUpdateJob.RECATEGORIZE:
            from . import recategorize

            return recategorize.work_list()
        if job.mode == StoreUpdateJob.CHECK:
            # Every shown store matched to a Google place (the others are not on Google).
            qs = qs.filter(id__in=StorePlace.objects.exclude(place_id="").values("store_id"))
            if job.scope == StoreUpdateJob.SUBURB and job.suburb_id:
                qs = qs.filter(suburb_id=job.suburb_id)
            elif job.scope == StoreUpdateJob.STATE and job.state_id:
                qs = qs.filter(state_id=job.state_id)
            elif job.scope == StoreUpdateJob.STORES:
                qs = qs.filter(pk__in=[int(x) for x in re.findall(r"\d+", job.store_ids)])
            ids = list(qs.order_by("id").values_list("id", flat=True))
            if job.scope == StoreUpdateJob.SAMPLE:
                ids = ids[: job.limit or 20]
            return ids[: job.limit] if job.limit else ids
        if job.scope == StoreUpdateJob.STORES:
            ids = [int(x) for x in re.findall(r"\d+", job.store_ids)]
            return [pk for pk in ids if Store.objects.filter(pk=pk).exists()]
        if job.scope == StoreUpdateJob.SUBURB and job.suburb_id:
            qs = qs.filter(suburb_id=job.suburb_id)
        elif job.scope == StoreUpdateJob.STATE and job.state_id:
            qs = qs.filter(state_id=job.state_id)
        if job.scope == StoreUpdateJob.SAMPLE:
            return sample_ids(job.limit or 20)
        if job.mode == StoreUpdateJob.REFRESH:
            # Every shown store already matched to a Google place.
            qs = qs.filter(id__in=StorePlace.objects.exclude(place_id="").values("store_id"))
        elif job.recheck_not_found:
            # The retry pass: only stores an earlier run could not find.
            qs = qs.filter(id__in=StorePlace.objects.filter(place_id="").values("store_id"))
        elif job.skip_checked:
            qs = qs.exclude(id__in=StorePlace.objects.values("store_id"))
        ids = list(qs.order_by("id").values_list("id", flat=True))
        return ids[: job.limit] if job.limit else ids

    def pause(self):
        """Claude's daily limit: wait (queued) and try again later."""
        job = self.job
        job.status = StoreUpdateJob.QUEUED
        job.resume_after = timezone.now() + LIMIT_RETRY
        job.usage = self.usage
        held = len(self.usage.get("retry") or [])
        self.log(f"Claude 일일 사용 한도에 도달해 잠시 멈춥니다 (대기 업체 {held}개). "
                 f"{timezone.localtime(job.resume_after):%H:%M}에 다시 시도하고, 한도가 풀리면 자동으로 이어갑니다.")
        job.save()

    def pause_budget(self, error):
        """The monthly Claude budget is used up: wait, checking again every hour,
        until next month or until the budget is raised."""
        job = self.job
        job.status = StoreUpdateJob.QUEUED
        job.resume_after = timezone.now() + LIMIT_RETRY
        if not self.usage.get("budget_paused"):
            self.log(f"{error} - 월 예산에 도달해 멈춥니다. 다음 달 1일이 되거나 예산을 올리면 자동으로 이어갑니다.")
        self.usage["budget_paused"] = True
        job.usage = self.usage
        job.save()

    def process(self, item):
        """One store, on demand, in a worker thread."""
        try:
            store = Store.objects.filter(pk=item).first()
            if store and self.job.mode == StoreUpdateJob.PHOTOS:
                self.photo_store(store)
            elif store and self.job.mode == StoreUpdateJob.CHECK:
                self.check_store(store)
            elif store and self.job.mode == StoreUpdateJob.LOGOS:
                self.logo_only(store)
            elif store and self.job.mode == StoreUpdateJob.RECATEGORIZE:
                from . import recategorize

                recategorize.one(self, store)
            elif store:
                self.verify_store(store)
        except store_writer.DailyLimitError:
            # Not the store's fault: keep it for when the limit resets.
            with self.lock:
                self.daily_limited = True
                self.usage.setdefault("retry", []).append(item)
        except store_writer.BudgetError as e:
            with self.lock:
                self.budget_error = e
                self.usage.setdefault("retry", []).append(item)
        except (google_places.PlacesError, store_writer.WriterError) as e:
            self.count("errors")
            self.log(f"{item}: 오류 - {e}")
        except Exception as e:  # keep going; one bad record must not stop the run
            self.count("errors")
            self.log(f"{item}: 오류 - {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            connection.close()  # each thread has its own database connection

    # ── batch runs (Bedrock batch inference) ──
    def wait(self, minutes, line=None):
        """Leave the job queued and come back later (the worker is free meanwhile)."""
        job = self.job
        job.status = StoreUpdateJob.QUEUED
        job.resume_after = timezone.now() + datetime.timedelta(minutes=minutes)
        if line:
            self.log(line)
        job.usage = self.usage
        job.save()

    def gather(self, item):
        """Batch step 1 for one store: Google now, Claude later."""
        try:
            store = Store.objects.filter(pk=item).first()
            if store:
                candidates = self.prepare(store)
                if candidates:
                    StoreBatchItem.objects.create(job=self.job, store_id=store.pk,
                                                  candidates=[[p, sc, pa] for p, sc, pa in candidates])
        except google_places.PlacesError as e:
            self.count("errors")
            self.log(f"{item}: 오류 - {e}")
        except Exception as e:
            self.count("errors")
            self.log(f"{item}: 오류 - {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            connection.close()

    def request_for(self, item):
        """(writer, facts) for one waiting item — the same request either way."""
        if self.job.mode == StoreUpdateJob.DISCOVER:
            facts = facts_for({}, [item.candidates[0][0]], mode="new")
            reason = (item.candidates[0][2] or {}).get("reason") if len(item.candidates[0]) > 2 else ""
            if reason in DISCOVERY_REASONS:
                facts["discovery_reason"] = DISCOVERY_REASONS[reason]
            return self.category_writer(), facts
        if self.job.mode == StoreUpdateJob.PENDING:
            proposal = ChangeProposal.objects.filter(pk=item.store_id).first()
            store = Store.all_objects.filter(pk=proposal.store_id).first() if proposal else None
            if store is None:
                return None, None
            return self.category_writer(), pending_facts(proposal, store)
        if self.job.mode == StoreUpdateJob.NAMES:
            store = Store.objects.filter(pk=item.store_id).first()
            return (self.name_writer(), self.name_facts(store)) if store else (None, None)
        store = Store.all_objects.filter(pk=item.store_id).first()
        if store is None:
            return None, None
        return self.writer, facts_for(store.raw, [c[0] for c in item.candidates], mode="verify")

    def apply_answer(self, item, written):
        if self.job.mode == StoreUpdateJob.DISCOVER:
            return self.finish_discover(item, written)
        if self.job.mode == StoreUpdateJob.PENDING:
            return self.finish_pending(item, written)
        if self.job.mode == StoreUpdateJob.NAMES:
            return self.finish_name(item, written)
        store = Store.objects.filter(pk=item.store_id).first()
        if store:
            self.finish(store, [tuple(c) for c in item.candidates], written)

    def run_batch(self):
        job, usage = self.job, self.usage
        phase = usage.get("phase", "gather")
        job.refresh_from_db(fields=["cancel_requested"])
        if job.cancel_requested:
            if usage.get("batch_arn") and phase == "waiting":
                store_batch.stop(usage["batch_arn"])
            return self.finalize(cancelled=True)
        job.resume_after = None

        if phase == "gather":
            work = usage["work"]
            size = max(1, settings.STORE_UPDATE_THREADS)
            with ThreadPoolExecutor(max_workers=size) as pool:
                while job.cursor < len(work):
                    job.refresh_from_db(fields=["cancel_requested"])
                    if job.cancel_requested:
                        return self.finalize(cancelled=True)
                    chunk = work[job.cursor:job.cursor + size]
                    gather = {StoreUpdateJob.DISCOVER: self.gather_discover, StoreUpdateJob.PENDING: self.gather_pending,
                              StoreUpdateJob.NAMES: self.gather_names}.get(job.mode, self.gather)
                    list(pool.map(gather, chunk))
                    with self.lock:
                        job.cursor += len(chunk)
                        job.processed = job.cursor
                        job.usage = usage
                        job.heartbeat = timezone.now()
                        job.save()
            usage["phase"] = phase = "submit"
            n = job.batch_items.filter(done=False).count()
            if job.mode == StoreUpdateJob.DISCOVER:
                skipped = (usage.get("discover") or {}).get("no_korean_sign", 0)
                self.log(f"1단계 완료: 검색 {job.processed}개, 처음 보는 한인 업체 후보 {n}개 "
                         f"(한국 관련 표시가 없는 장소 {skipped}개는 제외)")
            else:
                self.log(f"1단계 완료: 구글 조회 {job.processed}개, Claude가 판단할 업체 {n}개")

        if phase == "submit" and job.target == StoreUpdateJob.CHINESE and not usage.get("kept"):
            keep = job.limit or 100
            ranked = sorted(job.batch_items.filter(done=False), key=lambda i: -popularity(i.candidates[0][0]))
            job.batch_items.filter(pk__in=[i.pk for i in ranked[keep:]]).delete()
            usage["kept"] = min(keep, len(ranked))
            self.log(f"평점·리뷰가 좋은 중식당 {len(ranked)}곳 중 인기 순으로 {usage['kept']}곳을 등록합니다.")
        if phase == "submit":
            items = list(job.batch_items.filter(done=False).order_by("id"))
            if len(items) < store_batch.MIN_RECORDS:
                # Too few for a batch job: ask Claude directly (same request).
                if items:
                    self.log(f"{len(items)}개는 배치 최소 수({store_batch.MIN_RECORDS}개)보다 적어 바로 처리합니다.")
                for item in items:
                    job.refresh_from_db(fields=["cancel_requested"])
                    if job.cancel_requested:
                        return self.finalize(cancelled=True)
                    writer, facts = self.request_for(item)
                    try:
                        if writer:
                            self.apply_answer(item, writer.write(facts))
                    except store_writer.DailyLimitError:
                        return self.wait(60, "Claude 일일 사용 한도 - 1시간 뒤 이어서 처리합니다.")
                    except store_writer.BudgetError as e:
                        return self.pause_budget(e)
                    except store_writer.WriterError as e:
                        self.count("errors")
                        self.log(f"#{item.store_id}: 오류 - {e}")
                    item.done = True
                    item.save(update_fields=["done"])
                    job.heartbeat = timezone.now()
                    job.save()
                return self.finalize()
            records = []
            for item in items:
                writer, facts = self.request_for(item)
                if writer:
                    records.append(writer.batch_record(item.pk, facts))
            try:
                store_writer.check_budget(usage, upcoming=batch_estimate(job.mode, len(records)))
            except store_writer.BudgetError as e:
                return self.pause_budget(e)
            usage["batch_arn"] = store_batch.submit(job.pk, records)
            usage["batch_records"] = len(records)
            usage["phase"] = "waiting"
            usage["batch_status"] = "Submitted"
            return self.wait(10, f"2단계: Claude 요청 {len(records)}개를 AWS 배치로 제출했습니다. "
                                 f"처리가 끝나면(보통 몇 시간, 최대 24시간) 자동으로 결과를 반영합니다.")

        if phase == "waiting":
            status, message = store_batch.status(usage["batch_arn"])
            if status != usage.get("batch_status"):
                self.log(f"AWS 배치 상태: {store_batch.STATUS_KO.get(status, status)}{(' - ' + message) if message else ''}")
            usage["batch_status"] = status
            if status in store_batch.RUNNING:
                return self.wait(10)
            if status not in store_batch.FINISHED:
                self.log("AWS 배치 작업이 끝나지 못했습니다. 이 업체들은 확인되지 않은 상태로 남아 다음 작업에서 다시 처리됩니다.")
                job.status = StoreUpdateJob.FAILED
                return self.finalize(status=StoreUpdateJob.FAILED)
            usage["phase"] = phase = "collect"

        if phase == "collect":
            items = {str(i.pk): i for i in job.batch_items.filter(done=False)}
            for record in store_batch.results(job.pk):
                item = items.pop(str(record.get("recordId")), None)
                if item is None:
                    continue
                try:
                    if record.get("error"):
                        raise store_writer.WriterError(str(record["error"])[:200])
                    written = store_writer.read_answer(record.get("modelOutput") or {}, usage, prefix="batch_tokens")
                    self.apply_answer(item, written)
                except Exception as e:
                    self.count("errors")
                    self.log(f"#{item.store_id}: 오류 - {e}")
                item.done = True
                item.save(update_fields=["done"])
            if items:
                self.log(f"결과가 없는 업체 {len(items)}개 - 확인되지 않은 상태로 남아 다음 작업에서 다시 처리됩니다.")
            job.batch_items.all().delete()
            usage["batch_usd"] = store_writer.cost_of({k: v for k, v in usage.items() if k.startswith("batch_tokens_")})
            return self.finalize()

    def finalize(self, *, cancelled=False, status=None):
        job = self.job
        job.status = status or (StoreUpdateJob.CANCELLED if cancelled else StoreUpdateJob.DONE)
        if cancelled:
            self.log("관리자가 작업을 중지했습니다.")
        services.recount_categories()  # skipped per store during the run
        job.finished_at = timezone.now()
        job.resume_after = None
        self.usage["cost_usd"] = {"google": google_places.cost_of(self.usage),
                                  "claude": store_writer.cost_of(self.usage)}
        job.usage = self.usage
        self.log(f"작업 종료: 처리 {job.processed}, 구글 일치 {job.matched}, 제안 {job.proposals}, 오류 {job.errors}"
                 f" · 예상 비용 Google ${self.usage['cost_usd']['google']} / Claude ${self.usage['cost_usd']['claude']}")
        if job.mode == StoreUpdateJob.DISCOVER and job.then_photos and job.status == StoreUpdateJob.DONE:
            ids = added_by(job)
            if ids:
                photos = StoreUpdateJob.objects.create(
                    mode=StoreUpdateJob.PHOTOS, scope=StoreUpdateJob.STORES, store_ids=",".join(map(str, ids)),
                    limit=0, use_batch=False, skip_checked=True, auto_apply=True, usage={"from_job": job.pk},
                    log=f"{timezone.localtime():%H:%M:%S} 신규 업체 찾기 #{job.pk}에서 등록한 업체 {len(ids)}곳의 사진\n")
                self.log(f"새로 등록한 업체 {len(ids)}곳의 사진은 작업 #{photos.pk}에서 가져옵니다.")
        job.save()

    def run(self):
        job = self.job
        if not self.usage.get("work"):
            job.usage = self.usage
            self.usage["work"] = self.work_list()
            job.total = len(self.usage["work"])
            self.log(f"작업 시작: {job.total}개 대상")
            job.save()
        if job.mode == StoreUpdateJob.CLEANUP:
            return self.run_cleanup()
        if job.mode == StoreUpdateJob.MERGE:
            return self.run_merge()
        if job.mode == StoreUpdateJob.PHOTOFIX:
            return self.run_photofix()
        if job.mode == StoreUpdateJob.RESTORE:
            return self.run_restore()
        # Nothing (not even Google) is spent while this month's Claude budget is used up.
        # The monthly check does not use Claude, so the budget does not hold it back.
        if job.mode not in (StoreUpdateJob.CHECK, StoreUpdateJob.LOGOS) and (
                self.usage.get("phase", "gather") == "gather" or self.usage.get("budget_paused")):
            try:
                store_writer.check_budget(self.usage)
            except store_writer.BudgetError as e:
                return self.pause_budget(e)
        if self.usage.pop("budget_paused", None):
            self.log("Claude 월 예산에 여유가 생겨 작업을 이어갑니다.")
        if job.mode in (StoreUpdateJob.DISCOVER, StoreUpdateJob.PENDING, StoreUpdateJob.NAMES) or (
                job.use_batch and job.mode in (StoreUpdateJob.VERIFY, StoreUpdateJob.REFRESH)):
            return self.run_batch()
        work = self.usage["work"]
        batch = max(1, settings.STORE_UPDATE_THREADS)
        if job.mode == StoreUpdateJob.PHOTOS:
            # Image work runs on the web server's small CPU: two stores at a time.
            batch = min(batch, PHOTO_THREADS)
        cancelled = False
        if self.usage.get("retry") or job.resume_after:
            if not store_writer.probe():  # still limited: wait again without spending on Google
                return self.pause()
            self.log("Claude 사용 한도가 풀려 작업을 이어갑니다.")
        job.resume_after = None
        with ThreadPoolExecutor(max_workers=batch) as pool:
            while job.cursor < len(work) or self.usage.get("retry"):
                job.refresh_from_db(fields=["cancel_requested"])
                if job.cancel_requested:
                    cancelled = True
                    break
                retry = self.usage.get("retry") or []
                if retry:  # stores held back by the daily limit go first
                    chunk, self.usage["retry"] = retry[:batch], retry[batch:]
                    advance = 0
                else:
                    chunk = work[job.cursor:job.cursor + batch]
                    advance = len(chunk)
                list(pool.map(self.process, chunk))  # the cursor only moves past finished stores
                with self.lock:
                    job.cursor += advance
                    job.processed = job.cursor - len(self.usage.get("retry") or [])
                    job.usage = self.usage
                    job.heartbeat = timezone.now()
                    job.save()
                if self.budget_error:
                    return self.pause_budget(self.budget_error)
                if self.daily_limited:
                    return self.pause()
        self.finalize(cancelled=cancelled)


def popularity(place):
    """Rating weighted by how many people rated (4.6 from 2,000 beats 4.9 from 100)."""
    return (place.get("rating") or 0) * math.log10(max(10, place.get("userRatingCount") or 0))


def added_by(job):
    """Stores a discovery run added (shown, linked to a Google place), by number."""
    ids = ChangeProposal.objects.filter(kind=ChangeProposal.NEW, status=ChangeProposal.APPROVED, evidence__job=job.pk,
                                        store_id__isnull=False).values_list("store_id", flat=True)
    return list(Store.objects.filter(pk__in=ids, id__in=StorePlace.objects.exclude(place_id="").values("store_id"))
                .order_by("id").values_list("id", flat=True))


# Claude cost per batch record when no earlier batch of the same kind
# has finished yet (Opus 4.6 at batch prices).
BATCH_COST_PER_RECORD = {StoreUpdateJob.NAMES: 0.003}


def batch_estimate(mode, n):
    """Expected Claude cost of a batch of n records, from finished batches of the same kind."""
    done = StoreUpdateJob.objects.filter(mode=mode, usage__has_key="batch_usd").values_list("usage", flat=True)
    records = sum(u.get("batch_records", 0) for u in done)
    usd = sum(u.get("batch_usd", 0) for u in done)
    per = usd / records if records else BATCH_COST_PER_RECORD.get(mode, 0.03)
    return per * n


FRANCHISE_ROOT = "테마검색/프랜차이즈"
PHOTOS_PER_STORE = 5   # photos a store page shows at most from this tool
PHOTOS_TO_CHECK = 8    # Google photos looked at per store (each download is billed)
PHOTO_THREADS = 2      # stores processed at once by the photo run


def is_food(raw):
    cats = f"{raw.get('category') or ''} {raw.get('category2') or ''}"
    return "맛집" in cats or "음식" in cats or "카페" in cats or "제과" in cats


def menu_second(chosen, kinds, usable, kept, need, raw):
    """Restaurants and cafés: a usable menu photo, when Google has one, is
    always shown and comes second (after the card photo)."""
    menus = [i for i in sorted(kinds) if kinds.get(i) == "menu" and i in usable]
    if not is_food(raw) or not menus or need < 1 or len(kept) >= 2:
        return chosen
    menu = next((i for i in chosen if kinds.get(i) == "menu"), menus[0])
    others = [i for i in chosen if i != menu][: need - 1]
    if not others and not kept:
        return [menu]
    pos = 1 - len(kept)  # second photo on the page counting the ones already there
    return others[:pos] + [menu] + others[pos:]


def store_photos(raw):
    """The store's photos in slot order."""
    return [raw[f"img_{i}"] for i in range(services.PHOTO_SLOTS) if raw.get(f"img_{i}")]


def photo_exists(path):
    from legacy.listing import media_exists

    return bool(path) and media_exists(f"{services.SKIN}{path}")


def preview(image):
    """A small JPEG of a photo for Claude to look at (fewer image tokens)."""
    small = image.convert("RGB")
    small.thumbnail((512, 512))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=70)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


def webp(image):
    """The photo as it is stored: at most 1200px, WebP, no camera metadata."""
    out = image.convert("RGB")
    out.thumbnail((1200, 1200))
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=80, method=4)  # method 6 is several times slower for a few % smaller files
    return buf.getvalue()


# ── duplicate listings ────────────────────────────────────────────────
# Words that say where a branch is or what kind of shop it is, not which
# shop: two listings must share more than these to be the same store.
PLACE_WORDS_KO = ("스트라스필드", "스트라", "이스트우드", "채스우드", "체스우드", "버우드", "리드컴", "리드콤", "캠시",
                  "파라마타", "웨스트라이드", "뉴잉톤", "블랙타운", "홈부쉬", "에핑", "라이드", "혼스비", "킬라라",
                  "맥쿼리", "노스라이드", "시드니", "멜번", "멜버른", "브리즈번", "골드코스트", "애들레이드", "퍼스",
                  "캔버라", "호주", "시티", "본사", "지점", "본점", "호점")
GENERIC_KO = ("미용실", "헤어", "한식당", "식당", "음식점", "레스토랑", "병원", "의원", "치과", "학원", "교회",
              "보험", "부동산", "회계사", "회계", "변호사", "법무", "여행사", "이민", "유학원", "컨설팅", "센터",
              "클리닉", "마트", "샵", "스토어", "코리아", "한국", "전기공사", "전기", "공사", "페인팅", "크리닝",
              "클리닝", "청소", "공항픽업", "픽업", "대리운전", "운전학원", "옷수선", "건강식품", "식품", "도매",
              "총판", "유리", "자동차", "의료기", "화장품", "이사", "운송", "여행", "투어", "서비스", "컴퓨터",
              "디자인", "프린팅", "인쇄", "간판", "전문점", "전문")


def core_name(title):
    """(latin tokens, hangul) of a listing's name without bracketed notes,
    place words, branch numbers and kind-of-shop words."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title or "")
    t = re.sub(r"\d+\s*호점", " ", t)
    ko = hangul(t)
    for w in PLACE_WORDS_KO + GENERIC_KO:
        ko = ko.replace(w, "")
    latin = latin_tokens(t) - suburb_words()
    return latin, ko


_suburb_words = []


def suburb_words():
    if not _suburb_words:
        _suburb_words.append({n.lower() for n in Suburb.objects.values_list("name", flat=True) if " " not in n}
                             | {"city", "branch", "hair", "salon", "dental", "clinic", "mart", "insurance"})
    return _suburb_words[0]


def plain_name(title):
    return re.sub(r"[^0-9a-z가-힣]", "", re.sub(r"\(.*?\)|\[.*?\]", "", (title or "").lower()))


def same_name(a, b):
    if plain_name(a) and plain_name(a) == plain_name(b):
        return True
    la, ka = core_name(a)
    lb, kb = core_name(b)
    if len(ka) >= 2 and len(kb) >= 2:
        # Both have a Korean name: it decides ("AMP 방경필" is not "AMP 이용수").
        if min(len(ka), len(kb)) < 3:  # "가나", "신라": too short to be anything but equal
            return ka == kb
        pa = {ka[i:i + 2] for i in range(len(ka) - 1)}
        pb = {kb[i:i + 2] for i in range(len(kb) - 1)}
        return len(pa & pb) / max(len(pa), len(pb)) >= 0.6
    if (len(ka) >= 2) != (len(kb) >= 2):
        # Only one has a Korean name: the English names must be the same.
        return bool(la) and la == lb and len(la) >= 2
    # English names only: shared words must be most of both.
    return bool(la and lb) and len(la & lb) / max(len(la), len(lb)) > 0.5


def branch_words(title):
    """Place words in a listing's name ("진가김치 (퍼스)" -> {"퍼스"})."""
    t = title or ""
    return ({w for w in PLACE_WORDS_KO if w in t and w not in ("본사", "지점", "본점", "호점")}
            | (latin_tokens(t) & suburb_words()))


def same_store(a, b, places):
    """Whether two listings are the same shop: similar names (ignoring
    suburbs, branch notes and kind-of-shop words) plus the same Google place,
    or the same phone close together. Different people sharing an office
    (agents, accountants) have different names and stay separate; branches
    far apart are never merged."""
    ra, rb = a.raw, b.raw
    if not same_name(ra.get("title", ""), rb.get("title", "")):
        return False
    ca, cb = store_coords(ra), store_coords(rb)
    apart = distance_m(*ca, *cb) if ca and cb else None
    if apart is not None and apart > 1000:
        return False
    if a.state_id and b.state_id and a.state_id != b.state_id:
        return False
    # Without map positions, branches show only in their suburbs and names.
    same_suburb = bool(a.suburb_id) and a.suburb_id == b.suburb_id
    if apart is None and a.suburb_id and b.suburb_id and not same_suburb:
        return False
    if apart is None and branch_words(ra.get("title")) != branch_words(rb.get("title")):
        return False
    if places.get(a.pk) and places.get(a.pk) == places.get(b.pk):
        return True
    pa, pb = digits(ra.get("phone")), digits(rb.get("phone"))
    phone = len(pa) >= 8 and len(pb) >= 8 and pa[-8:] == pb[-8:]
    return phone and (apart <= 300 if apart is not None else same_suburb)


def keeper_rank(store):
    raw = store.raw
    return (len(store_photos(raw)), bool(raw.get("review")), bool(raw.get("short_comment")),
            has_both_scripts(raw.get("title")), -store.pk)


def duplicate_groups(stores):
    """([[keeper id, dup id, ...], ...], [log lines for listings left alone])."""
    stores = list(stores)
    by_id = {s.pk: s for s in stores}
    places = dict(StorePlace.objects.exclude(place_id="").filter(store_id__in=by_id)
                  .values_list("store_id", "place_id"))
    buckets = collections.defaultdict(set)
    for s in stores:
        if places.get(s.pk):
            buckets["p" + places[s.pk]].add(s.pk)
        phone = digits(s.raw.get("phone"))
        if len(phone) >= 8:
            buckets["t" + phone[-8:]].add(s.pk)
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for members in buckets.values():
        members = sorted(members)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if find(a) != find(b) and same_store(by_id[a], by_id[b], places):
                    parent[find(b)] = find(a)
    comps = collections.defaultdict(list)
    for pk in by_id:
        comps[find(pk)].append(pk)
    groups, skipped = [], []
    for members in comps.values():
        if len(members) < 2:
            continue
        keeper = max((by_id[pk] for pk in members), key=keeper_rank)
        dups = [pk for pk in sorted(members) if pk != keeper.pk and same_store(keeper, by_id[pk], places)]
        left = [pk for pk in members if pk != keeper.pk and pk not in dups]
        if dups:
            groups.append([keeper.pk] + dups)
        if left:
            skipped.append(f"확인 필요 (합치지 않음): #{keeper.pk} {keeper.raw.get('title', '')} 와 "
                           + ", ".join(f"#{pk} {by_id[pk].raw.get('title', '')}" for pk in left))
    return sorted(groups), skipped


MERGE_FILL = ("short_comment", "review", "build_date", "homepage", "address", "phone", "x_point", "y_point",
              "etc1", "etc3", "etc4")


def merged_raw(keeper, dups):
    """The kept listing's values with the others' categories (as many as the
    three category slots hold), photos and any values it is missing."""
    raw = dict(keeper.raw)
    paths = []
    for s in [keeper] + dups:
        for key in ("category", "category2", "category3"):
            p = s.raw.get(key) or ""
            if p and p not in paths:
                paths.append(p)
    theme = [p for p in paths if p.startswith("테마검색")]
    abc = [p for p in paths if p.startswith("가나다검색")]
    other = [p for p in paths if p not in theme and p not in abc]
    raw["category"] = raw.get("category") or (theme[0] if theme else "")
    raw["category2"] = raw.get("category2") or (abc[0] if abc else "")
    used = {raw["category"], raw["category2"]}
    if not raw.get("category3"):
        extra = [p for p in theme + abc + other if p not in used]
        raw["category3"] = extra[0] if extra else ""
    used.add(raw.get("category3", ""))
    dropped = [p.split("/")[-1] for p in paths if p not in used]
    for s in dups:
        for key in MERGE_FILL:
            if not raw.get(key) and s.raw.get(key):
                raw[key] = s.raw[key]
    own = store_photos(raw)
    photos = list(own)
    for s in dups:
        photos += [p for p in store_photos(s.raw) if p not in photos]
    if len(own) < PHOTOS_PER_STORE:
        photos = photos[:PHOTOS_PER_STORE]
    else:
        photos = own
    for i in range(services.PHOTO_SLOTS):
        raw[f"img_{i}"] = photos[i] if i < len(photos) else ""
    return raw, dropped


def unchecked_count():
    """Public stores the tool has not checked yet."""
    return Store.objects.exclude(id__in=StorePlace.objects.values("store_id")).count()


def theme_pairs():
    """Each theme category path → the 가나다 category it is most often paired with."""
    counts = collections.defaultdict(collections.Counter)
    for c1, c2 in Store.all_objects.values_list("category_path", "category2_path"):
        if c1 and c1.startswith("테마검색/") and "&#" not in c1 and c2:
            counts[c1][c2] += 1
    return {theme: c.most_common(1)[0][0] for theme, c in sorted(counts.items())}


def sample_ids(n):
    """A test sample spread across kinds of business (one theme category at
    a time), preferring stores with a map position and a phone number."""
    rng = random.Random(20260927)
    groups = collections.defaultdict(list)
    for pk, path, raw in Store.objects.values_list("id", "category_path", "raw"):
        if store_coords(raw) and digits(raw.get("phone")):
            groups[path].append(pk)
    themes = sorted(groups, key=lambda t: -len(groups[t]))
    picked = []
    while len(picked) < n and any(groups[t] for t in themes):
        for t in themes:
            if groups[t] and len(picked) < n:
                picked.append(groups[t].pop(rng.randrange(len(groups[t]))))
    return picked


# ── the worker ────────────────────────────────────────────────────────
STALE = datetime.timedelta(minutes=10)


def claim_job():
    """Take the next queued job (or one whose worker died) — only one worker
    runs a job even with several servers."""
    with transaction.atomic():
        stale = timezone.now() - STALE
        # A job whose worker died: stopped if a stop was requested, otherwise resumed.
        StoreUpdateJob.objects.filter(status=StoreUpdateJob.RUNNING, heartbeat__lt=stale, cancel_requested=True).update(
            status=StoreUpdateJob.CANCELLED, finished_at=timezone.now())
        StoreUpdateJob.objects.filter(status=StoreUpdateJob.RUNNING, heartbeat__lt=stale).update(
            status=StoreUpdateJob.QUEUED)
        job = (StoreUpdateJob.objects.select_for_update(skip_locked=True)
               .filter(status=StoreUpdateJob.QUEUED)
               .filter(models.Q(resume_after__isnull=True) | models.Q(resume_after__lte=timezone.now()))
               .order_by("id").first())
        if job is None:
            return None
        job.status = StoreUpdateJob.RUNNING
        job.started_at = job.started_at or timezone.now()
        job.heartbeat = timezone.now()
        job.save()
        return job


def run_job(job):
    try:
        Runner(job).run()
    except Exception as e:
        job.status = StoreUpdateJob.FAILED
        job.log += f"작업 실패: {type(e).__name__}: {e}\n"
        job.finished_at = timezone.now()
        job.save()
        traceback.print_exc()
