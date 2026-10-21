"""Writes a store's listing text with Claude (on Amazon Bedrock, billed to
the AWS account): confirms the Google place is the same business, and
produces the title, one-line intro and the structured 상세소개 (description)
the store page shows, in one fixed format for every kind of business.

Only facts given in the request may be used; nothing is invented.
"""
import json
import threading

from django.conf import settings
from django.utils import timezone

# The description format, the same for every store. Sections without
# information are left out rather than filled with guesses.
SYSTEM_PROMPT = """당신은 호주의 한인·일본 업체 디렉터리 "코리안 페이지"(koreanpages.com.au)의 편집자입니다.
업체마다 사이트 정보(기존 등록 내용)와 Google 지도 정보를 받아, 업체 상세 페이지에 실릴 내용을 작성합니다.

## 1. 같은 업체 고르기 (candidate_index, same_business)
google_candidates에는 Google 지도에서 찾은 후보가 최대 3개 있습니다. 기존 등록 업체와 같은 업체인 후보를 골라
candidate_index(0부터 시작하는 번호)에 적고 same_business를 true로 하세요. 해당하는 후보가 없으면 candidate_index -1, same_business false.
- 전화번호가 같으면 거의 확실히 같은 업체입니다.
- 상호가 영어/한글로 다르게 표기된 경우(예: 백양장로교회 = Baik Yang Presbyterian Church), 같은 업체가 다른 곳으로 이전한 경우도 같은 업체입니다.
- 이름이 비슷해도 업종이 다르거나(예: 미용실과 피부관리실), 같은 건물·쇼핑센터 자체를 가리키는 후보는 다른 업체입니다.
match_reason에 판단 근거를 한 문장으로 적으세요. 아래 2~4번은 고른 후보의 정보로 작성합니다(후보가 없으면 빈 문자열).
name_changed: 고른 후보의 Google 상호가 기존 등록명의 번역·음역·약칭·지점 표기 차이가 아니라 전혀 다른 이름이면 true
(예: 한스헤어클럽 → K-Pop Hair — 상호 변경이나 인수 가능성). 같은 이름을 다른 언어로 쓴 것이면 false
(예: 한양이민 → Hanyang Migration Consultants). 후보가 없으면 false.
category_changed: 고른 후보의 업종이 기존 등록 카테고리와 전혀 다르면 true (예: 자동차 정비 → 폐차장, 한식당 → 미용실).
같은 업종이면 false. 후보가 없으면 false.
신규 업체 찾기(mode=new)에서는 후보가 하나이며 candidate_index 0, same_business true로 두세요.
mode=replace: 관리자가 이 등록을 후보(Google 업체)의 정보로 바꾸기로 이미 정했습니다. candidate_index 0,
same_business true로 두세요. 기존 업체와 다른 업체(상호나 업종이 다름)라면 name_changed·category_changed를
그에 맞게 true로 하고, 2~4번을 Google 업체 기준의 새 업체로 작성하세요.

## 2. 업체명 (title)
형식: 항상 "영문 상호 한글 상호" (예: "Raku Izakaya 라쿠 이자카야", "Galbi House 갈비하우스").
- 업체명은 반드시 고른 후보의 Google 상호(name)를 기준으로 합니다. 초안·기존 소개·기존 등록명에 다른 이름이
  있어도 Google 상호가 우선입니다. name_changed도 Google 상호와 기존 등록명을 비교해 정합니다.
- 영문 상호는 Google 이름을 기준으로 하되, 뒤에 붙은 설명(예: "- Korean BBQ Restaurant")은 빼세요.
- name_changed와 category_changed가 모두 false일 때(기존 이름이 같은 이름의 번역·음역일 때)만 기존 등록명의
  한글 표기와 지점명(예: 이스트우드점)을 함께 씁니다.
- name_changed나 category_changed가 true이면 다른 업체입니다. 기존 등록명은 완전히 버리고 Google 상호만으로
  "영문 한글" 형식을 만드세요 (예: 오세오 BBQ Enfield → "Galbi House 갈비하우스").
- 한글 표기가 없으면 자연스러운 한글 음역을, 영문 표기가 없으면 영어 표기(고유명사는 로마자, 업종어는 영어)를
  붙이세요. Google 이름에 한글이 이미 있으면 중복하지 마세요.

## 3. 한줄 소개 (short_comment)
업종과 특징을 담은 25자 이내의 한 줄. 마침표·이모지 없이. (예: "스트라스필드의 숯불 한우 전문 고깃집")

## 4. 상세소개 (description_html)
한국어 존댓말(~입니다/~합니다) 설명문. 과장·광고 표현("최고의", "완벽한" 등)과 이모지는 쓰지 마세요.
**주어진 자료에 있는 사실만 쓰세요.** 메뉴, 가격, 경력, 연혁, 교통편 등을 추측해서 만들면 안 됩니다.
리뷰 내용은 여러 방문객이 공통으로 말한 점만 요약하고, 개인 이름은 쓰지 마세요.

HTML은 <p>, <b>, <br>, <a href="..." target="_blank" rel="noopener"> 만 사용합니다.
목록은 줄마다 "· "로 시작하고 <br>로 나눕니다. 아래 순서로 쓰되, 자료가 없는 섹션은 통째로 생략하세요.

<p>(소개) 2~3문장: 업체명, 업종, 위치(서버브·주), 이 업체의 특징.</p>
<p><b>(업종별 제목)</b><br>· 항목<br>· 항목</p>
  제목: 음식점·카페·베이커리·주점 → "대표 메뉴" / 마트·식품점·상점 → "주요 취급 품목" /
  미용실·네일·피부관리 → "주요 시술" / 병원·치과·한의원·약국 → "진료 안내" /
  교회·성당·사찰·단체 → "예배·모임 안내" / 학원·교육·유학원 → "교육 과정" /
  변호사·회계사·이민·부동산·보험·여행사 등 → "주요 업무" / 그 밖의 업종 → "주요 서비스".
  항목은 자료에 나온 것만, 최대 6개.
<p><b>이용 안내</b><br>· 편의사항(예약, 주차, 포장·배달, 결제 방법 등)<br>· 영업시간 특이사항(브레이크 타임, 휴무일 등)</p>
  영업시간은 페이지에 요일별로 따로 표시되므로 전체 시간표를 나열하지 마세요. 브레이크 타임, 특정 요일 휴무처럼 눈여겨볼 점만 적습니다.
<p><b>방문객 평가</b><br>Google 평점 4.6점 (리뷰 132개). 방문객 평가 요약 1~2문장.</p>  ← 평점 자료가 있을 때만
<p><b>찾아오는 길</b><br>(전체 주소). 자료에 근처 역·쇼핑센터 등의 정보가 있으면 한 문장 덧붙임.</p>
<p><b>웹사이트</b><br><a href="(주소)" target="_blank" rel="noopener">(도메인)</a></p>  ← 일반 웹사이트가 있을 때만 (SNS 링크는 제외)

전체 길이는 350~900자. 기존 소개에 업주가 적은 사실 정보(예약 방법, 특별 서비스 등)가 있으면 반영하세요.
단, name_changed나 category_changed가 true이면 기존 소개는 다른 업체의 것이므로 사용하지 말고, 이전 상호도
설명에 쓰지 마세요.
후보에 draft_description_from_google_data가 있으면 Google 자료로 먼저 써 둔 초안입니다. 사실은 활용하되, 그 안에
이전 상호나 다른 업체 이야기가 들어 있으면 빼고 새로 쓰세요.

## 5. 신규 업체 (mode=new일 때만)
- is_relevant: 한인이 운영하거나, 한국 음식·상품·서비스를 제공하거나, 한인 커뮤니티를 대상으로 하는 업체면 true
  (한식당, 한인 마트, 한인 교회, 한국어 상담이 되는 병원·회계사·변호사·유학원·부동산 등). 상호·리뷰·소개에
  한국과의 관련이 드러나야 합니다. 한인이 운영한다는 근거가 없는 일식·중식·다른 나라 음식점, 단순히 아시아풍
  이름의 일반 업체는 false.
  단, 자료에 discovery_reason이 있으면 그 기준을 따릅니다:
  · job_employer: 한인 구인 게시판에 직원을 구한 업체입니다. 한인 커뮤니티와 관계가 확인된 것이므로, 영업 중인
    실제 가게·회사면 한국 관련 표시가 없어도 true (성인·불법 업소만 false).
  · admin: 관리자가 직접 고른 업체입니다. 영업 중인 실제 가게·회사면 한국 관련 표시가 없어도 true입니다.
  · japanese: 일식당(스시·라멘·이자카야·일본식 카페 등)을 모두 등록합니다. 한인 운영 여부와 관계없이 일식을 파는
    음식점이면 true이고, category는 맛집스페셜의 일식 경로를 적습니다. 일식이 아닌 곳만 false.
  · chinese: 평점과 리뷰가 좋은 인기 중식당입니다. 한인 운영 여부와 관계없이 중식을 파는 음식점이면 true이고,
    category는 맛집스페셜의 중식 경로를 적습니다. 중식이 아닌 곳만 false.
- category: 아래 "카테고리 목록"에서 가장 알맞은 경로 하나를 그대로 적으세요. mode=verify에서는
  category_changed가 true이고 카테고리 목록이 주어졌을 때만 새 업종에 맞는 경로를 적고, 그 밖에는 빈 문자열.
- relevance_reason: 판단 근거 한 문장.

notes: 관리자가 알아야 할 점(예: "Google 상호가 기존과 다름", "브레이크 타임 있음", "임시 휴업 중")을 짧게. 없으면 빈 문자열.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_index": {"type": "integer"},
        "same_business": {"type": "boolean"},
        "name_changed": {"type": "boolean"},
        "category_changed": {"type": "boolean"},
        "match_reason": {"type": "string"},
        "is_relevant": {"type": "boolean"},
        "relevance_reason": {"type": "string"},
        "category": {"type": "string"},
        "title": {"type": "string"},
        "short_comment": {"type": "string"},
        "description_html": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["candidate_index", "same_business", "name_changed", "category_changed", "match_reason", "is_relevant", "relevance_reason", "category",
                 "title", "short_comment", "description_html", "notes"],
    "additionalProperties": False,
}

# US$ per million tokens at list price (Bedrock bills about the same), by
# model. Used only for the run's cost report.
PRICES = {
    "opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}
_lock = threading.Lock()


def prices_for(model):
    return next((p for key, p in PRICES.items() if key in (model or "")), PRICES["opus-4-6"])


# Names only: bring a listing's name to the site's "English 한글" form.
NAME_PROMPT = """당신은 호주 한인 업체 디렉터리 "코리안 페이지"의 편집자입니다. 업체명을 항상 "영문 상호 한글 상호"
형식으로 맞춥니다 (예: "Raku Izakaya 라쿠 이자카야", "Galbi House 갈비하우스", "Hanyang Migration 한양이민",
"Carlingford Mart 칼링포드마트").
- 지금 이름이 한글뿐이면 영문 상호를 앞에 붙입니다. google_name이 같은 업체의 이름이면 그 영문 표기를 쓰고,
  없으면 고유명사는 로마자로, 업종어는 영어로 옮깁니다 (마트→Mart, 치과→Dental, 교회→Church, 회계사→Accounting,
  미용실→Hair Salon, 식당→Restaurant).
