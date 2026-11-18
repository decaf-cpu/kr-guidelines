"""국문 게시판 지침과 영문 학회지 논문이 같은 지침인지 찾는다.

학회 게시판에는 '2023 대한갑상선학회 임신 중 및 산후 갑상선질환의 진단 및 치료 권고안 개정안'으로,
학회지에는 'Diagnosis and Management of Thyroid Disease during Pregnancy and Postpartum: 2023 Revised
Korean Thyroid Association Guidelines'로 올라오는 식이라 제목 글자로는 묶을 수 없다.
주제어 사전(국문 ↔ 영문)으로 두 제목을 같은 '주제 묶음'으로 바꿔 비교한다.

판정 (보수적으로):
- 발행연도 차이 1년 이내, 'Part II'처럼 편 번호가 있으면 같아야 함
- 공통 주제어 2개 이상, 그리고 한쪽 주제어가 다른 쪽에 거의 다 들어 있어야 함 (짧은 쪽의 80% 이상)
- 학회가 다르면 공통 주제어 3개 이상
자동으로 못 찾는 경우는 config/duplicates.yaml에 적는다.
"""
from __future__ import annotations

import re
import unicodedata

# (주제, 국문 패턴, 영문 패턴) — 영문은 소문자로 비교
TERMS: list[tuple[str, str, str]] = [
    # 갑상선
    ("dtc", r"갑상선\s*분화암|분화\s*갑상선암", r"differentiated thyroid"),
    ("thyroid_ca", r"갑상선\s*(분화|유두)?\s*암", r"thyroid (cancer|carcinoma)|papillary thyroid"),
    ("thyroid_nodule", r"갑상선\s*결절", r"thyroid nodule"),
    ("hyperthyroid", r"갑상선\s*기능\s*항진|그레이브스", r"hyperthyroid|graves"),
    ("subclin_hypo", r"무증상\s*갑상선\s*기능\s*저하", r"subclinical hypothyroid"),
    ("congenital_hypo", r"선천성\s*갑상선\s*기능\s*저하", r"congenital hypothyroid"),
    ("thyroid", r"갑상선", r"thyroid"),
    ("active_surv", r"능동\s*감시|적극적\s*관찰", r"active surveillance"),
    ("rai", r"방사성\s*요오드", r"radioactive iodine|radioiodine"),
    ("followup", r"추적|추적\s*관찰", r"follow-?up|surveillance after"),
    ("pregnancy", r"임신|산후|산모", r"pregnan|postpartum"),
    ("pediatric", r"소아|청소년|어린이|아동", r"child|pediatric|paediatric|adolescen|infant"),
    # 내분비·골대사
    ("bone_health", r"골\s*건강", r"bone health"),
    ("osteoporosis", r"골다공증", r"osteopor"),
    ("gio", r"(글루코코르티코이드|스테로이드)\s*(유발|유도)", r"glucocorticoid-induced"),
    ("mronj", r"(턱뼈|악골)\s*괴사", r"osteonecrosis of the jaw"),
    ("aff", r"비정형\s*대퇴(골)?\s*골절", r"atypical femoral fracture"),
    ("fls", r"골절\s*연계|FLS", r"fracture liaison"),
    ("btm", r"골\s*(교체|표지|대사)\s*표지", r"bone turnover marker"),
    ("drug_holiday", r"약물\s*휴지|휴약", r"drug holiday"),
    ("vitd", r"비타민\s*D", r"vitamin d"),
    ("calcium", r"칼슘", r"calcium"),
    ("fracture", r"골절", r"fracture"),
    ("obesity", r"비만", r"obesity|overweight"),
    ("diabetes", r"당뇨", r"diabet"),
    ("dyslipidemia", r"이상\s*지질|고지혈", r"dyslipid"),
    ("aldosteronism", r"알도스테론", r"aldosteron"),
    ("cushing", r"쿠싱", r"cushing"),
    ("pheo", r"갈색\s*세포종|부신경절종", r"pheochromocytoma|paraganglioma"),
    ("adrenal_inc", r"부신\s*우연종", r"adrenal incidentaloma"),
    ("ici", r"면역\s*관문\s*억제", r"immune checkpoint"),
    ("ghd", r"성장\s*호르몬\s*결핍", r"growth hormone deficiency"),
    ("puberty", r"성\s*조숙", r"precocious puberty"),
    ("menopause", r"폐경", r"menopaus"),
    ("hrt", r"호르몬\s*(대체\s*)?요법", r"hormone (replacement )?therapy"),
    # 소화기
    ("gerd", r"위식도\s*역류", r"gastro-?esophageal reflux|\bgerd\b"),
    ("ibs", r"과민성\s*장", r"irritable bowel"),
    ("constipation", r"변비", r"constipation"),
    ("dyspepsia", r"소화\s*불량", r"dyspepsia"),
    ("achalasia", r"이완\s*불능|아칼라지아", r"achalasia"),
    ("fmt", r"(대변|분변)\s*미생물", r"fecal microbiota"),
    ("probiotic", r"프로바이오틱", r"probiotic"),
    ("colonoscopy", r"대장\s*내시경", r"colonoscop"),
    ("bowel_prep", r"장\s*정결", r"bowel prep"),
    ("polypectomy", r"용종\s*절제", r"polypectomy"),
    ("sedation", r"진정", r"sedation"),
    ("peg", r"위루", r"gastrostomy"),
    ("capsule", r"캡슐\s*내시경", r"capsule endoscop"),
    ("stent", r"스텐트", r"stent"),
    ("reprocess", r"재처리|세척\s*(및\s*)?소독", r"reprocessing"),
    ("eus", r"초음파\s*내시경", r"endoscopic ultrasound|\beus\b"),
    ("crc", r"대장암", r"colorectal cancer"),
    ("hpylori", r"헬리코박터|H\.?\s*pylori", r"helicobacter|h\. ?pylori"),
    # 부인과
    ("cervical_ca", r"자궁\s*경부암", r"cervical cancer"),
    ("ovarian_ca", r"난소암", r"ovarian cancer"),
    ("uterine_ca", r"자궁\s*(내막|체부)암", r"uterine corpus|endometrial cancer"),
    ("hpv", r"인유두종|HPV", r"papillomavirus|\bhpv\b"),
    ("endometriosis", r"자궁\s*내막증", r"endometriosis"),
    ("fertility", r"가임력", r"fertility preservation"),
    ("contraception", r"피임", r"contracept"),
    # 기타
    ("vaccine", r"백신|예방\s*접종", r"vaccin|immuniz"),
    ("heart_failure", r"심부전", r"heart failure"),
    ("palliative", r"완화|호스피스", r"palliative|hospice"),
    ("sarcopenia", r"근감소", r"sarcopenia"),
    ("protein", r"단백질", r"protein"),
    ("aplastic", r"재생\s*불량", r"aplastic"),
    ("myeloma", r"골수종", r"myeloma"),
    ("itp", r"면역\s*혈소판\s*감소", r"immune thrombocytopenia"),
    ("hemolytic", r"용혈", r"hemolytic"),
    ("vte", r"정맥\s*혈전", r"venous thromboembolism"),
    ("axspa", r"척추\s*관절염|강직\s*척추염", r"spondyloarthritis|ankylosing"),
    ("rheumatic", r"류마티스|자가\s*면역", r"rheumat|autoimmune"),
    ("gout", r"통풍", r"\bgout\b"),
    ("pneumococcal", r"폐렴\s*구균", r"pneumococc"),
    ("rsv", r"RSV|호흡기\s*세포\s*융합", r"respiratory syncytial|\brsv\b"),
    ("covid", r"코로나|COVID", r"covid|sars-cov-2"),
    ("exercise", r"운동", r"exercise"),
]
_KO = [(k, re.compile(p, re.I)) for k, p, _ in TERMS]
_EN = [(k, re.compile(p, re.I)) for k, _, p in TERMS]
# 넓은 주제어는 더 좁은 주제어가 있으면 뺀다 (갑상선분화암이면 '갑상선'은 셈하지 않음)
_BROADER = {"thyroid": {"dtc", "thyroid_ca", "thyroid_nodule", "hyperthyroid", "subclin_hypo", "congenital_hypo"},
            "thyroid_ca": {"dtc"}, "fracture": {"aff", "fls"}, "osteoporosis": {"gio"}}
