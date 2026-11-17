"""
Kamer-monitor: Hotel Le Sanglier des Ardennes (Durbuy), 28 -> 29 november 2026.

Controleert elke ~10 minuten:
  1. de eigen boekingsmodule van het hotel (Cubilis, gelinkt vanaf sanglier-durbuy.be)
  2. Booking.com
en stuurt een pushmelding naar je telefoon (gratis app "ntfy") zodra er
ergens een kamer beschikbaar is.

Installatie (eenmalig):
    pip install playwright requests
    playwright install chromium

Telefoon: installeer de app "ntfy" (iOS/Android) en abonneer je op het
topic hieronder (NTFY_TOPIC). Kies een eigen, moeilijk te raden naam.

EERST TESTEN:
    python sanglier_monitor.py --test
  -> controleert beide sites één keer, toont het resultaat en bewaart
     screenshots (test_cubilis.png, test_booking.png) zodat je kunt zien
     wat het script ziet.

Daarna starten:
    python sanglier_monitor.py
Laat het venster openstaan (en de computer aan) tot 28 november.
"""

import os
import random
import re
import sys
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

# ---------- instellingen ----------
CHECKIN = "2026-11-28"
CHECKOUT = "2026-11-29"
ADULTS = 2
ROOMS = 1
INTERVAL_MIN = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sanglier-kamer-VERANDER-MIJ-8372")  # <-- of zet als env/secret
STOP_AT = datetime(2026, 11, 28, 12)
# -----------------------------------

d_in = datetime.strptime(CHECKIN, "%Y-%m-%d")
d_out = datetime.strptime(CHECKOUT, "%Y-%m-%d")

HOTEL_SITE = "https://www.sanglier-durbuy.be/nl"
HOTEL_PAGES = [
    "https://www.sanglier-durbuy.be/nl",
    "https://www.sanglier-durbuy.be/en/hotel/rooms-suites",
    "https://www.sanglier-durbuy.be/fr",
]
# Nieuwe Cubilis-boekingsmodule (reservations.cubilis.eu/<hotel>/Rooms/Select)
CUBILIS_SLUGS = ["sanglier-des-ardennes-durbuy", "sanglier-des-ardennes", "hotel-le-sanglier-des-ardennes"]
CUBILIS_START = HOTEL_SITE   # link in de melding: de hotelsite


def cubilis_select_url(base: str) -> str:
    return (f"{base.rstrip('/')}/Rooms/Select?Language=nl-NL"
            f"&Arrival={d_in:%Y-%m-%d}&Departure={d_out:%Y-%m-%d}"
            f"&Room=&Rate=&Package=&DiscountCode=")

BOOKING_URL = (
    "https://www.booking.com/hotel/be/sanglier-des-ardennes.html"
    f"?checkin={CHECKIN}&checkout={CHECKOUT}"
    f"&group_adults={ADULTS}&no_rooms={ROOMS}&group_children=0"
    "&selected_currency=EUR&lang=nl"
)

NO_AVAIL = [
    "geen beschikbaarheid", "niet beschikbaar", "geen kamers beschikbaar",
    "volzet", "volgeboekt", "uitverkocht",
    "aucune disponibilité", "pas de disponibilité", "complet", "indisponible",
    "no availability", "not available", "no rooms available", "sold out",
]
BOOK_WORDS = ["boek", "reserveer", "réserver", "book now", "reserve", "selecteer", "kies"]

# Datums zoals ze op de pagina kunnen staan (controle dat de juiste datums gezocht zijn)
DATE_MARKERS = [
    f"{d_in:%d/%m/%Y}", f"{d_in:%d-%m-%Y}", f"{d_in:%Y-%m-%d}",
    f"{d_in.day} nov", f"{d_in.day} november", f"{d_in.day} novembre",
]


def notify(title: str, message: str, url: str = "", priority: str = "urgent") -> None:
    print(f"[{datetime.now():%H:%M}] MELDING: {title} - {message}")
    headers = {"Title": title, "Priority": priority, "Tags": "hotel"}
    if url:
        headers["Click"] = url
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=message.encode("utf-8"), headers=headers, timeout=15)
    except Exception as e:
        print("  ntfy mislukt:", e)


def accept_cookies(page) -> None:
    for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accepteren')",
                "button:has-text('Alles accepteren')", "button:has-text('Accepter')",
                "button:has-text('Accept')", "button:has-text('OK')"):
        try:
            page.click(sel, timeout=1000)
            return
        except Exception:
            pass


def classify(text: str) -> str:
    t = text.lower()
    has_dates = any(m in t for m in DATE_MARKERS)
    has_price = bool(re.search(r"(€\s?\d{2,4}|\d{2,4}[.,]\d{2}\s?€|eur\s?\d{2,4})", t))
    has_book = any(w in t for w in BOOK_WORDS)
    if any(p in t for p in NO_AVAIL) and not has_price:
        return "unavailable"
    if has_dates and has_price and has_book:
        return "available"
    return "unknown"