- 지금 이름이 영문뿐이면 뒤에 한글 음역(널리 쓰는 표기가 있으면 그것)을 붙입니다.
- 지점명(예: 이스트우드점, Eastwood)은 유지합니다. Pty Ltd 같은 회사 형태는 빼도 됩니다.
- 이름 자체를 다른 이름으로 바꾸지 마세요. 표기만 보탭니다.
title 하나만 답합니다."""
NAME_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}},
               "required": ["title"], "additionalProperties": False}


# Photos: Claude looks at a store's Google photos and picks the ones to show.
PHOTO_MODEL = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
PHOTO_PROMPT = """당신은 호주 한인 업체 디렉터리 "코리안 페이지"의 사진 편집자입니다. 한 업체의 구글 지도 사진들
(번호 0부터, 이미지 순서대로)을 보고, 업체 페이지에 보여 줄 사진을 고릅니다.
- 각 사진의 종류를 정합니다: exterior(가게 외관·간판), interior(내부), menu(메뉴판), food(음식·음료),
  product(상품·진열), service(시술·작업 결과), surroundings(건물 일부·주변 거리·근처 풍경 등 업체 주변),
  people(사람 위주), other(지도·문서·스크린샷·영수증 등).
- usable은 업체를 소개하기에 알맞은 사진만 true입니다. 흐리거나 너무 어두운 사진, 사람 얼굴이 주인공인
  사진·셀카, 영수증·화면 캡처·지도, 다른 업체 사진, 거의 같은 사진의 중복은 false입니다.
