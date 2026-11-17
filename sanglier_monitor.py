"""
Kamer-monitor: Hotel Le Sanglier des Ardennes (Durbuy).

Controleert per gewenst verblijf (zie STAYS):
  1. de eigen boekingssite van het hotel (booking.sanglier-durbuy.be)
  2. Booking.com
en stuurt een pushmelding naar je telefoon (gratis app "ntfy") zodra er
ergens een kamer beschikbaar is.

Draait normaal via GitHub Actions (elke ~15 min, zie .github/workflows/check.yml).

Lokaal:
    pip install playwright requests
    playwright install chromium
    python sanglier_monitor.py --test    # één controle, met screenshots
    python sanglier_monitor.py --once    # één controle (zoals GitHub doet)
    python sanglier_monitor.py           # blijft draaien, elke ~10 min
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
STAYS = [                      # (aankomst, vertrek)
    ("2026-11-28", "2026-11-29"),
    ("2026-11-27", "2026-11-29"),
]
ADULTS = 2
ROOMS = 1
INTERVAL_MIN = 10
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sanglier-kamer-VERANDER-MIJ-8372")
# -----------------------------------

ENGINE = "https://booking.sanglier-durbuy.be/#!/language/nl"
ENGINE_DATES = "https://booking.sanglier-durbuy.be/#!/dates"
MONTHS_NL = ["", "jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

NO_AVAIL = [
    "geen beschikbaarheid", "niet beschikbaar", "geen kamers beschikbaar",
    "volzet", "volgeboekt", "uitverkocht",
    "aucune disponibilité", "pas de disponibilité", "complet", "indisponible",
    "no availability", "not available", "no rooms available", "sold out",
]

DEBUG_LOG: list = []


def log(msg: str) -> None:
    DEBUG_LOG.append(msg)
    print("  " + msg)


def d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def stay_label(ci: str, co: str) -> str:
    a, b = d(ci), d(co)
    return f"{a.day}->{b.day} {MONTHS_NL[b.month]}"


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
    for sel in ("#onetrust-accept-btn-handler", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                "button:has-text('Alles toestaan')", "button:has-text('Accepteren')",
                "button:has-text('Alles accepteren')", "button:has-text('Accept')"):
        try:
            page.click(sel, timeout=800)
            return
        except Exception:
            pass


# ---------- Eigen boekingssite van het hotel ----------
# Kalender-dagen hebben CSS-klassen: 'start-day' = aankomst mogelijk,
# 'last' = vertrek mogelijk, 'disabled'/'unavailable' = niet te kiezen.
CAL_JS = """() => {
    const tds = [...document.querySelectorAll('td.uib-day')];
    const out = []; let inMonth = false;
    tds.forEach((td, i) => {
        const t = (td.innerText || '').trim();
        if (t === '01' || t === '1') inMonth = !inMonth;
        if (inMonth) out.push([parseInt(t, 10), td.className, i]);
    });
    const label = [...document.querySelectorAll('strong, button, th')]
        .map(e => (e.innerText || '').trim()).find(x => /\\d{4}/.test(x)) || '';
    return {label, days: out};
}"""

CLICK_JS = """(idx) => {
    const td = document.querySelectorAll('td.uib-day')[idx];
    const b = td.querySelector('button') || td;
    b.click();
    return td.className;
}"""


def click_cell(page, idx: int) -> None:
    """Echte muisklik op een kalenderdag."""
    cell = page.locator("td.uib-day").nth(idx)
    btn = cell.locator("button")
    target = btn.first if btn.count() else cell
    target.scroll_into_view_if_needed()
    target.click()


def read_calendar(page) -> dict:
    info = page.evaluate(CAL_JS)
    return {"label": info.get("label", ""),
            "days": {day: (cls, idx) for day, cls, idx in info.get("days", [])}}


def open_calendar(page) -> None:
    page.goto(ENGINE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    accept_cookies(page)
    if "dates" not in page.url:
        page.goto(ENGINE_DATES, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector("td.uib-day", timeout=30000)
    page.wait_for_timeout(2500)


def check_hotel(page, ci: str, co: str, shot: str = "") -> str:
    a, b = d(ci), d(co)
    name = stay_label(ci, co)
    try:
        open_calendar(page)
        cal = read_calendar(page)
        if "nov" not in cal["label"].lower():
            log(f"[hotel {name}] kalender staat op '{cal['label']}', verwacht november")
            return "unknown"
        if a.day not in cal["days"] or b.day not in cal["days"]:
            log(f"[hotel {name}] dagen niet gevonden in kalender")
            return "unknown"
        cls_in, idx_in = cal["days"][a.day]
        log(f"[hotel {name}] aankomst {a.day}: [{cls_in}]")
        if "start-day" not in cls_in.split():
            return "unavailable"

        # Aankomstdag aanklikken; de kalender toont dan de mogelijke vertrekdagen
        click_cell(page, idx_in)
        page.wait_for_timeout(2000)
        if shot:
            page.screenshot(path=f"{shot}_na_aankomst.png", full_page=True)
        cal2 = read_calendar(page)
        cls_out, idx_out = cal2["days"].get(b.day, ("", -1))
        log(f"[hotel {name}] na klik aankomst, vertrek {b.day}: [{cls_out}]")
        if idx_out < 0:
            return "unknown"
        # Na de aankomstklik kiest de kalender zelf 1 nacht; anders vertrekdag aanklikken.
        if "last-selected" not in cls_out.split():
            try:
                click_cell(page, idx_out)
            except Exception as e:
                log(f"[hotel {name}] vertrekdag niet klikbaar: {str(e)[:120]}")
                return "unavailable"
            page.wait_for_timeout(2000)
        cal3 = read_calendar(page)
        c_in3 = cal3["days"].get(a.day, ("", -1))[0].split()
        c_out3 = cal3["days"].get(b.day, ("", -1))[0].split()
        log(f"[hotel {name}] na selectie: aankomst [{' '.join(c_in3)}] | vertrek [{' '.join(c_out3)}]")
        if shot:
            page.screenshot(path=f"{shot}_geselecteerd.png", full_page=True)
        # Geldig verblijf = vertrekdag is gemarkeerd als laatste dag van de selectie.
        # (Is de klik op de vertrekdag een nieuwe aankomst geworden, dan is dit niet zo.)
        if "last-selected" not in c_out3:
            return "unavailable"
        try:
            page.click("text=Verder", timeout=3000)
        except Exception:
            log(f"[hotel {name}] knop 'Verder' niet gevonden")
        page.wait_for_timeout(9000)
        text = page.inner_text("body")
        low = text.lower()
        if shot:
            page.screenshot(path=f"{shot}_resultaat.png", full_page=True)
            with open(f"{shot}_resultaat.txt", "w", encoding="utf-8") as f:
                f.write(page.url + "\n\n" + text[:8000])
        log(f"[hotel {name}] na Verder: {page.url}")
        if "#!/dates" in page.url:
            log(f"[hotel {name}] wel geselecteerd maar niet doorgegaan naar stap 2")
            return "unknown"
        if any(p in low for p in NO_AVAIL) and not re.search(r"€\s?\d|\d[.,]\d{2}\s?€", text):
            return "unavailable"
        return "available"
    except Exception as e:
        log(f"[hotel {name}] fout: {e}")
        return "unknown"


# ---------- Booking.com ----------
def booking_url(ci: str, co: str) -> str:
    return ("https://www.booking.com/hotel/be/sanglier-des-ardennes.html"
            f"?checkin={ci}&checkout={co}&group_adults={ADULTS}&no_rooms={ROOMS}"
            "&group_children=0&selected_currency=EUR&lang=nl")


def check_booking(page, ci: str, co: str, shot: str = "") -> str:
    name = stay_label(ci, co)
    try:
        page.goto(booking_url(ci, co), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        accept_cookies(page)
        if shot:
            page.screenshot(path=f"{shot}.png")
        if page.locator("#hprt-table .hprt-table-cell-price, select.hprt-nos-select").count() > 0:
            return "available"
        low = page.inner_text("body").lower()
        if any(p in low for p in NO_AVAIL):
            return "unavailable"
        log(f"[booking {name}] geen prijzen en geen 'niet beschikbaar'-tekst gevonden")
        return "unknown"
    except Exception as e:
        log(f"[booking {name}] fout: {e}")
        return "unknown"


# ---------- uitvoeren ----------
def active_stays() -> list:
    now = datetime.now()
    return [(ci, co) for ci, co in STAYS if now < d(ci).replace(hour=14)]


def all_checks(page, test: bool = False) -> list:
    """Geeft lijst van (bron, verblijf, status, url)."""
    results = []
    for ci, co in active_stays():
        tag = f"{d(ci).day}-{d(co).day}"
        st = check_hotel(page, ci, co, shot=f"test_hotel_{tag}" if test else "")
        results.append(("Hotelwebsite", stay_label(ci, co), st, ENGINE_DATES))
        st = check_booking(page, ci, co, shot=f"test_booking_{tag}" if test else "")
        results.append(("Booking.com", stay_label(ci, co), st, booking_url(ci, co)))
    return results


def report(results: list) -> None:
    for src, stay, st, url in results:
        print(f"{src} {stay}: {st}")
        if st == "available":
            notify(f"KAMER BESCHIKBAAR {stay} via {src}!",
                   f"Le Sanglier des Ardennes, {stay}. Tik om te boeken.", url=url)


def new_browser(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        locale="nl-NL",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    )
    return browser, ctx.new_page()


def main() -> None:
    test = "--test" in sys.argv
    once = "--once" in sys.argv
    if not active_stays():
        print("Alle datums zijn voorbij, niets te doen.")
        return
    with sync_playwright() as p:
        browser, page = new_browser(p)
        try:
            if test or once:
                results = all_checks(page, test=test)
                report(results)
                summary = " | ".join(f"{s} {v}: {st}" for s, v, st, _ in results)
                if test:
                    # controle: een verblijf dat volgens de kalender wel kan (vr 27 -> za 28)
                    st = check_hotel(page, "2026-11-27", "2026-11-28", shot="test_controle_27-28")
                    summary += f" | CONTROLE Hotelwebsite 27->28: {st}"
                    with open("test_log.txt", "w", encoding="utf-8") as f:
                        f.write("\n".join(DEBUG_LOG) + "\n\n" + summary)
                    if not os.environ.get("SKIP_TEST_NOTIFY"):
                        notify("Monitor-test", "Testmelding: meldingen werken.", priority="low")
                now = datetime.utcnow()
                if once and now.hour == 7 and now.minute < 15:   # ~08:00 NL-tijd
                    notify("Monitor draait nog", summary, priority="low")
                return

            notify("Monitor gestart", "Ik controleer hotelwebsite + Booking.com.", priority="low")
            while active_stays():
                report(all_checks(page))
                time.sleep(INTERVAL_MIN * 60 + random.randint(0, 120))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
