import json
import os
import re
import logging
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── TOKEN ─────────────────────────────────────────────────────────────────────
TOKEN = "8726201756:AAEzhCk2bkJBnzvDbQrLv4mYD_k13_tBbic"  # BotFather-dan aldığınız tokeni buraya yazın

# ── LOG FILE ──────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), "greenhouse_log.json")

# ── CONVERSATION STATES ───────────────────────────────────────────────────────
(
    DATA_VAXTI,
    TARIX, ISTIXANA, HAVA,
    TEMP_07, TEMP_11, TEMP_14, TEMP_18,
    TORPAQ_57, TORPAQ_1012,
    BITKI, SON_SULAMA, SON_GUBRE, MEHSUL, PROBLEM,
) = range(15)


# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_entry(data: dict):
    log = load_log()
    log.append(data)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def parse_temp_humidity(text: str):
    """Extract first two numbers from a string (temp, humidity)."""
    numbers = re.findall(r"\d+\.?\d*", text)
    if len(numbers) >= 2:
        return float(numbers[0]), float(numbers[1])
    return None, None


# ── DECISION ENGINE ───────────────────────────────────────────────────────────

def generate_decision(data: dict) -> str:
    data_vaxti  = data.get("data_vaxti", "səhər").lower().strip()
    torpaq_57   = data.get("torpaq_57",   "").lower()
    torpaq_1012 = data.get("torpaq_1012", "").lower()
    bitki       = data.get("bitki",       "").lower()
    son_gubre   = data.get("son_gubre",   "").lower()
    problem     = data.get("problem",     "").lower()

    # Soil moisture booleans
    s57_dry   = any(w in torpaq_57   for w in ["quru"])
    s57_moist = any(w in torpaq_57   for w in ["nəm", "nem"])
    s57_wet   = any(w in torpaq_57   for w in ["yaş", "yas"])

    s1012_dry   = any(w in torpaq_1012 for w in ["quru"])
    s1012_moist = any(w in torpaq_1012 for w in ["nəm", "nem"])
    s1012_wet   = any(w in torpaq_1012 for w in ["yaş", "yas"])

    s1012_has_water = s1012_moist or s1012_wet
    s57_has_water   = s57_moist or s57_wet
    both_dry        = s57_dry and s1012_dry
    both_moist      = s57_has_water and s1012_has_water

    wilting = any(w in bitki for w in ["solux", "solğun", "solgun"])

    # Per-reading climate flags: check both temp AND humidity in the SAME reading
    critical_climate = False  # any single reading: temp > 30 AND hum < 50
    hot_dry          = False  # any single reading: temp >= 28 AND hum < 50 (but not critical)
    readings = []
    for key in ["temp_07", "temp_11", "temp_14", "temp_18"]:
        t, h = parse_temp_humidity(data.get(key, ""))
        if t is not None and h is not None:
            readings.append((t, h))
            if t > 30 and h < 50:
                critical_climate = True
            elif t >= 28 and h < 50 and not critical_climate:
                hot_dry = True

    # Re-evaluate hot_dry: if critical_climate was set AFTER a hot_dry reading, clear hot_dry
    if critical_climate:
        hot_dry = False

    max_temp = max((t for t, h in readings), default=0.0)
    min_hum  = min((h for t, h in readings), default=100.0)

    # Fertilizer timing — text checks FIRST, then numeric
    gubre_days = None
    sg_lower = son_gubre.lower()
    if any(x in sg_lower for x in ["bu gün", "bugün"]):
        gubre_days = 0
    elif "dünən" in sg_lower:
        gubre_days = 1
    elif "1 gün" in sg_lower:
        gubre_days = 1
    elif "2 gün" in sg_lower:
        gubre_days = 2
    else:
        nums = re.findall(r"\d+", son_gubre)
        if nums:
            gubre_days = int(nums[0])

    salinity = any(w in problem for w in ["duz", "salinit", "tuzlu", "duzlu"])

    # ── TEMPERATURE ADJUSTMENT MULTIPLIER ─────────────────────────────────────
    if max_temp < 20:
        temp_mult = 0.80
        temp_note = f"(temp {max_temp}°C < 20°C: həcm 20% azaldılıb)"
    elif max_temp <= 28:
        temp_mult = 1.00
        temp_note = ""
    elif max_temp <= 33:
        temp_mult = 1.15
        temp_note = f"(temp {max_temp}°C, 28-33°C: həcm 15% artırılıb)"
    else:
        temp_mult = 1.25
        temp_note = f"(temp {max_temp}°C > 33°C: həcm 25% artırılıb + çiləmə mütləqdir)"

    def adj(base_low, base_high):
        lo = round(base_low * temp_mult, 1)
        hi = round(base_high * temp_mult, 1)
        note = f" {temp_note}" if temp_note else ""
        return f"{lo}–{hi} L/bitki{note}"

    # ── STRESS ANALYSIS ───────────────────────────────────────────────────────
    stress_note      = ""
    wilting_override = None
    if wilting:
        if both_moist:
            stress_note      = "KÖK BOĞULMASI şübhəsi: hər iki qat nəmdir, bitki soluxur — sulamani DAYANDIR, havalandırma artır, kökləri yoxla."
            wilting_override = "stop"
        elif s1012_has_water:
            stress_note      = "Günorta soluxma + dərin torpaq nəmdir: problem havadadır. Sulama VERMƏYİN — yalnız çiləmə edin."
            wilting_override = "misting_only"
        elif s1012_dry:
            stress_note      = "Bitki soluxur + dərin torpaq qurudur: həqiqi su çatışmazlığı — dərhal sulama lazımdır (0.3–0.5 L)."
            wilting_override = "irrigate"

    # ── MORNING IRRIGATION ────────────────────────────────────────────────────
    if wilting_override == "stop":
        morning = (
            "→ Sulama: DAYANDIRIN\n"
            "   → Hər iki qat nəmdir, bitki soluxur — kök boğulması şübhəsi."
        )
    elif both_dry:
        morning = (
            f"→ Sulama: BƏLİ\n"
            f"   → Həcm: {adj(0.5, 0.6)}\n"
            "   → Hər iki qat qurudur — tam sulama lazımdır."
        )
    elif s57_dry and s1012_has_water:
        morning = (
            f"→ Sulama: BƏLİ (yüngül)\n"
            f"   → Həcm: {adj(0.3, 0.4)}\n"
            "   → 10-12 sm nəmdir — yalnız üst qat nəmləndirilir."
        )
    elif s57_has_water and s1012_dry:
        morning = (
            f"→ Sulama: BƏLİ (dərinə nəmləndirmə)\n"
            f"   → Həcm: {adj(0.4, 0.5)}\n"
            "   → 10-12 sm qurudur — dərinə nəmləndirmə lazımdır."
        )
    else:
        morning = (
            "→ Sulama: LAZIM DEYİL\n"
            "   → Hər iki torpaq qatı kifayət qədər nəmdir."
        )

    # ── MIDDAY STRATEGY — always two separate sub-blocks ─────────────────────
    # YARPAQ ÇİLƏMƏSİ (misting)
    if critical_climate:
        misting_line = "YARPAQ ÇİLƏMƏSİ: BƏLİ — hər 30-45 dəqiqədən bir. (KRİTİK rejim)"
    elif max_temp > 33:
        misting_line = "YARPAQ ÇİLƏMƏSİ: BƏLİ — hər 20-30 dəqiqədən bir. (temp > 33°C)"
    elif hot_dry or wilting_override == "misting_only":
        misting_line = "YARPAQ ÇİLƏMƏSİ: BƏLİ — hər 1–1.5 saatdan bir."
    elif both_moist:
        misting_line = "YARPAQ ÇİLƏMƏSİ: LAZIM DEYİL — hər iki qat nəmdir."
    else:
        misting_line = "YARPAQ ÇİLƏMƏSİ: lazım olarsa yüngül çiləmə."

    # KÖK SULAMASI (root irrigation)
    if wilting_override == "stop":
        root_irr_line = "KÖK SULAMASI: DAYANDIRIN — kök boğulması şübhəsi."
    elif both_dry and wilting_override != "misting_only":
        root_irr_line = f"KÖK SULAMASI: QISA — {adj(0.2, 0.3)} (hər iki qat quru, maks 5 dəq)."
    else:
        root_irr_line = "KÖK SULAMASI: YOXDUR — günorta ağır sulama qadağandır."

    midday = f"→ {misting_line}\n   → {root_irr_line}"

    # ── EVENING IRRIGATION ────────────────────────────────────────────────────
    if wilting_override == "stop":
        evening = (
            "→ Sulama: DAYANDIRIN\n"
            "   → Kök boğulması şübhəsi — sulama yoxdur."
        )
    elif both_dry:
        evening = (
            f"→ Sulama: BƏLİ\n"
            f"   → Həcm: {adj(0.3, 0.4)}\n"
            "   → Gün ərzində quruluq davam edibsə sulama lazımdır."
        )
    elif s57_dry and s1012_has_water:
        evening = (
            "→ Sulama: ŞƏRTƏ BAĞLIDIR\n"
            "   → Şərt: günorta soluxma davam edibsə — 0.2–0.3 L/bitki.\n"
            "   → 10-12 sm hələ nəmdirsə — axşam sulama yoxdur."
        )
    elif s57_has_water and s1012_dry:
        evening = (
            f"→ Sulama: BƏLİ\n"
            f"   → Həcm: {adj(0.2, 0.3)}\n"
            "   → Dərin qat qurudur."
        )
    else:
        evening = (
            "→ Sulama: LAZIM DEYİL\n"
            "   → Hər iki torpaq qatı nəmdir."
        )

    # ── FERTILIZATION ─────────────────────────────────────────────────────────
    soil_too_wet = s1012_wet or s57_wet

    if both_dry:
        gubre = (
            "→ Gübrə VERMƏYİN — torpaq qurudur.\n"
            "   → Əvvəlcə sulama edin, 2-3 saat keçdikdən sonra gübrə verin."
        )
    elif soil_too_wet:
        gubre = (
            "→ Gübrə VERMƏYİN — torpaq həddindən artıq nəmdir.\n"
            "   → Gübrə kökdən yuyulacaq."
        )
    elif gubre_days is not None and gubre_days < 3:
        days_left = 3 - gubre_days
        gubre = (
            f"→ Gübrə VERMƏYİN.\n"
            f"   → Son gübrə {gubre_days} gün əvvəl verilib — minimum 3 gün gözləyin.\n"
            f"   → Hələ {days_left} gün gözləmək lazımdır."
        )
    elif gubre_days is not None and gubre_days <= 5:
        gubre = (
            f"→ Yüngül gübrə icazəlidir (yarım norma).\n"
            f"   → Son gübrədən {gubre_days} gün keçib (3–5 gün aralığı).\n"
            "   → Sulamasından 2-3 saat sonra verin."
        )
    else:
        gubre = (
            "→ Tam doza gübrə icazəlidir.\n"
            "   → Torpaq nəmdir — gübrə üçün uyğun şərait.\n"
            "   → Sulamasından 2-3 saat sonra verin."
        )
    if salinity:
        gubre += "\n   → Duzlanma riski: növbəti sulamada əvvəlcə təmiz su verin."

    # ── VENTILATION ───────────────────────────────────────────────────────────
    if critical_climate:
        hava_q = (
            "→ Havalandırma: MAKSİMUM — bütün yanlar, bütün pəncərələr TAM AÇIQ!\n"
            "→ İstixana yanlarını QƏTIYYƏN bağlamayın — istilik tələsi, bitki ölər.\n"
            f"→ KRİTİK ŞƏRAIT: temp > 30°C + rütubət < 50%.\n"
            "→ Çiləmə: hər 30-45 dəqiqədən bir — mütləqdir."
            + ("\n→ Temp > 33°C: çiləməni hər 20-30 dəqiqəyə artırın + kölgələndirmə əlavə edin." if max_temp > 33 else "")
        )
    elif hot_dry:
        hava_q = (
            "→ Havalandırma: ARTIRIŞLI REJİM — yanlar yarım açıq.\n"
            f"→ Temp {max_temp}°C + rütubət {min_hum}% — isti və quru şərait.\n"
            "→ Çiləmə: hər 1–1.5 saatdan bir tövsiyə edilir."
        )
    elif 22 <= max_temp <= 28 and 50 <= min_hum <= 70:
        hava_q = (
            "→ Havalandırma: NORMAL rejim. Əlavə tədbir tələb olunmur.\n"
            f"→ Temp: {max_temp}°C / Rütubət: {min_hum}% — optimal şərait."
        )
    elif 22 <= max_temp <= 28 and min_hum > 80:
        hava_q = (
            "→ Havalandırma: ARTIRIN — rütubət həddindən yüksəkdir.\n"
            "→ Çiləməni DAYANDIR.\n"
            f"→ Rütubət: {min_hum}% > 80% — göbələk riski (Botrytis, Mildew)!\n"
            "→ Plyonka kənarlarını qaldırın."
        )
    elif max_temp < 18 and min_hum > 85:
        hava_q = (
            "→ Havalandırma: QISA MÜDDƏTLİ — 15-20 dəqiqə açın, sonra bağlayın.\n"
            f"→ Temp: {max_temp}°C aşağı + rütubət: {min_hum}% yüksək — gecə damcılanma riski.\n"
            "→ Uzun müddət açıq saxlamayın — soyuq stres yaranır."
        )
    elif max_temp < 15:
        hava_q = (
            "→ Havalandırma: BAĞLAYIN — istiliyi qoruyun.\n"
            f"→ Temp: {max_temp}°C < 15°C — soyuq şərait."
            + ("\n→ Rütubət > 90%: 10 dəq havalandırma, sonra dərhal bağlayın." if min_hum > 90 else "")
        )
    else:
        hava_q = (
            f"→ Standart havalandırma kifayətdir.\n"
            f"→ Maksimum temp: {max_temp}°C / Minimum rütubət: {min_hum}%."
        )

    # ── BIGGEST RISK ──────────────────────────────────────────────────────────
    risks = []
    if critical_climate:
        risks.append("KRİTİK: Yüksək istilik (>30°C) + aşağı rütubət (<50%) — bitki ölümü riski!")
    elif hot_dry:
        risks.append("Yüksək istilik + quru hava — bitki stressinə səbəb ola bilər.")
    if stress_note:
        risks.append(stress_note)
    if min_hum > 80 and max_temp >= 22:
        risks.append("Yüksək rütubət — göbələk riski (Botrytis, Mildew).")
    if salinity and gubre_days is not None and gubre_days < 3:
        risks.append("Duzlanma riski — gübrə əvvəl təmiz su verilməlidir.")
    if not risks:
        risks.append("Bu gün kritik risk aşkarlanmadı.")

    # ── TOMORROW CHECK ────────────────────────────────────────────────────────
    sabah = []
    if s57_dry:
        sabah.append("- Torpaq 5-7 sm sulama sonrası yaxşılaşıbmı?")
    if wilting:
        sabah.append("- Günorta soluxma davam edirsə, çiləmə tezliyini artırın.")
    if gubre_days is not None:
        if gubre_days < 3:
            sabah.append(f"- Gübrələmə: hələ {3 - gubre_days} gün gözləmək lazımdır.")
        elif gubre_days <= 5:
            sabah.append("- Gübrələmə: 5 gün tamam olarsa — tam doza icazəlidir.")
        else:
            sabah.append("- Gübrələmə: tam doza icazəlidir — torpaq durumuna görə qərar verin.")
    if not sabah:
        sabah.append("- Ümumi torpaq və bitki vəziyyətini yoxlayın.")

    # ── TIME-AWARE HEADER ─────────────────────────────────────────────────────
    if data_vaxti == "axşam":
        if critical_climate:
            header = "SABAH SƏHƏR ÜÇÜN QƏRAR ⚠ KRİTİK İQLİM"
        else:
            header = "SABAH SƏHƏR ÜÇÜN QƏRAR"
    elif data_vaxti == "günorta":
        if critical_climate:
            header = "BUGÜN GÜNORTADAN SONRA QƏRAR ⚠ KRİTİK İQLİM"
        else:
            header = "BUGÜN GÜNORTADAN SONRA QƏRAR"
    else:  # səhər (default)
        if critical_climate:
            header = "BUGÜNKÜ QƏRAR ⚠ KRİTİK İQLİM"
        else:
            header = "BUGÜNKÜ QƏRAR"

    # ── SECTION 1: SULAMA ─────────────────────────────────────────────────────
    if data_vaxti == "axşam":
        # Tomorrow morning irrigation based on today's soil data
        if wilting_override == "stop":
            sul_qarar = "Sabah səhər: DAYANDIRIN"
            sul_sebeb = "Bu gün hər iki torpaq qatı nəm idi, bitki soluxurdu — sabah da sulamayın, kökləri yoxlayın."
        elif both_dry:
            sul_qarar = f"Sabah səhər: BƏLİ {adj(0.5, 0.6)}"
            sul_sebeb = "Bu gün torpaq hər iki qatda quru idi — sabah səhər tam sulama lazımdır."
        elif s57_dry and s1012_has_water:
            sul_qarar = f"Sabah səhər: BƏLİ {adj(0.3, 0.4)} (yüngül)"
            sul_sebeb = "10-12 sm hələ nəmdirsə — sabah yalnız üst qat nəmləndirilir."
        elif s57_has_water and s1012_dry:
            sul_qarar = f"Sabah səhər: BƏLİ {adj(0.4, 0.5)} (dərinə)"
            sul_sebeb = "Dərin qat quru idi — sabah səhər dərinə nəmləndirmə lazımdır."
        else:
            sul_qarar = "Sabah səhər: LAZIM DEYİL (yoxlayın)"
            sul_sebeb = "Bu gün torpaq nəm idi — sabah səhər vəziyyəti yoxlayın, lazım olarsa sulayin."

    elif data_vaxti == "günorta":
        # Only afternoon + evening actions (morning already happened)
        if wilting_override == "stop":
            sul_qarar = "Günorta: DAYANDIRIN | Axşam: DAYANDIRIN"
            sul_sebeb = "Kök boğulması şübhəsi — günorta da axşam da sulama yoxdur."
        elif both_dry:
            sul_qarar = f"Günorta: çiləmə + qısa kök sul. {adj(0.2, 0.3)} | Axşam: BƏLİ {adj(0.3, 0.4)}"
            sul_sebeb = "Hər iki qat quru — günorta çiləmə + qısa kök sulaması, axşam tam sulama."
        elif s57_dry and s1012_has_water:
            sul_qarar = "Günorta: yüngül çiləmə | Axşam: şərtə bağlı 0.2–0.3 L"
            sul_sebeb = "10-12 sm nəmdir — günorta çiləmə yetər; axşam soluxma davam edərsə sulayın."
        elif s57_has_water and s1012_dry:
            sul_qarar = f"Günorta: çiləmə | Axşam: BƏLİ {adj(0.2, 0.3)}"
            sul_sebeb = "Dərin qat qurudur — günorta çiləmə, axşam dərinə sulama."
        else:
            sul_qarar = "Günorta: LAZIM DEYİL | Axşam: LAZIM DEYİL"
            sul_sebeb = "Torpaq nəmdir — günorta sulama qadağandır, axşam da lazım deyil."

    else:  # səhər (default — existing logic)
        if wilting_override == "stop":
            sul_qarar = "Səhər: DAYANDIRIN | Axşam: DAYANDIRIN"
            sul_sebeb = "Hər iki torpaq qatı nəmdir, bitki soluxur — kök boğulması şübhəsi."
        elif both_dry:
            sul_qarar = f"Səhər: BƏLİ {adj(0.5, 0.6)} | Axşam: BƏLİ {adj(0.3, 0.4)}"
            sul_sebeb = "Hər iki torpaq qatı qurudur — tam sulama lazımdır."
        elif s57_dry and s1012_has_water:
            sul_qarar = f"Səhər: BƏLİ {adj(0.3, 0.4)} (yüngül) | Axşam: şərtə bağlı 0.2–0.3 L"
            sul_sebeb = "10-12 sm nəmdir — yalnız üst qat nəmləndirilir; axşam yalnız soluxma davam edərsə."
        elif s57_has_water and s1012_dry:
            sul_qarar = f"Səhər: BƏLİ {adj(0.4, 0.5)} (dərinə) | Axşam: BƏLİ {adj(0.2, 0.3)}"
            sul_sebeb = "Üst qat nəmdir, lakin dərin qat qurudur — dərinə nəmləndirmə lazımdır."
        else:
            sul_qarar = "Səhər: LAZIM DEYİL | Axşam: LAZIM DEYİL"
            sul_sebeb = "Hər iki torpaq qatı kifayət qədər nəmdir."

    # ── SECTION 2: GÜBRƏLƏMƏ ──────────────────────────────────────────────────
    # Timing phrase adapts to data_vaxti
    if data_vaxti == "axşam":
        gubre_timing = "Sabah səhər sulamasından 2-3 saat sonra verin."
    elif data_vaxti == "günorta":
        gubre_timing = "Sulamadan 2-3 saat sonra verin."
    else:
        gubre_timing = "Səhər sulamasından 2-3 saat sonra verin."

    if both_dry:
        gubre_qarar = "VERMƏYİN"
        gubre_sebeb = "Torpaq qurudur — əvvəlcə sulayın, 2-3 saat sonra gübrə verin."
    elif soil_too_wet:
        gubre_qarar = "VERMƏYİN"
        gubre_sebeb = "Torpaq həddindən artıq nəmdir — gübrə kökdən yuyulacaq."
    elif gubre_days is not None and gubre_days < 3:
        gubre_qarar = "VERMƏYİN"
        gubre_sebeb = f"Son gübrədən cəmi {gubre_days} gün keçib — minimum 3 gün gözləyin."
    elif gubre_days is not None and gubre_days <= 5:
        gubre_qarar = "Yüngül doza (yarım norma)"
        gubre_sebeb = f"Son gübrədən {gubre_days} gün keçib — 3-5 günlük aralıq üçün yarım norma icazəlidir. {gubre_timing}"
    else:
        gubre_qarar = "Tam doza"
        gubre_sebeb = f"Torpaq nəmdir və gübrə intervalı keçib — tam doza üçün uyğun şərait. {gubre_timing}"
    if salinity:
        gubre_sebeb += " Duzlanma riski: əvvəlcə təmiz su verin."

    # ── SECTION 3: HAVALANDIRMA ───────────────────────────────────────────────
    if critical_climate:
        hava_qarar = "MAKSİMUM (bütün yanlar TAM AÇIQ)"
        hava_sebeb = f"Temp > 30°C + rütubət < 50% — kritik istilik, bağlamaq qadağandır."
    elif hot_dry:
        hava_qarar = "ARTIRIŞLI (yanlar yarım açıq)"
        hava_sebeb = f"Temp {max_temp}°C + rütubət {min_hum}% — isti və quru, əlavə havalandırma lazımdır."
    elif 22 <= max_temp <= 28 and 50 <= min_hum <= 70:
        hava_qarar = "NORMAL"
        hava_sebeb = f"Temp {max_temp}°C / rütubət {min_hum}% — optimal şərait, əlavə tədbir tələb olunmur."
    elif 22 <= max_temp <= 28 and min_hum > 80:
        hava_qarar = "ARTIRIN (rütubət yüksəkdir)"
        hava_sebeb = f"Rütubət {min_hum}% > 80% — göbələk riski, çiləməni dayandırın."
    elif max_temp < 18 and min_hum > 85:
        hava_qarar = "Qısa müddətli (15-20 dəq açın, bağlayın)"
        hava_sebeb = f"Temp {max_temp}°C aşağı + rütubət {min_hum}% yüksək — gecə damcılanma riski."
    elif max_temp < 15:
        hava_qarar = "BAĞLAYIN"
        hava_sebeb = f"Temp {max_temp}°C < 15°C — istiliyi qoruyun."
    else:
        hava_qarar = "NORMAL (standart)"
        hava_sebeb = f"Temp {max_temp}°C / rütubət {min_hum}% — əlavə tədbirə ehtiyac yoxdur."

    # Add "sabah üçün" prefix to hava_sebeb when axşam mode
    if data_vaxti == "axşam":
        hava_sebeb = f"Bu axşamkı göstəricilərə görə: {hava_sebeb}"

    # ── SECTION 4: GÜNORTA STRATEGİYASI / SABAH GÜNORTA RİSK PLANI ──────────
    if data_vaxti == "axşam":
        sec4_name = "Sabah günorta risk planı"
        if critical_climate:
            sec4_qarar_line = f"{misting_line} (bu gün kritik idi — sabah da ehtiyatlı olun)"
            sec4_root_line  = f"{root_irr_line}"
            sec4_sebeb = "Axşam ölçüsünə görə kritik istilik qeydə alındı — sabah günorta eyni şərait gözlənilir, hazır olun."
        elif hot_dry:
            sec4_qarar_line = f"{misting_line} (isti-quru pattern — sabah da davam edə bilər)"
            sec4_root_line  = f"{root_irr_line}"
            sec4_sebeb = f"Axşam ölçüsünə görə temp {max_temp}°C + rütubət {min_hum}% idi — sabah günorta oxşar şərait gözlənilir."
        elif max_temp > 33:
            sec4_qarar_line = "YÜKSƏK İSTİLİK RİSKİ — çiləmə artırın"
            sec4_root_line  = "KÖK SULAMASI: günorta çəkinin"
            sec4_sebeb = f"Bu axşamkı göstəricilərə görə max temp {max_temp}°C > 33°C — sabah günorta da çiləmə artırılmalıdır."
        else:
            sec4_qarar_line = "Sabah günorta üçün xüsusi risk aşkarlanmadı"
            sec4_root_line  = "Standart rejim kifayətdir"
            sec4_sebeb = f"Bu günkü iqlimdə kritik risk yox idi (temp {max_temp}°C / rütubət {min_hum}%)."
    else:
        sec4_name       = "Günorta strategiyası"
        sec4_qarar_line = misting_line
        sec4_root_line  = root_irr_line
        if critical_climate:
            sec4_sebeb = "Kritik istilik — hər 30-45 dəqiqədən bir çiləmə mütləqdir."
        elif max_temp > 33:
            sec4_sebeb = "Temp > 33°C — çiləməni hər 20-30 dəqiqəyə artırın."
        elif hot_dry or wilting_override == "misting_only":
            sec4_sebeb = "İsti və quru hava — çiləmə bitki stresini azaldır."
        elif both_dry:
            sec4_sebeb = "Hər iki qat quru — qısa kök sulaması icazəlidir (maks 5 dəq)."
        elif both_moist:
            sec4_sebeb = "Torpaq nəmdir — günorta ağır sulama qadağandır."
        else:
            sec4_sebeb = "Günorta kök sulaması qadağandır — yalnız lazım olarsa yüngül çiləmə."

    # ── SECTION 5: ETMƏ ───────────────────────────────────────────────────────
    etme = []
    if both_moist or s1012_wet or s57_wet:
        etme.append("Torpaq nəm olduğu halda əlavə sulama etməyin — kök boğulması riski var.")
    if critical_climate or hot_dry:
        etme.append("İstixana yanlarını bağlamayın — istilik tələsi yaranır, bitki ölə bilər.")
    if max_temp > 33:
        etme.append("Günorta kök sulaması etməyin — yüksək temp altında kök yanması baş verə bilər.")
    if min_hum > 80 and max_temp >= 22:
        etme.append("Çiləməni artırmayın — rütubət artıq yüksəkdir, Botrytis riski var.")
    if wilting and both_moist:
        etme.append("Soluxmaya görə sulama etməyin — problem kökdədir, su çatışmazlığı deyil.")
    if s57_dry and not both_dry:
        etme.append("Yalnız üst torpaq quruduğuna görə həddindən artıq sulamayın — dərin qat nəmdir.")
    if salinity:
        etme.append("Gübrəni birbaşa quru torpağa verməyin — əvvəlcə təmiz su ilə nəmləndirin.")
    if not etme:
        etme.append("Günorta saatlarında (12-16) kök sulamasından çəkinin.")
        etme.append("Bitkini yoxlamadan sulamaya başlamayın.")
    etme = etme[:3]

    # ── SECTION 6: SABAH YOXLA ────────────────────────────────────────────────
    sabah_checks = sabah[:3]

    # ── ASSEMBLE OUTPUT ───────────────────────────────────────────────────────
    etme_lines  = "\n".join(f"- {w}" for w in etme)
    sabah_lines = "\n".join(sabah_checks)

    decision = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"1. Sulama:\n"
        f"   Qərar: {sul_qarar}\n"
        f"   Səbəb: {sul_sebeb}\n\n"
        f"2. Gübrələmə:\n"
        f"   Qərar: {gubre_qarar}\n"
        f"   Səbəb: {gubre_sebeb}\n\n"
        f"3. Havalandırma:\n"
        f"   Qərar: {hava_qarar}\n"
        f"   Səbəb: {hava_sebeb}\n\n"
        f"4. {sec4_name}:\n"
        f"   Qərar: {sec4_qarar_line}\n"
        f"           {sec4_root_line}\n"
        f"   Səbəb: {sec4_sebeb}\n\n"
        f"5. ETMƏ:\n"
        f"{etme_lines}\n\n"
        f"6. Sabah yoxla:\n"
        f"{sabah_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return decision


# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [["səhər", "günorta", "axşam"]]
    await update.message.reply_text(
        "🌿 Xoş gəldiniz! İstixana məlumatlarını addım-addım daxil edəcəyik.\n\n"
        "Hər addımda bir sual veriləcək — cavabı yazıb göndərin.\n"
        "Ləğv etmək üçün /cancel yazın.\n\n"
        "🕐 Data vaxtı: səhər / günorta / axşam",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return DATA_VAXTI


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 KÖMƏK\n\n"
        "/start — Yeni gün məlumatlarını daxil etməyə başla\n"
        "/log   — Son 5 günün qeydlərini göstər\n"
        "/help  — Bu mesajı göstər\n"
        "/cancel — Cari daxiletməni ləğv et\n\n"
        "Məlumat daxiletmə zamanı addım-addım suallar veriləcək.\n"
        "Hər suala qısa cavab yazın (düymələrdən və ya klaviaturadan)."
    )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log = load_log()
    if not log:
        await update.message.reply_text("📋 Hələ heç bir qeyd saxlanılmayıb.")
        return

    last_5 = log[-5:]
    lines = ["📋 SON 5 GÜNÜN QEYDLƏRİ:\n"]
    for entry in reversed(last_5):
        lines.append(
            f"📅 {entry.get('tarix', 'N/A')} — "
            f"{entry.get('istixana', '')} | {entry.get('hava', '')}\n"
            f"   Torpaq: 5-7sm: {entry.get('torpaq_57', '')} | "
            f"10-12sm: {entry.get('torpaq_1012', '')}\n"
            f"   Bitki: {entry.get('bitki', '')} | "
            f"Məhsul: {entry.get('mehsul', '')}\n"
            f"   Problem: {entry.get('problem', '')}\n"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Daxiletmə ləğv edildi. /start ilə yenidən başlaya bilərsiniz.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── CONVERSATION STEPS ────────────────────────────────────────────────────────

async def step_data_vaxti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["data_vaxti"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 Bu günün tarixi nədir?\nNümunə: 24.04.2026",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TARIX


async def step_tarix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tarix"] = update.message.text.strip()
    keyboard = [["böyük", "kiçik"], ["orta"]]
    await update.message.reply_text(
        "🏠 İstixana növü hansıdır?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ISTIXANA


async def step_istixana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["istixana"] = update.message.text.strip()
    keyboard = [["günəşli", "buludlu"], ["yağışlı", "dumanlı"]]
    await update.message.reply_text(
        "🌤 Bu günün havası necədir?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return HAVA


async def step_hava(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["hava"] = update.message.text.strip()
    await update.message.reply_text(
        "🌡 07:00-dakı temperatur və rütubət?\nNümunə: 15°C / 85%  və ya  15 85",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TEMP_07


async def step_temp07(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_07"] = update.message.text.strip()
    await update.message.reply_text("🌡 11:00-dakı temperatur və rütubət?\nNümunə: 22°C / 55%")
    return TEMP_11


async def step_temp11(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_11"] = update.message.text.strip()
    await update.message.reply_text("🌡 14:00-dakı temperatur və rütubət?\nNümunə: 25°C / 40%")
    return TEMP_14


async def step_temp14(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_14"] = update.message.text.strip()
    await update.message.reply_text("🌡 18:00-dakı temperatur və rütubət?\nNümunə: 23°C / 60%")
    return TEMP_18


async def step_temp18(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_18"] = update.message.text.strip()
    keyboard = [["quru", "nəm"], ["yaş"]]
    await update.message.reply_text(
        "🌱 Torpaq 5-7 sm dərinlikdə necədir?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return TORPAQ_57


async def step_torpaq57(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["torpaq_57"] = update.message.text.strip()
    keyboard = [["quru", "nəm"], ["yaş"]]
    await update.message.reply_text(
        "🌱 Torpaq 10-12 sm dərinlikdə necədir?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return TORPAQ_1012


async def step_torpaq1012(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["torpaq_1012"] = update.message.text.strip()
    keyboard = [["normal", "sağlam"], ["günorta soluxma", "solğun"]]
    await update.message.reply_text(
        "🥒 Bitkinin ümumi vəziyyəti?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return BITKI


async def step_bitki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bitki"] = update.message.text.strip()
    await update.message.reply_text(
        "💧 Son sulama nə vaxt və nə qədər olub?\nNümunə: dünən 0.5L  və ya  2 gün əvvəl 0.4L",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SON_SULAMA


async def step_son_sulama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["son_sulama"] = update.message.text.strip()
    await update.message.reply_text(
        "🌿 Son gübrə nə vaxt verildi?\nNümunə: 18-11-59 2 gün əvvəl  və ya  yoxdur"
    )
    return SON_GUBRE


async def step_son_gubre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["son_gubre"] = update.message.text.strip()
    await update.message.reply_text(
        "📦 Bugünkü məhsul miqdarı?\nNümunə: 600 kq  və ya  yoxdur"
    )
    return MEHSUL


async def step_mehsul(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mehsul"] = update.message.text.strip()
    await update.message.reply_text(
        "⚠️ Bu gün hər hansı problem var?\nNümunə: günorta rütubət düşür  və ya  yoxdur"
    )
    return PROBLEM


async def step_problem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["problem"] = update.message.text.strip()

    # Save to log
    data = dict(context.user_data)
    data["timestamp"] = datetime.now().isoformat()
    save_entry(data)

    await update.message.reply_text(
        "✅ Məlumatlar qeyd edildi. Qərar hazırlanır...",
        reply_markup=ReplyKeyboardRemove(),
    )

    decision = generate_decision(data)
    await update.message.reply_text(decision)

    return ConversationHandler.END


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            DATA_VAXTI:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_data_vaxti)],
            TARIX:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_tarix)],
            ISTIXANA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_istixana)],
            HAVA:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_hava)],
            TEMP_07:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_temp07)],
            TEMP_11:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_temp11)],
            TEMP_14:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_temp14)],
            TEMP_18:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_temp18)],
            TORPAQ_57:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_torpaq57)],
            TORPAQ_1012: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_torpaq1012)],
            BITKI:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_bitki)],
            SON_SULAMA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_son_sulama)],
            SON_GUBRE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_son_gubre)],
            MEHSUL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_mehsul)],
            PROBLEM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_problem)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("log",   cmd_log))

    print("🌿 Greenhouse bot işə düşdü... (dayandırmaq üçün Ctrl+C)")
    app.run_polling()


if __name__ == "__main__":
    main()