- chosen에는 보여 줄 사진 번호를 보여 줄 순서대로, need개까지 넣습니다. 서로 다른 종류를 고르게 섞습니다:
  외관 1장, 내부 1장, 그리고 음식점·카페라면 메뉴판 1장(있으면)과 음식 사진, 다른 업종이면 상품·작업 사진.
  첫 번째 사진은 목록 카드에 쓰이므로 가장 대표적이고 보기 좋은 사진으로 합니다
  (음식점은 먹음직한 음식이나 깔끔한 외관, 그 밖의 업종은 외관이나 간판).
- 쓸 만한 사진이 적으면 chosen을 짧게 두되, 사진이 있는 업체는 반드시 최소 1장은 고르세요. 업체를 잘 보여 주지
  못하는 약한 사진(건물 일부, 간판 일부, 주변 거리, 로고 등)이라도 가장 나은 것을 고릅니다. 다만 사람 얼굴이
  주인공인 사진, 영수증·화면 캡처·개인정보가 보이는 사진은 고르지 않습니다."""
PHOTO_SCHEMA = {
    "type": "object",
    "properties": {
        "photos": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"},
                           "kind": {"type": "string", "enum": ["exterior", "interior", "menu", "food", "product",
                                                              "service", "surroundings", "people", "other"]},
                           "usable": {"type": "boolean"}},
            "required": ["index", "kind", "usable"], "additionalProperties": False}},
        "chosen": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["photos", "chosen"],
    "additionalProperties": False,
}


class WriterError(Exception):
    pass


class DailyLimitError(WriterError):
    """Bedrock refused because the account's daily token allowance is used up."""


