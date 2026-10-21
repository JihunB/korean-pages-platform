"""Member login, signup and password reset, at the legacy URLs and with the
legacy request/response contracts, so the original pages' JavaScript runs
unchanged.

Ports of: happy_member_login.php (mode=login_reg / logout),
kp_register.php, kp_verify_code.php, kp_resend_code.php,
kp_request_password_reset.php, kp_verify_reset_code.php,
kp_reset_password.php and happy_member_ajax_check_nick.php. Every message
string is copied from those files.

The new site starts with zero accounts (the old ones are not migrated), so
passwords use Django's hasher rather than the CMS's Happy_Secret_Code().
"""
import logging
import re
from urllib.parse import urlparse

from django.contrib.auth.hashers import make_password
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import EmailCode, Member, PasswordResetToken, PendingRegistration

from .api import php_json
from .site import MAIN_URL, SITE_NAME

logger = logging.getLogger(__name__)

SESSION_KEY = "kp_member_id"
PASSWORD_FAIL_LIMIT = 5  # HAPPY_CONFIG['password_fail_limit']
MAIL_CHECK = re.compile(
    r"^([\w-]+(?:\.[\w-]+)*)@((?:[\w-]+\.)*\w[\w-]{0,66})\.([a-z]{2,6}(?:\.[a-z]{2})?)$",
    re.I | re.ASCII,
)


# ── session ───────────────────────────────────────────────────────────
def current(request):
    mid = request.session.get(SESSION_KEY)
    if not mid:
        return None
    member = Member.objects.filter(id=mid, is_active=True).first()
    if member is None:
        request.session.pop(SESSION_KEY, None)
    return member


def logout(request):
    request.session.flush()


def same_origin(request):
    """The legacy endpoints take plain form/XHR posts without a CSRF token
    (the original page scripts send none), so cross-site posts are refused
    by checking Origin / Referer against this host instead."""
    source = request.META.get("HTTP_ORIGIN") or request.META.get("HTTP_REFERER") or ""
    if not source:
        return False
    return urlparse(source).netloc == request.get_host()


def page(text):
    return HttpResponse(text, content_type="text/html; charset=utf-8")


def php_error(text):
    # inc/function.php error()
    return page(
        f"<script>\nwindow.alert('{text}');\nhistory.go(-1);\n\n"
        "if ( window.name == 'BTPG_WALLET' )\n{\n\twindow.close();\n}\n</script>"
    )


# ── happy_member_login.php?mode=login_reg ─────────────────────────────
def return_url(request):
    url = request.POST.get("returnUrl") or request.GET.get("returnUrl") or "./"
    if url in ("", "./"):
        url = request.META.get("HTTP_REFERER", "")
    if re.search(r"happy_member_login", url) or (
        re.search(r"happy_member\.php", url) and not re.search(r"mode=mypage", url)
    ):
        url = "./"
    # Never redirect off-site.
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != request.get_host():
        url = "./"
    return url.replace("'", "%27")


def login_reg(request, member):
    if member:
        return page("<script>history.go(-1);</script>")
    member_id = request.POST.get("member_id", "").strip().lower()
    password = request.POST.get("member_pass", "")
    if member_id == "" or password == "":
        return php_error("아이디와 비밀번호를 모두 입력해주세요.")
    user = Member.objects.filter(login_id=member_id).first()
    if user is None:
        return php_error(f"[{member_id}] 아이디가 없습니다.")
    if not user.is_active:
        return php_error("이용이 정지된 계정입니다.")
    if not user.check_password(password):
        user.password_fail_count += 1
        user.save(update_fields=["password_fail_count"])
        if user.password_fail_count >= PASSWORD_FAIL_LIMIT:
            return page("<META HTTP-EQUIV=refresh CONTENT=0;URL=happy_member_login.php?mode=fail_limit_out>")
        return php_error(
            f"비밀번호를 {user.password_fail_count}회 틀렸습니다.\\n"
            f"{PASSWORD_FAIL_LIMIT}회 비밀번호 입력 실패시, 비밀번호 재설정 후 로그인 가능합니다."
        )
    if user.password_fail_count >= PASSWORD_FAIL_LIMIT:
        return page("<META HTTP-EQUIV=refresh CONTENT=0;URL=happy_member_login.php?mode=fail_limit_out>")
    request.session.cycle_key()
    request.session[SESSION_KEY] = user.id
    user.password_fail_count = 0
    user.login_count += 1
    user.last_login_at = timezone.now()
    user.save(update_fields=["password_fail_count", "login_count", "last_login_at"])
    return page(f"<script>window.location.replace('{return_url(request)}');</script>")