# ---------- Cubilis (eigen website van het hotel) ----------
_found_booking_urls: list = []
DEBUG_LOG: list = []


def log(msg: str) -> None:
    DEBUG_LOG.append(msg)
    print("  " + msg)


def discover_booking_links(page) -> list:
    """Zoekt op de hotelsite naar links naar de boekingsmodule."""
    links = []
    for site in HOTEL_PAGES:
        try:
            resp = page.goto(site, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            accept_cookies(page)
            hrefs = page.eval_on_selector_all(
                "a[href], iframe[src], form[action]",
                "els => els.map(e => e.href || e.src || e.action)")
            log(f"hotelsite {site} -> HTTP {resp.status if resp else '?'}, {len(hrefs)} links")
            for h in hrefs:
                if h and re.search(r"(cubilis|stardekk|book|reserv|boek)", h, re.I) \
                        and "booking.com" not in h and h not in links:
                    links.append(h)
        except Exception as e:
            log(f"hotelsite {site} fout: {e}")
        if any("cubilis" in l for l in links):
            break
    for l in links[:15]:
        log(f"boekingslink gevonden: {l}")
    return links


def candidate_urls(links: list) -> list:
    cands = []
    for l in links:
        low = l.lower()
        if "giftcard" in low or "facebook" in low or "booking.com" in low:
            continue
        m = re.match(r"(https?://reservations\.cubilis\.eu/[^/?#]+)", l, re.I)
        if m:
            cands.append(cubilis_select_url(m.group(1)))
        elif "sanglier-durbuy.be" in low or "cubilis" in low or "mylighthouse" in low:
            cands.append(l)
    # voorkeur: Nederlandstalige reserveringspagina eerst
    cands.sort(key=lambda u: (0 if "/nl/" in u else 1))
    cands.append("https://booking.sanglier-durbuy.be/#!/language/nl")
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def page_text_all_frames(page) -> str:
    parts = []
    for fr in page.frames:
        try:
            parts.append(fr.inner_text("body", timeout=3000))
        except Exception:
            pass
    return "\n".join(parts)


ENGINE = "https://booking.sanglier-durbuy.be/#!/language/nl"
ENGINE_DATES = "https://booking.sanglier-durbuy.be/#!/dates"


def click_day(page, day: int, month_label: str = "November 2026") -> bool:
    """Klikt op een dag in de kalender van de november-maand (niet de grijze dagen van okt/dec)."""
    js = """([day, label]) => {
        const txt = el => (el.innerText || '').trim();
        // zoek de kalender met het juiste maandlabel
        let root = document;
        for (const el of document.querySelectorAll('*')) {
            if (el.children.length === 0 && txt(el) === label) {
                let p = el; for (let i = 0; i < 6 && p.parentElement; i++) p = p.parentElement;
                root = p; break;
            }
        }
        const cells = [...root.querySelectorAll('td, button, a, span, div')]
            .filter(e => txt(e) === String(day).padStart(2, '0') || txt(e) === String(day));
        // neem de cel na de eerste 1 van de maand (dus in november zelf)
        const all = [...root.querySelectorAll('td')].map(txt);
        const firstIdx = all.findIndex(t => t === '01' || t === '1');
        const tds = [...root.querySelectorAll('td')];
        let target = null;
        for (let i = Math.max(firstIdx, 0); i < tds.length; i++) {
            const t = txt(tds[i]);
            if (t === String(day).padStart(2, '0') || t === String(day)) { target = tds[i]; break; }
        }
        if (!target && cells.length) target = cells[cells.length > 1 ? 1 : 0];
        if (!target) return 'not found';
        const clickable = target.querySelector('a, button, span, div') || target;
        clickable.click();
        return 'clicked: ' + target.className + ' | ' + target.outerHTML.slice(0, 300);
    }"""
    try:
        res = page.evaluate(js, [day, month_label])
        log(f"klik dag {day}: {res}")
        return str(res).startswith("clicked")
    except Exception as e:
        log(f"klik dag {day} fout: {e}")
        return False


DAY_CLASSES_JS = """() => {
    const tds = [...document.querySelectorAll('td.uib-day')];
    const out = []; let inMonth = false;
    for (const td of tds) {
        const t = (td.innerText || '').trim();
        if (t === '01' || t === '1') inMonth = !inMonth;   // 1e keer: begin maand, 2e keer: volgende maand
        if (inMonth) out.push([parseInt(t, 10), td.className]);
    }
    const label = [...document.querySelectorAll('strong, button, th')]
        .map(e => (e.innerText || '').trim()).find(x => /\\d{4}/.test(x)) || '';
    return {label, days: out};
}"""


def check_cubilis(page, shot=None) -> str:
    """Eigen boekingsmodule van het hotel (booking.sanglier-durbuy.be).

    De kalender geeft elke dag CSS-klassen mee:
      'start-day' = je kunt op die dag aankomen
      'last'      = je kunt op die dag vertrekken
    Beschikbaar voor 28->29 nov = 28 heeft 'start-day' en 29 heeft 'last'.
    """
    status = "unknown"
    try:
        page.goto(ENGINE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        accept_cookies(page)
        if "dates" not in page.url:
            page.goto(ENGINE_DATES, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("td.uib-day", timeout=30000)
        page.wait_for_timeout(3000)
        info = page.evaluate(DAY_CLASSES_JS)
        label = info.get("label", "")
        days = {d: c for d, c in info.get("days", [])}
        c_in, c_out = days.get(d_in.day, ""), days.get(d_out.day, "")
        log(f"kalender '{label}': {d_in.day} nov = [{c_in}] | {d_out.day} nov = [{c_out}]")
        if "nov" not in label.lower() and label:
            log("let op: kalender staat niet op november")
        if c_in and c_out:
            can_arrive = "start-day" in c_in.split()
            can_leave = "last" in c_out.split()
            status = "available" if (can_arrive and can_leave) else "unavailable"
        if shot:
            page.screenshot(path="test_hotel_kalender.png", full_page=True)
    except Exception as e:
        log(f"hotelmodule fout: {e}")
    if shot:
        with open("test_log.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(DEBUG_LOG))
    return status


# ---------- Booking.com ----------
def check_booking(page, shot=None) -> str:
    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    accept_cookies(page)
    if shot:
        page.screenshot(path=shot, full_page=False)
    if page.locator("#hprt-table .hprt-table-cell-price, select.hprt-nos-select").count() > 0:
        return "available"
    t = page.inner_text("body").lower()
    if any(p in t for p in NO_AVAIL):
        return "unavailable"
    return "unknown"


SOURCES = [
    ("Hotelwebsite", check_cubilis, ENGINE_DATES, "test_cubilis.png"),
    ("Booking.com", check_booking, BOOKING_URL, "test_booking.png"),
]


def run_once(page) -> None:
    """Eén controle-ronde (voor GitHub Actions, elke ~15 min)."""
    now = datetime.utcnow()
    if now > STOP_AT:
        print("Datum voorbij, niets te doen.")
        return
    summary = []
    for name, fn, url, _ in SOURCES:
        try:
            st = fn(page)
        except Exception as e:
            st = "unknown"
            print(f"  {name} fout:", e)
        print(f"{name}: {st}")
        summary.append(f"{name}: {st}")
        if st == "available":
            notify(f"KAMER BESCHIKBAAR via {name}!",
                   f"Le Sanglier des Ardennes {CHECKIN} -> {CHECKOUT}. Tik om te boeken.",
                   url=url)
    # Dagelijks statusbericht rond 08:00 NL-tijd, zodat je weet dat het draait
    if now.hour == 7 and now.minute < 15:
        notify("Monitor draait nog", " | ".join(summary), priority="low")


def main() -> None:
    test = "--test" in sys.argv
    once = "--once" in sys.argv
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="nl-NL",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        )
        page = ctx.new_page()

        if once:
            run_once(page)
            browser.close()
            return

        if test:
            for name, fn, _, shot in SOURCES:
                try:
                    st = fn(page, shot=shot)
                except Exception as e:
                    st = f"fout: {e}"
                print(f"{name}: {st}   (screenshot: {shot})")
            if not os.environ.get("SKIP_TEST_NOTIFY"):
                notify("Monitor-test", "Testmelding: meldingen werken.", priority="low")
            browser.close()
            return

        notify("Monitor gestart",
               f"Ik controleer hotelwebsite + Booking.com voor {CHECKIN} -> {CHECKOUT}.",
               priority="low")
        unknown = {name: 0 for name, *_ in SOURCES}
        last_alert = {name: 0.0 for name, *_ in SOURCES}

        while datetime.now() < STOP_AT:
            for name, fn, url, _ in SOURCES:
                try:
                    st = fn(page)
                except Exception as e:
                    st = "unknown"
                    print(f"  {name} fout:", e)
                print(f"[{datetime.now():%d-%m %H:%M}] {name}: {st}")

                if st == "available":
                    if time.time() - last_alert[name] > 30 * 60:   # max 1x per half uur
                        notify(f"KAMER BESCHIKBAAR via {name}!",
                               f"Le Sanglier des Ardennes {CHECKIN} -> {CHECKOUT}. "
                               "Tik om te boeken.", url=url)
                        last_alert[name] = time.time()
                    unknown[name] = 0
                    continue

                unknown[name] = unknown[name] + 1 if st == "unknown" else 0
                if unknown[name] == 6:
                    notify(f"Monitor: check {name} handmatig",
                           "Pagina kon 6x niet goed gelezen worden (captcha of andere opmaak).",
                           url=url, priority="default")

            time.sleep(INTERVAL_MIN * 60 + random.randint(0, 120))
        browser.close()


if __name__ == "__main__":
    main()