_HANGUL = re.compile(r"[가-힣]")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_PART = re.compile(r"\bpart\s*([ivx]+|\d+)\b", re.I)


def concepts(title: str) -> set[str]:
    t = unicodedata.normalize("NFC", title or "")
    table = _KO if _HANGUL.search(t) else _EN
    found = {k for k, rx in table if rx.search(t)}
    for broad, narrow in _BROADER.items():
        if broad in found and found & narrow:
            found.discard(broad)
    return found


def part_no(title: str) -> int | None:
    m = _PART.search(title or "")
    if not m:
        return None
    v = m.group(1).lower()
    return int(v) if v.isdigit() else _ROMAN.get(v)


def same_guideline(ko: dict, en: dict, year_of) -> bool:
    """ko: 국문 제목 묶음, en: 영문(학회지) 묶음. 둘 다 build의 group(dict)."""
    ya, yb = year_of(ko), year_of(en)
    if not ya or not yb or abs(int(ya) - int(yb)) > 1:
        return False
    pa, pb = part_no(ko["title"]), part_no(en["title"])
    if (pa or pb) and pa != pb:
        return False
    ca, cb = concepts(ko["title"]), concepts(en["title"])
    common = ca & cb
    need = 2 if set(ko["societies"]) & set(en["societies"]) else 3
    if len(common) < need:
        return False
    return len(common) >= 0.8 * min(len(ca), len(cb))