# ── verification emails (kp_register.php / kp_request_password_reset.php) ──
EMAIL_HTML = """
<table width='100%' cellpadding='0' cellspacing='0' style='background:#dcdcdc; padding:40px 0; font-family:-apple-system,Helvetica,Arial,sans-serif;'>
<tr><td align='center'>
<table width='600' cellpadding='0' cellspacing='0' style='background:#ffffff; max-width:600px; width:100%;'>
	<tr>
		<td align='center' style='background:#f3f3f3; padding:40px 20px;'>
			<div style='font-size:28px; font-weight:800; color:#191919;'>{heading}</div>
		</td>
	</tr>
	<tr>
		<td align='center' style='padding:48px 32px 40px;'>
			<img src='{site_url}/skin/lifeb/upload/happy_config/copyright_email.png' alt='{site}' style='max-width:160px; height:auto; margin-bottom:32px;'>
			<div style='font-size:15px; color:#333; margin-bottom:32px;'>{lead}</div>
			<div style='font-size:42px; font-weight:800; color:#191919; letter-spacing:6px; margin-bottom:32px;'>{code}</div>
			<div style='font-size:14px; color:#666; line-height:1.8;'>
				해당 인증번호는 10분간 유효합니다.<br>
				본인이 요청하지 않은 경우 이 메일을 무시하셔도 됩니다.<br><br>
				감사합니다.
			</div>
		</td>
	</tr>
</table>
</td></tr>
</table>
"""
SIGNUP_MAIL = ("회원가입 인증", "Verify your login", "회원가입을 위한 인증번호를 안내드립니다.")
RESET_MAIL = ("비밀번호 재설정 인증", "Reset your password", "비밀번호 재설정을 위한 인증번호를 안내드립니다.")


def send_code(email, code, kind):
    subject, heading, lead = kind
    html = EMAIL_HTML.replace("{heading}", heading).replace("{site}", SITE_NAME) \
        .replace("{site_url}", MAIN_URL) \
        .replace("{lead}", lead).replace("{code}", code)
    msg = EmailMultiAlternatives(f"{SITE_NAME} {subject}", "", None, [email])
    msg.attach_alternative(html, "text/html")
    try:
        msg.send()
    except Exception:
        # As on the live site, a failed send does not fail the request; the
        # code stays valid and "resend" can be used. It is logged so SES
        # problems (e.g. sandbox limits) show up in the server log.
        logger.exception("Verification email to %s failed", email)


# ── JSON endpoints ────────────────────────────────────────────────────
def fail(message, error=None):
    body = {"ok": False, "error": error, "message": message} if error else {"ok": False, "message": message}
    return php_json(body)


def post_only(view):
    @csrf_exempt
    def wrapped(request):
        if request.method != "POST" or not same_origin(request):
            return php_json({"ok": False, "message": "잘못된 요청입니다. 처음부터 다시 시도해 주세요."})
        return view(request)
    return wrapped


@post_only
def register(request):
    """kp_register.php"""
    p = request.POST
    name = p.get("user_name", "").strip()
    email = p.get("user_email", "").lower().strip()
    password = p.get("user_pass", "")

    def num(key):
        try:
            return int(p.get(key, "0"))
        except ValueError:
            return 0

    year, month, day = num("birth_year"), num("birth_month"), num("birth_day")
    gender = p.get("gender", "")
    region_id, region_si = num("region_id"), num("region_si")

    if name == "":
        return fail("이름을 입력해 주세요.", "missing_field")
    if email == "" or not MAIL_CHECK.match(email):
        return fail("올바른 이메일 주소를 입력해 주세요.", "invalid_email")
    if len(password.encode("utf-8")) < 8:
        return fail("비밀번호는 8자 이상이어야 합니다.", "weak_password")
    if year < 1900 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return fail("생년월일을 올바르게 선택해 주세요.", "invalid_birth")
    if gender not in ("man", "girl"):
        return fail("성별을 선택해 주세요.", "invalid_gender")
    if not region_id:
        return fail("지역을 선택해 주세요.", "invalid_region")
    if Member.objects.filter(login_id=email).exists() or Member.objects.filter(email=email).exists():
        return fail("이미 사용 중인 이메일입니다.", "email_taken")
    if Member.objects.filter(nickname=name).exists():
        return fail("이미 사용 중인 닉네임입니다.", "nickname_taken")

    pending = PendingRegistration(
        email=email, name=name, nickname=name, birth_year=year, birth_month=month,
        birth_day=day, gender=gender, suburb_id=region_id or None, state_id=region_si or None,
    )
    pending.password = make_password(password)
    with transaction.atomic():
        PendingRegistration.objects.filter(email=email).delete()
        pending.save()
        code = EmailCode.issue(email, EmailCode.SIGNUP).code
    send_code(email, code, SIGNUP_MAIL)
    return php_json({"ok": True})


