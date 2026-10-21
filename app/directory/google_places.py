"""Google Maps Platform — Places API (New), used by the store-update tool.

Two calls:

* ``search_ids`` — Text Search asking only for place IDs. Google does not
  charge for this ("Text Search Essentials IDs Only"), so the tool can
  search freely and pays only for the places it looks at in detail.
* ``details`` — one Place Details request with every field the store page
  uses (name, address, phone, hours, website, amenities, rating, reviews).
  Those fields put the request in the "Enterprise + Atmosphere" price tier.

Every call is counted in ``usage`` so each run can report what it cost.
"""
import json
import threading
import time
import urllib.error
import urllib.request

from django.conf import settings

BASE = "https://places.googleapis.com/v1"

DETAIL_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "addressComponents", "location", "businessStatus",
    "googleMapsUri", "types", "primaryType", "primaryTypeDisplayName",
    "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri", "regularOpeningHours",
    "rating", "userRatingCount", "priceLevel", "editorialSummary", "reviews",
    "parkingOptions", "paymentOptions", "accessibilityOptions", "delivery", "dineIn", "takeout",
    "reservable", "outdoorSeating", "goodForChildren", "goodForGroups", "allowsDogs",
    "servesVegetarianFood", "restroom", "liveMusic", "menuForChildren",
])

# US$ per request at Google's list prices (per 1,000: IDs-only search free,
# Place Details Enterprise + Atmosphere $25). Monthly free allowances and
# the account's trial credit make the real bill lower; this is an upper bound.
# Photo lists come with the free "IDs Only" details tier; each downloaded
# photo is a "Place Details Photos" event ($7 per 1,000; 1,000 free a month).
PRICE = {"search_ids": 0.0, "details": 0.025, "search_full": 0.04, "photo_list": 0.0, "photo": 0.007,
         "details_check": 0.02, "search_franchise": 0.035}

# Franchise searches: a store page's facts without reviews or atmosphere
# fields ("Text Search Enterprise", $35 per 1,000 pages of 20).
FRANCHISE_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "addressComponents", "location", "businessStatus", "googleMapsUri",
    "types", "primaryType", "nationalPhoneNumber", "websiteUri", "regularOpeningHours", "rating", "userRatingCount",
])

# The monthly check of listed stores: only what it compares (status, name,
# address, location, phone, hours, rating and review count). No reviews or
# atmosphere fields, so the request is "Place Details Enterprise" ($20 per
# 1,000, 1,000 free a month) instead of Enterprise + Atmosphere.
CHECK_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "addressComponents", "location", "businessStatus",
    "nationalPhoneNumber", "regularOpeningHours", "rating", "userRatingCount",
])


class PlacesError(Exception):
    pass


_lock = threading.Lock()


