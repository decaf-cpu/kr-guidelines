"""게시물 제목이 진료지침인지 판정하고 태그를 붙인다."""
from __future__ import annotations

import re

STRONG = re.compile(
    r"(진료\s*지침|임상\s*지침|치료\s*지침|진료\s*가이드\s*라인|가이드\s*라인|guideline|"
    r"권고\s*안|권고\s*문|권고\s*사항|합의\s*문|합의\s*안|합의\s*성명|consensus|"
    r"position\s*statement|입장\s*문|성명서|지침|recommendation|practice\s*parameter)",
    re.I,
)

# 행사·행정 공지 (진료지침 문서가 아님)
EVENT = re.compile(
    r"(학술\s*대회|연수\s*강좌|심포지(엄|움)|워크\s*숍|워크샵|세미나|설명회|공청회|"
    r"사전\s*등록|등록\s*안내|초록\s*(접수|모집)|모집|채용|선거|총회|이사회|회비|부고|"
    r"개최\s*안내|일정\s*안내|설문|투표|후원|광고|교육\s*과정|강의|웨비나|webinar|"
    r"symposium|congress|workshop|meeting|구입|판매|구매|주문)",
    re.I,
)

# 행사 공지처럼 보여도 문서 공개를 알리는 제목이면 살린다
PUBLISH = re.compile(r"(발간|공개|게재|배포|개정판|요약본|전문|다운로드|pdf)", re.I)

HANGUL = re.compile(r"[가-힣]")


def is_guideline(title: str, dedicated: bool = False) -> bool:
    t = title or ""
    if len(t.strip()) < 4:
        return False
    if EVENT.search(t) and not PUBLISH.search(t):
        return False
    if dedicated:
        return True
    return bool(STRONG.search(t))


def tags_for(title: str) -> list[str]:
    t = title or ""
    tags = []
    if re.search(r"(개정|update|updated|revision|revised)", t, re.I):
        tags.append("개정")
    if re.search(r"(요약|summary|synopsis|pocket|포켓)", t, re.I):
        tags.append("요약본")
    if re.search(r"(환자|일반인|국민|patient)", t, re.I):
        tags.append("환자용")
    if re.search(r"(합의|consensus|전문가\s*의견|expert\s*opinion)", t, re.I):
        tags.append("합의문")
    if re.search(r"(권고|recommendation|position\s*statement|성명|입장)", t, re.I):
        tags.append("권고·성명")
    return tags


def lang_of(title: str) -> str:
    return "ko" if HANGUL.search(title or "") else "en"
