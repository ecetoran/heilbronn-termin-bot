"""Heilbronn Ausländerbehörde - randevu takip botu.

etermin.net'teki "issue of a residence permit" (ilk oturum izni) hizmeti için
EN ERKEN boş günü kontrol eder. Daha erken bir gün açıldığında (ör. biri
randevusunu iptal ettiğinde) Telegram'dan mesaj atar.

Ortam değişkenleri (GitHub Secrets / Variables):
  TELEGRAM_BOT_TOKEN  (zorunlu) - @BotFather'dan alınan token
  TELEGRAM_CHAT_ID    (zorunlu) - mesajın gideceği chat id
  MAX_DATE            (isteğe bağlı) - YYYY-MM-DD. Sadece bu tarih ve
                      öncesindeki günler için haber verir.
  TEST_MODE           (isteğe bağlı) - "true" ise mevcut durumu her halükârda
                      Telegram'a yollar (kurulum testi).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

WEBID = "qtermin-stadtheilbronn-abh"
SERVICE_ID = 144511       # "issue of a residence permit"
DURATION_MIN = 20         # bu hizmetin süresi (1 kişi x 20 dk)
BOOKING_URL = "https://www.etermin.net/qtermin-stadtheilbronn-abh?lang=en"
STATE_FILE = "state/state.json"
FAILS_BEFORE_WARNING = 3  # art arda kaç hatadan sonra uyarı atılsın

GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def fetch_available_days():
    """Bugünden itibaren ilk boş günü ve o ayın kalan boş günlerini döndürür.

    Sitenin 'rangesearch' sorgusu verilen tarihten ileriye bakar, ilk boş
    günü bulur ve o günden ay sonuna kadarki boş günleri listeler. Yani
    listenin ilk elemanı her zaman EN ERKEN boş gündür.
    Parametreler sitenin kendi sayfasının gönderdiği istekten kopyalandı.
    """
    params = {
        "date": date.today().isoformat(),
        "serviceid": SERVICE_ID,
        "rangesearch": 1,
        "caching": "false",
        "capacity": 1,
        "duration": DURATION_MIN,
        "cluster": "false",
        "slottype": 0,
        "fillcalendarstrategy": 0,
        "showavcap": "false",
        "appfuture": 270,
        "appdeadline": 480,
        "appdeadlinewm": 1,
        "oneoff": "null",
        "msdcm": 0,
        "calendarid": "",
    }
    url = "https://www.etermin.net/api/timeslots?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "webid": WEBID,
            "Accept": "application/json",
            "Referer": BOOKING_URL,
            "User-Agent": "Mozilla/5.0 (compatible; personal-appointment-reminder)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Beklenmeyen cevap: {str(data)[:200]}")
    return sorted({d["start"][:10] for d in data if d.get("available", 0) > 0})


def tr_date(iso):
    d = date.fromisoformat(iso)
    return f"{GUNLER[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik. "
            "GitHub > Settings > Secrets and variables > Actions kısmını kontrol et."
        )
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Telegram mesajı gönderilemedi (HTTP {e.code}). Token veya chat id "
            "yanlış olabilir, ya da botuna henüz Start'a basmamış olabilirsin."
        ) from None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # ilk çalıştırma


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    max_date = os.environ.get("MAX_DATE", "").strip()
    test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"

    prev = load_state()
    state = dict(prev) if prev else {"earliest": None, "fail_count": 0}

    # 1) Siteyi oku
    try:
        days = fetch_available_days()
    except Exception as e:
        print(f"Siteden okuma hatası: {e}")
        if test_mode:
            send_telegram(f"⚠️ Randevu botu siteyi okuyamadı:\n{e}")
            sys.exit(1)
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == FAILS_BEFORE_WARNING:
            send_telegram(
                "⚠️ Randevu botu bir süredir siteyi okuyamıyor. Şehir sistemi "
                "değişmiş olabilir. Bu sırada randevulara elle bakmanı öneririm:\n"
                f"{BOOKING_URL}"
            )
        save_state(state)
        return  # GitHub'dan her 10 dakikada hata e-postası gelmesin diye

    if state.get("fail_count", 0) >= FAILS_BEFORE_WARNING:
        send_telegram("✅ Randevu botu tekrar normal çalışıyor.")
    state["fail_count"] = 0

    earliest = days[0] if days else None
    prev_earliest = prev.get("earliest") if prev else None
    print(f"En erken boş gün: {earliest} (önceki kontrol: {prev_earliest})")
    print(f"Bu aydaki boş günler: {days}")

    # 2) Haber verilecek mi?
    if test_mode:
        send_telegram(
            "✅ Randevu botu çalışıyor!\n\n"
            f"Şu an en erken boş gün: {tr_date(earliest) if earliest else 'yok'}\n"
            f"Tarih filtresi: {max_date or 'yok'}\n\n"
            "Bundan sonra daha erken bir gün açılınca sana yazacağım.\n"
            f"{BOOKING_URL}"
        )
    elif prev is None:
        print("İlk çalıştırma: en erken gün kaydedildi, mesaj atılmadı.")
    elif earliest and (prev_earliest is None or earliest < prev_earliest):
        if not max_date or earliest <= max_date:
            others = [date.fromisoformat(d).strftime("%d.%m") for d in days[1:6]]
            send_telegram(
                "📅 DAHA ERKEN RANDEVU AÇILDI!\n\n"
                f"En erken gün: {tr_date(earliest)}\n"
                + (f"(önceki en erken: {tr_date(prev_earliest)})\n" if prev_earliest else "")
                + (f"Aynı aydaki diğer günler: {', '.join(others)}\n" if others else "")
                + f"\nHemen al 👉 {BOOKING_URL}\n"
                "residence permit > issue of a residence permit > +"
            )
            print("Bildirim gönderildi.")

    state["earliest"] = earliest
    save_state(state)


if __name__ == "__main__":
    main()