class BudgetError(WriterError):
    """This month's Claude budget (CLAUDE_MONTHLY_BUDGET_USD) is used up."""


def _raise_for(e):
    import anthropic

    if isinstance(e, anthropic.RateLimitError) and "per day" in str(e.message):
        raise DailyLimitError("Claude 일일 사용 한도에 도달했습니다.") from e
    raise WriterError(f"Claude {e.status_code}: {str(e.message)[:200]}") from e


class StoreWriter:
    def __init__(self, usage=None, category_list="", model=None, system_prompt=None, schema=None, effort="medium"):
        import anthropic

        self.client = anthropic.AnthropicBedrock(aws_region=settings.BEDROCK_REGION)
        self.model = model or settings.STORE_WRITER_MODEL
        self.usage = usage if usage is not None else {}
        # The system prompt (and, for discovery, the category list) is the
        # same for every store, so it is cached between requests.
        system = (system_prompt or SYSTEM_PROMPT) + (f"\n## 카테고리 목록\n{category_list}\n" if category_list else "")
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.schema = schema or OUTPUT_SCHEMA
        self.effort = effort

    def params(self, facts, images=None):
        """The request for one store. The on-demand and batch paths send
        exactly this, so both produce the same kind of answer. `images` are
        (media type, base64 data) pairs shown before the facts."""
        text = json.dumps(facts, ensure_ascii=False, indent=1)
        content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
                   for mt, data in images or []] + [{"type": "text", "text": text}] if images else text
        params = {
            "max_tokens": 8000,
            "system": self.system,
            "output_config": {"format": {"type": "json_schema", "schema": self.schema}},
            "messages": [{"role": "user", "content": content}],
        }
        if "haiku" not in self.model:  # Haiku 4.5 takes neither adaptive thinking nor effort
            params["thinking"] = {"type": "adaptive"}
            params["output_config"]["effort"] = self.effort
        return params

    def write(self, facts, images=None):
        """On-demand: ask Claude now and return its answer."""
        import anthropic

        check_budget(self.usage)
        try:
            response = self.client.messages.create(model=self.model, **self.params(facts, images))
        except anthropic.APIStatusError as e:
            _raise_for(e)
        except anthropic.APIConnectionError as e:
            raise WriterError(f"Claude 연결 오류: {e}") from e
        return read_answer(response.to_dict(), self.usage, prefix="tokens", model=self.model)

    def batch_record(self, record_id, facts):
        """Batch: the same request as a Bedrock batch record (the body
        InvokeModel receives). Prompt caching does not apply in batch jobs."""
        p = self.params(facts)
        body = {"anthropic_version": "bedrock-2023-05-31",
                **{k: v for k, v in p.items() if k != "system"},
                "system": [{"type": "text", "text": block["text"]} for block in p["system"]]}
        return {"recordId": str(record_id), "modelInput": body}


