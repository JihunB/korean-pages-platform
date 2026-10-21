import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from community import images

register = template.Library()


@register.filter
def is_new(created):
    """A post from the last 7 days (shows the NEW mark)."""
    import datetime

    from django.utils import timezone

    return bool(created) and timezone.now() - created < datetime.timedelta(days=7)


@register.filter
def img(path):
    """Web-size photo URL."""
    return images.url(path)


@register.filter
def card(path):
    """Card thumbnail URL (640x400)."""
    return images.url(path, card=True)


_URL = re.compile(r"https?://[^\s<]+")
_PHONE = re.compile(r"(?<![\d/])(?:\+?61[ -]?|0)4\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)|(?<![\d/])0[2378][ -]?\d{4}[ -]?\d{4}(?!\d)")


@register.filter
def ad_text(text):
    """An ad's text as written, line by line, with its links and phone
    numbers tappable (the card shows the whole ad, not a cut-off excerpt)."""
    lines = []
    for line in (text or "").strip().splitlines():
        out, pos = [], 0
        for m in sorted(list(_URL.finditer(line)) + list(_PHONE.finditer(line)), key=lambda m: m.start()):
            if m.start() < pos:
                continue
            out.append(escape(line[pos:m.start()]))
            found = m.group(0)
            if found.startswith("http"):
                out.append(f'<a href="{escape(found)}" target="_blank" rel="noopener">{escape(found)}</a>')
            else:
                out.append(f'<a href="tel:{re.sub(r"[^\d+]", "", found)}">{escape(found)}</a>')
            pos = m.end()
        out.append(escape(line[pos:]))
        lines.append("".join(out))
    return mark_safe("<br>".join(lines))  # nosec B308 B703 - every piece escaped above; links only http(s) / digit-only tel:


_BOLD = re.compile(r"\*\*(.+?)\*\*")


@register.filter
def story_html(text):
    """A 후기 body as readable HTML: "■ " lines become headings, "- " lines
    lists, lines starting with 💡 / ✅ / ⚠️ / 📌 highlighted boxes, **words**
    bold, other lines paragraphs. Everything is escaped first."""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul class=\"kp-st-list\">" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in (text or "").splitlines():
        line = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escape(raw.strip()))
        if not line:
            flush()
            continue
        if line.startswith(("■", "## ")):
            flush()
            out.append(f"<h3 class=\"kp-st-h\">{line.lstrip('■#').strip()}</h3>")
        elif line.startswith(("- ", "• ", "· ")):
            items.append(line[2:].strip())
        elif line.startswith(("💡", "✅", "📌")):
            flush()
            out.append(f"<div class=\"kp-st-tip\">{line}</div>")
        elif line.startswith(("⚠️", "⚠", "❗")):
            flush()
            out.append(f"<div class=\"kp-st-warn\">{line}</div>")
        else:
            flush()
            out.append(f"<p>{line}</p>")
    flush()
    return mark_safe("".join(out))  # nosec B308 B703 - each line escaped before any markup is added


@register.filter
def theme_label(path):
    """The 테마 group a post is filed under (테마검색/건강스페셜/한의원 → 건강)."""
    from directory.models import Category, menu_label

    parts = (path or "").split("/")
    if len(parts) < 2 or parts[0] != "테마검색":
        return ""
    group = Category.objects.filter(legacy_path="/".join(parts[:2])).values_list("name", flat=True).first()
    return menu_label(group or "").split("/")[0].strip()
