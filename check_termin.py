"""Heilbronn Ausländerbehörde - randevu takip botu (hızlı sürüm).

etermin.net'teki "issue of a residence permit" (ilk oturum izni) hizmeti için
EN ERKEN boş günü kontrol eder. Daha erken bir gün açıldığında (ör. biri
randevusunu iptal ettiğinde) Telegram'dan, o günün saatleriyle birlikte
mesaj atar.

GitHub her 5 dakikada bir çalıştırır; her çalıştırmada CHECKS_PER_RUN kez,
CHECK_INTERVAL_SEC saniye arayla kontrol eder (varsayılan: 4 kez, dakikada bir).

Ortam değişkenleri (GitHub Secrets / Variables):
  TELEGRAM_BOT_TOKEN  (zorunlu) - @BotFather'dan alınan token
  TELEGRAM_CHAT_ID    (zorunlu) - mesajın gideceği chat id
  MAX_DATE            (isteğe bağlı) - YYYY-MM-DD. Sadece bu tarih ve
                      öncesindeki günler için haber verir.
  TEST_MODE           (isteğe bağlı) - "true" ise tek kontrol yapar ve mevcut
                      durumu her halükârda Telegram'a yollar (kurulum testi).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

WEBID = "qtermin-stadtheilbronn-abh"
SERVICE_ID = 144511       # "issue of a residence permit"
DURATION_MIN = 20         # bu hizmetin süresi (1 kişi x 20 dk)
# Bu link takvimi doğrudan bu hizmet seçili olarak açar:
BOOKING_URL = f"https://www.etermin.net/{WEBID}/serviceid/{SERVICE_ID}?lang=en"
STATE_FILE = "state/state.json"
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "4"))
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
FAILS_BEFORE_WARNING = 10  # art arda kaç başarısız kontrolden sonra uyarı atılsın

GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Parametreler sitenin kendi sayfasının gönderdiği istekten kopyalandı.
COMMON_PARAMS = {
    "serviceid": SERVICE_ID,
    "capacity": 1,
    "caching": "false",
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


def api_get(extra_params):
    params = dict(COMMON_PARAMS, **extra_params)
    # quote_via=quote: boşlukları sitenin kendisi gibi %20 olarak kodlar
    url = "https://www.etermin.net/api/timeslots?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )
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
    return data


def fetch_available_days():
    """Bugünden itibaren ilk boş günü ve o ayın kalan boş günlerini döndürür.

    'rangesearch' sorgusu verilen tarihten ileriye bakar, ilk boş günü bulur
    ve o günden ay sonuna kadarki boş günleri listeler. Yani listenin ilk
    elemanı her zaman EN ERKEN boş gündür.
    """
    data = api_get({"date": date.today().isoformat(), "rangesearch": 1})
    return sorted({d["start"][:10] for d in data if d.get("available", 0) > 0})


def fetch_times(day):
    """Verilen gündeki boş saatleri (ör. ['08:30', '08:50']) döndürür."""
    data = api_get(
        {
            "date": day,
            "tz": "W. Europe Standard Time",
            "tzaccount": "W. Europe Standard Time",
        }
    )
    return sorted({d["start"][11:16] for d in data if d.get("start", "")[:10] == day})


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


def times_text(day):
    try:
        times = fetch_times(day)
    except Exception as e:  # saatler alınamazsa bildirim yine gitsin
        print(f"Saatler alınamadı: {e}")
        return ""
    if not times:
        return ""
    shown = ", ".join(times[:8])
    more = f" (+{len(times) - 8} saat daha)" if len(times) > 8 else ""
    return f"Saatler: {shown}{more}\n"


def check_once(state, first_run, test_mode, max_date):
    """Tek bir kontrol yapar, gerekirse mesaj atar, state'i günceller."""
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
        return

    if state.get("fail_count", 0) >= FAILS_BEFORE_WARNING:
        send_telegram("✅ Randevu botu tekrar normal çalışıyor.")
    state["fail_count"] = 0

    earliest = days[0] if days else None
    prev_earliest = state.get("earliest")
    print(f"En erken boş gün: {earliest} (önceki: {prev_earliest})")

    if test_mode:
        send_telegram(
            "✅ Randevu botu çalışıyor! (hızlı sürüm)\n\n"
            f"Şu an en erken boş gün: {tr_date(earliest) if earliest else 'yok'}\n"
            + (times_text(earliest) if earliest else "")
            + f"Tarih filtresi: {max_date or 'yok'}\n\n"
            "Daha erken bir gün açılınca sana hemen yazacağım.\n"
            f"{BOOKING_URL}"
        )
    elif first_run:
        print("İlk çalıştırma: en erken gün kaydedildi, mesaj atılmadı.")
    elif earliest and (prev_earliest is None or earliest < prev_earliest):
        if not max_date or earliest <= max_date:
            send_telegram(
                "📅 DAHA ERKEN RANDEVU AÇILDI!\n\n"
                f"Gün: {tr_date(earliest)}\n"
                + times_text(earliest)
                + (f"(önceki en erken: {tr_date(prev_earliest)})\n" if prev_earliest else "")
                + f"\nHemen al 👉 {BOOKING_URL}\n"
                "(Link takvimi doğrudan açar: saati seç → Continue)"
            )
            print("Bildirim gönderildi.")

    state["earliest"] = earliest


def main():
    max_date = os.environ.get("MAX_DATE", "").strip()
    test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"

    prev = load_state()
    first_run = prev is None
    state = dict(prev) if prev else {"earliest": None, "fail_count": 0}

    checks = 1 if test_mode else max(1, CHECKS_PER_RUN)
    for i in range(checks):
        if i > 0:
            time.sleep(CHECK_INTERVAL_SEC)
        check_once(state, first_run and i == 0, test_mode, max_date)
        save_state(state)


if __name__ == "__main__":
    main()
