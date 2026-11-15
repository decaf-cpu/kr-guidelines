"""HTTP 요청: robots.txt 준수, 호스트별 요청 간격, 인코딩 처리, SSL 오류 재시도."""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
import urllib3

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; KoreanGuidelineIndex/1.0; "
    "+non-commercial index of public clinical practice guidelines)"
)

_ENC_ALIASES = {
    "euc-kr": "cp949",
    "euckr": "cp949",
    "ks_c_5601-1987": "cp949",
    "ksc5601": "cp949",
    "x-windows-949": "cp949",
}


def _norm_encoding(enc: str | None) -> str | None:
    if not enc:
        return None
    enc = enc.strip().strip("\"'").lower()
    return _ENC_ALIASES.get(enc, enc)


def decode(resp: requests.Response) -> str:
    """한국 학회 사이트의 EUC-KR/CP949 페이지까지 제대로 디코딩."""
    raw = resp.content
    enc = None
    ct = resp.headers.get("content-type", "").lower()
    m = re.search(r"charset=([\w\-]+)", ct)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", raw[:5000], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    enc = _norm_encoding(enc)
    if not enc:
        try:
            raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "cp949"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(
        self,
        delay: float = 1.5,
        timeout: float = 25,
        respect_robots: bool = True,
        user_agent: str = DEFAULT_UA,
        retries: int = 2,
    ):
        self._local = threading.local()
        self._lock = threading.Lock()
        self.delay = delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.retries = retries
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.insecure_hosts: set[str] = set()
        self.blocked_hosts: set[str] = set()  # 방화벽 차단 페이지를 받은 사이트: 이번 실행 동안 더 요청하지 않음
        self.request_count = 0

    # ── 내부 도우미 ──────────────────────────────
    @property
    def session(self) -> requests.Session:
        """스레드마다 세션 하나 (학회 여러 곳을 동시에 수집)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
                }
            )
            self._local.session = s
        return s

    def _wait(self, host: str) -> None:
        """같은 사이트에는 delay초 간격 보장 (다른 사이트끼리는 동시 진행)."""
        while True:
            with self._lock:
                last = self._last_hit.get(host)
                now = time.monotonic()
                if last is None or now - last >= self.delay:
                    self._last_hit[host] = now
                    self.request_count += 1
                    return
                wait = self.delay - (now - last)
            time.sleep(wait)

    def _raw_get(self, url: str, verify: bool, stream: bool = False, headers: dict | None = None) -> requests.Response:
        host = urlparse(url).netloc
        if host in self.insecure_hosts:
            verify = False
        self._wait(host)
        try:
            return self.session.get(url, timeout=self.timeout, verify=verify, stream=stream, allow_redirects=True, headers=headers)
        except requests.exceptions.SSLError:
            if not verify:
                raise
            # 국내 학회 사이트는 중간 인증서 누락이 흔함 → 공개 페이지 열람에 한해 검증 없이 재시도
            log.warning("SSL 검증 실패, 검증 없이 재시도: %s", host)
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.insecure_hosts.add(host)
            self._wait(host)
            return self.session.get(url, timeout=self.timeout, verify=False, stream=stream, allow_redirects=True, headers=headers)

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self._raw_get(base + "/robots.txt", verify=True)
                if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                    rp.parse(decode(r).splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None  # robots.txt 없음 → 허용
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ── 공개 API ─────────────────────────────────
    def get(self, url: str, verify: bool = True, stream: bool = False, headers: dict | None = None) -> requests.Response | None:
        host = urlparse(url).netloc
        if host in self.blocked_hosts:
            return None
        if not self.allowed(url):
            log.info("robots.txt 차단: %s", url)
            return None
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                r = self._raw_get(url, verify=verify, stream=stream, headers=headers) if headers else self._raw_get(url, verify=verify, stream=stream)
                if r.status_code >= 500 and attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                if self._is_block_page(r):
                    log.warning("방화벽 차단 감지, 이번 실행에서 %s 요청 중단", host)
                    self.blocked_hosts.add(host)
                    r.close()
                    return None
                return r
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
        log.warning("요청 실패: %s (%s)", url, last_exc)
        return None

    @staticmethod
    def _is_block_page(r: requests.Response) -> bool:
        if r.status_code in (403, 406, 429) or "html" in r.headers.get("content-type", "").lower():
            cl = r.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > 5000:
                return False
            head = r.content[:3000] if not r.raw or r._content_consumed else b""
            if not head and cl and cl.isdigit() and int(cl) <= 5000:
                head = r.content[:3000]
            return bool(re.search(rb"(web\s*firewall|security polic|access denied|request blocked|too many requests|\xec\xb0\xa8\xeb\x8b\xa8)", head, re.I))
        return False

    def get_html(self, url: str, verify: bool = True) -> tuple[str | None, requests.Response | None]:
        r = self.get(url, verify=verify)
        if r is None or r.status_code >= 400:
            return None, r
        return decode(r), r