@post_only
def verify_code(request):
    """kp_verify_code.php"""
    email = request.POST.get("email", "").lower().strip()
    code = request.POST.get("code", "").strip()
    if email == "" or code == "":
        return fail("이메일과 인증 코드를 모두 입력해 주세요.")
    valid = EmailCode.objects.filter(
        email=email, code=code, purpose=EmailCode.SIGNUP, expires_at__gte=timezone.now()
    ).exists()
    if not valid:
        return fail("인증 코드가 올바르지 않거나 만료되었습니다.")
    pending = PendingRegistration.objects.filter(email=email).order_by("-id").first()
    if pending is None:
        return fail("회원가입 정보를 찾을 수 없습니다. 다시 가입해 주세요.")
    if Member.objects.filter(login_id=email).exists() or Member.objects.filter(email=email).exists():
        PendingRegistration.objects.filter(email=email).delete()
        EmailCode.objects.filter(email=email, purpose=EmailCode.SIGNUP).delete()
        return fail("이미 사용 중인 이메일입니다.")
    with transaction.atomic():
        Member.objects.create(
            login_id=email, email=email, password=pending.password, name=pending.name,
            nickname=pending.nickname, birth_year=pending.birth_year,
            birth_month=pending.birth_month, birth_day=pending.birth_day,
            gender=pending.gender, suburb_id=pending.suburb_id, state_id=pending.state_id,
            email_verified=True, password_changed_at=timezone.now(),
        )
        PendingRegistration.objects.filter(email=email).delete()
        EmailCode.objects.filter(email=email, purpose=EmailCode.SIGNUP).delete()
    return php_json({"ok": True})


@post_only
def resend_code(request):
    """kp_resend_code.php"""
    email = request.POST.get("email", "").lower().strip()
    if email == "":
        return fail("이메일을 입력해 주세요.")
    if not PendingRegistration.objects.filter(email=email).exists():
        return fail("회원가입 정보를 찾을 수 없습니다. 다시 가입해 주세요.")
    code = EmailCode.issue(email, EmailCode.SIGNUP).code
    send_code(email, code, SIGNUP_MAIL)
    return php_json({"ok": True})


@post_only
def request_password_reset(request):
    """kp_request_password_reset.php — answers the same whether or not the
    address has an account."""
    email = request.POST.get("email", "").lower().strip()
    if email == "" or not MAIL_CHECK.match(email):
        return fail("올바른 이메일 주소를 입력해 주세요.")
    if Member.objects.filter(email=email).exists():
        code = EmailCode.issue(email, EmailCode.RESET).code
        send_code(email, code, RESET_MAIL)
    return php_json({"ok": True})


@post_only
def verify_reset_code(request):
    """kp_verify_reset_code.php"""
    email = request.POST.get("email", "").lower().strip()
    code = request.POST.get("code", "").strip()
    if email == "" or code == "":
        return fail("이메일과 인증 코드를 모두 입력해 주세요.")
    valid = EmailCode.objects.filter(
        email=email, code=code, purpose=EmailCode.RESET, expires_at__gte=timezone.now()
    ).exists()
    if not valid:
        return fail("인증 코드가 올바르지 않거나 만료되었습니다.")
    EmailCode.objects.filter(email=email, purpose=EmailCode.RESET).delete()
    token = PasswordResetToken.issue(email).token
    return php_json({"ok": True, "token": token})


@post_only
def reset_password(request):
    """kp_reset_password.php"""
    email = request.POST.get("email", "").lower().strip()
    token = request.POST.get("token", "").strip()
    password = request.POST.get("new_pass", "")
    if email == "" or token == "":
        return fail("잘못된 요청입니다. 처음부터 다시 시도해 주세요.")
    if len(password.encode("utf-8")) < 8:
        return fail("비밀번호는 8자 이상이어야 합니다.")
    valid = PasswordResetToken.objects.filter(
        email=email, token=token, expires_at__gte=timezone.now()
    ).exists()
    if not valid:
        return fail("인증이 만료되었습니다. 처음부터 다시 시도해 주세요.")
    for member in Member.objects.filter(email=email):
        member.set_password(password)
        member.password_fail_count = 0
        member.save(update_fields=["password", "password_changed_at", "password_fail_count"])
    PasswordResetToken.objects.filter(email=email).delete()
    return php_json({"ok": True})


def check_nick(request):
    """happy_member_ajax_check_nick.php — plain-text 'input' / 'ok' / 'no'."""
    nick = request.GET.get("nick", "").strip()
    now_id = request.GET.get("nowid", "").strip()
    if nick == "":
        return HttpResponse("input", content_type="text/html; charset=utf-8")
    taken = Member.objects.filter(nickname=nick)
    if now_id:
        taken = taken.exclude(login_id=now_id)
    return HttpResponse("no" if taken.exists() else "ok", content_type="text/html; charset=utf-8")


def display_names(login_ids):
    """Reviewer names as kp_review_list.php shows them: nickname, else name."""
    out = {}
    for login_id, nick, name in Member.objects.filter(login_id__in=login_ids).values_list("login_id", "nickname", "name"):
        out[login_id] = nick or name
    return out