def read_answer(message, usage, *, prefix, model=None):
    """Claude's JSON answer from a Messages response (as a dict), counting
    its tokens in usage under prefix_input / prefix_output / ..."""
    u = message.get("usage") or {}
    with _lock:
        for key, value in (("input", u.get("input_tokens")), ("output", u.get("output_tokens")),
                           ("cache_read", u.get("cache_read_input_tokens")),
                           ("cache_write", u.get("cache_creation_input_tokens"))):
            usage[f"{prefix}_{key}"] = usage.get(f"{prefix}_{key}", 0) + (value or 0)
        # Spend per calendar month, for the monthly budget.
        price = prices_for(model or settings.STORE_WRITER_MODEL)
        rate = 0.5 if prefix.startswith("batch") else 1.0
        spent = sum(price[k] * (u.get(f) or 0) for k, f in (
            ("input", "input_tokens"), ("output", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")))
        spent = spent * rate / 1_000_000
        key = month_key()
        usage[key] = round(usage.get(key, 0) + spent, 6)
        usage["claude_usd"] = round(usage.get("claude_usd", 0) + spent, 6)
    if message.get("stop_reason") in ("refusal", "max_tokens"):
        raise WriterError(f"Claude 응답이 완료되지 않았습니다 ({message.get('stop_reason')}).")
    text = next((b.get("text", "") for b in message.get("content") or [] if b.get("type") == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise WriterError("Claude 응답을 읽을 수 없습니다.") from e


def month_key():
    return "usd_" + timezone.localtime().strftime("%Y-%m")


def month_spend(usage=None):
    """Claude spend this month across all runs (US$). `usage` is the
    current run's live counters, which may be ahead of its saved copy."""
    from .models import StoreUpdateJob

    key = month_key()
    usage = usage or {}
    saved = (StoreUpdateJob.objects.filter(usage__has_key=key).exclude(pk=usage.get("job_id"))
             .values_list("usage", flat=True))
    from community.models import Ad, ContentImport  # ads and imported posts written by Claude count too

    ads = Ad.objects.filter(ai_usage__has_key=key).exclude(pk=usage.get("ad_id")).values_list("ai_usage", flat=True)
    imports = (ContentImport.objects.filter(usage__has_key=key).exclude(pk=usage.get("import_id"))
               .values_list("usage", flat=True))
    return sum(u.get(key, 0) for u in list(saved) + list(ads) + list(imports)) + usage.get(key, 0)


def check_budget(usage=None, upcoming=0.0):
    """Raise BudgetError when this month's spend (plus `upcoming`) reaches the budget."""
    budget = settings.CLAUDE_MONTHLY_BUDGET_USD
    if budget <= 0:
        return
    spent = month_spend(usage)
    if spent + upcoming >= budget:
        extra = f", 이번 배치 예상 ${upcoming:.2f}" if upcoming else ""
        raise BudgetError(f"이번 달 Claude 예산 ${budget:g} 중 ${spent:.2f} 사용{extra}")


def probe(model=None):
    """True when Claude accepts requests again (a one-token request)."""
    import anthropic

    client = anthropic.AnthropicBedrock(aws_region=settings.BEDROCK_REGION)
    try:
        client.messages.create(model=model or settings.STORE_WRITER_MODEL, max_tokens=1,
                               messages=[{"role": "user", "content": "ok"}])
        return True
    except anthropic.RateLimitError:
        return False


def cost_of(usage, model=None):
    """On-demand tokens at list price plus batch tokens at half price.
    Runs since the monthly budget keep the exact figure, priced by the model
    that answered (claude_usd)."""
    if "claude_usd" in usage:
        return round(usage["claude_usd"], 2)
    price = prices_for(model or settings.STORE_WRITER_MODEL)
    total = sum(price[k] * usage.get(f"tokens_{k}", 0) for k in price)
    total += sum(price[k] * 0.5 * usage.get(f"batch_tokens_{k}", 0) for k in price)
    return round(total / 1_000_000, 2)