class Places:
    def __init__(self, api_key=None, usage=None):
        self.key = api_key or settings.GOOGLE_MAPS_API_KEY
        if not self.key:
            raise PlacesError("GOOGLE_MAPS_API_KEY가 설정되지 않았습니다 (Secrets Manager koreanpages/prod).")
        self.usage = usage if usage is not None else {}

    def _count(self, sku):
        with _lock:
            self.usage[sku] = self.usage.get(sku, 0) + 1

    def _request(self, url, *, body=None, field_mask):
        headers = {"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": field_mask}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - URL built from the fixed https BASE
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise PlacesError(f"Google {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise PlacesError(f"Google 연결 오류: {e.reason}") from e

    def search_ids(self, query, *, lat=None, lng=None, radius=2000, max_results=5):
        """Place IDs for a text query, optionally biased to a circle."""
        body = {"textQuery": query, "regionCode": "AU", "pageSize": max_results}
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": float(radius)}}
        self._count("search_ids")
        result = self._request(f"{BASE}/places:searchText", body=body, field_mask="places.id")
        return [p["id"] for p in result.get("places", [])]

    def search_ids_all(self, query, *, lat, lng, radius):
        """Up to 60 IDs (3 pages) for area discovery."""
        ids, token = [], None
        for _ in range(3):
            body = {"textQuery": query, "regionCode": "AU", "pageSize": 20,
                    "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                "radius": float(radius)}}}
            if token:
                body["pageToken"] = token
            self._count("search_ids")
            result = self._request(f"{BASE}/places:searchText", body=body,
                                   field_mask="places.id,nextPageToken")
            ids += [p["id"] for p in result.get("places", [])]
            token = result.get("nextPageToken")
            if not token:
                break
            time.sleep(2)  # the next page becomes valid after a short delay
        return ids

    def search_places(self, query, *, lat, lng, radius, pages=3):
        """Discovery: up to 60 places with every field the store page uses,
        20 per request (Text Search, Enterprise + Atmosphere) — far cheaper
        per place than a details call for each."""
        mask = ",".join("places." + f for f in DETAIL_FIELDS.split(",")) + ",nextPageToken"
        found, token = [], None
        for _ in range(pages):
            body = {"textQuery": query, "regionCode": "AU", "pageSize": 20, "languageCode": "en",
                    "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                "radius": float(radius)}}}
            if token:
                body["pageToken"] = token
            self._count("search_full")
            result = self._request(f"{BASE}/places:searchText", body=body, field_mask=mask)
            found += result.get("places", [])
            token = result.get("nextPageToken")
            if not token:
                break
            time.sleep(2)  # the next page becomes valid after a short delay
        return found

    @staticmethod
    def _rect_body(query, rect, included_type):
        lat1, lng1, lat2, lng2 = rect
        body = {"textQuery": query, "regionCode": "AU", "pageSize": 20, "languageCode": "en",
                "locationRestriction": {"rectangle": {"low": {"latitude": lat1, "longitude": lng1},
                                                      "high": {"latitude": lat2, "longitude": lng2}}}}
        if included_type:
            body["includedType"] = included_type
            body["strictTypeFiltering"] = True
        return body

    def search_ids_rect(self, query, rect, included_type=None):
        """Up to 60 place IDs inside a rectangle (free: IDs only). 60 means
        there may be more, so the caller splits the rectangle."""
        ids, token = [], None
        for _ in range(3):
            body = self._rect_body(query, rect, included_type)
            if token:
                body["pageToken"] = token
            self._count("search_ids")
            result = self._request(f"{BASE}/places:searchText", body=body, field_mask="places.id,nextPageToken")
            ids += [p["id"] for p in result.get("places", [])]
            token = result.get("nextPageToken")
            if not token:
                break
            time.sleep(2)
        return ids

    def search_rect_page(self, query, rect, token=None):
        """One page (20) of a franchise search inside a rectangle, with what a
        store page needs but no reviews or atmosphere fields ("Text Search
        Enterprise", cheaper). Returns (places, next page token)."""
        body = self._rect_body(query, rect, None)
        if token:
            body["pageToken"] = token
        mask = ",".join("places." + f for f in FRANCHISE_FIELDS.split(",")) + ",nextPageToken"
        self._count("search_franchise")
        result = self._request(f"{BASE}/places:searchText", body=body, field_mask=mask)
        return result.get("places", []), result.get("nextPageToken")

    def search_places_rect(self, query, rect, included_type=None, pages=3):
        """Every field for up to 60 places inside a rectangle (20 per paid request)."""
        mask = ",".join("places." + f for f in DETAIL_FIELDS.split(",")) + ",nextPageToken"
        found, token = [], None
        for _ in range(pages):
            body = self._rect_body(query, rect, included_type)
            if token:
                body["pageToken"] = token
            self._count("search_full")
            result = self._request(f"{BASE}/places:searchText", body=body, field_mask=mask)
            found += result.get("places", [])
            token = result.get("nextPageToken")
            if not token:
                break
            time.sleep(2)
        return found

    def photo_list(self, place_id):
        """The place's photos (up to 10), Google's own order: name, size and
        who took each one."""
        self._count("photo_list")
        result = self._request(f"{BASE}/places/{place_id}", field_mask="id,photos")
        return result.get("photos") or []

    def photo(self, name, max_px=1600):
        """One photo's image bytes, at most max_px on its longer side."""
        url = f"{BASE}/{name}/media?maxWidthPx={max_px}&maxHeightPx={max_px}"
        self._count("photo")
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"X-Goog-Api-Key": self.key})
                with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - URL built from the fixed https BASE
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise PlacesError(f"Google {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise PlacesError(f"Google 연결 오류: {e.reason}") from e

    def details(self, place_id):
        self._count("details")
        return self._request(f"{BASE}/places/{place_id}?languageCode=en&regionCode=AU",
                             field_mask=DETAIL_FIELDS)

    def check_details(self, place_id):
        self._count("details_check")
        return self._request(f"{BASE}/places/{place_id}?languageCode=en&regionCode=AU",
                             field_mask=CHECK_FIELDS)


def cost_of(usage):
    return round(sum(PRICE[sku] * usage.get(sku, 0) for sku in PRICE), 2)
