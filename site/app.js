(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const PAGE = 120;
  const RECENT_OK_DAYS = 3; // 이 기간 안에 정상 수집된 학회는 한 번의 접속 실패로 "확인 필요"가 되지 않음

  const state = { q: "", group: "", divs: new Set(), society: "", period: "", view: "timeline", sort: "date", newOnly: false, koOnly: false, shown: PAGE };
  let DATA = [], SOCS = {}, TAX = {}, STATUS = {}, UPDATED = null;
  const REPO = "decaf-cpu/kr-guidelines";
  const RECHECK_LABEL = "수동등록-재확인";
  const RECHECK_URL = `https://github.com/${REPO}/issues/new?template=manual-recheck.yml`;

  // ── 날짜 ─────────────────────────────
  const effDate = (it) => it.date || it.first_seen || "";
  const sortKey = (it) => {
    if (state.sort === "seen") return (it.first_seen || "") + "|" + (it.date || "");
    const d = it.date;
    if (!d) return "0000|" + (it.first_seen || "");
    return (d.length === 4 ? d + "-00-00" : d.length === 7 ? d + "-00" : d) + "|" + (it.first_seen || "");
  };
  const fmtDate = (it) => {
    if (it.date) return it.date.replace(/-/g, ".");
    return it.first_seen ? it.first_seen.replace(/-/g, ".") : "—";
  };
  const dateNote = (it) => {
    if (!it.date) return it.sources.every((s) => s.manual) ? "" : "수집일";
    if (it.date_source === "bulk") return "일괄 등록일";
    if (it.date_source === "lookup") return "문헌 확인";
    if ((it.date_source || "").startsWith("file")) {
      return { "file-text": "PDF 본문 발행연도", "file-pdf": "PDF 작성일", "file-filename": "파일명 연도", "file-lastmod": "파일 등록일" }[it.date_source] || "파일 날짜";
    }
    if (it.date_precision === "year") {
      const base = { title: "제목 연도", "detail-year": "게시판 표기 연도" }[it.date_source] || "연도";
      return it.posted ? `${base} · 게시 ${it.posted.replace(/-/g, ".")}` : base;
    }
    return "";
  };
  // 발행 시점이 최근 n개월 이내인지. 월 단위는 그 달 마지막 날 기준,
  // 연도만 아는 항목은 1월인지 12월인지 알 수 없어 제외
  const withinMonths = (it, n) => {
    const d = it.date;
    if (!d || d.length === 4) return false;
    const [y, m, dd] = d.split("-").map(Number);
    const end = dd ? new Date(y, m - 1, dd) : m ? new Date(y, m, 0) : new Date(y, 11, 31);
    const cutoff = new Date();
    cutoff.setMonth(cutoff.getMonth() - n);
    return end >= cutoff;
  };
  const daysAgo = (iso) => (Date.now() - new Date(iso + "T00:00:00+09:00").getTime()) / 864e5;
  // 발간일이 최근 1년 안인지: 연도·월만 아는 항목은 그 기간의 첫날로 따져 확실한 것만
  const publishedWithinYear = (it) => {
    const d = it.date;
    if (!d) return false;
    const [y, m, dd] = d.split("-").map(Number);
    const start = new Date(y, (m || 1) - 1, dd || 1);
    const cutoff = new Date();
    cutoff.setFullYear(cutoff.getFullYear() - 1);
    return start >= cutoff;
  };
  // NEW: 새로 올라온 글(학회를 처음 수집할 때 이미 있던 글 제외) + 수집 30일 이내 + 발간 1년 이내
  const isNewItem = (it) => !it.backfill && !!it.first_seen && daysAgo(it.first_seen) <= 30 && publishedWithinYear(it);

  // ── URL 해시 ↔ 상태 ───────────────────
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    state.q = p.get("q") || "";
    state.group = p.get("g") || "";
    state.divs = new Set((p.get("d") || "").split(",").filter(Boolean));
    state.society = p.get("s") || "";
    state.period = p.get("p") || "";
    state.view = p.get("v") || "timeline";
    state.sort = p.get("o") || "date";
    state.newOnly = p.get("recent") === "1";
    state.koOnly = p.get("ko") === "1";
  }
  function writeHash() {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.group) p.set("g", state.group);
    if (state.divs.size) p.set("d", [...state.divs].join(","));
    if (state.society) p.set("s", state.society);
    if (state.period) p.set("p", state.period);
    if (state.view !== "timeline") p.set("v", state.view);
    if (state.sort !== "date") p.set("o", state.sort);
    if (state.newOnly) p.set("recent", "1");
    if (state.koOnly) p.set("ko", "1");
    const h = p.toString();
    history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
  }

  // ── 필터 ─────────────────────────────
  function matches(it, skip = "") {
    if (skip !== "group" && state.group && !it.groups.includes(state.group)) return false;
    if (skip !== "div" && state.divs.size && !it.divisions.some((d) => state.divs.has(d))) return false;
    if (state.society && !it.societies.includes(state.society)) return false;
    if (state.period) {
      const y = parseInt(effDate(it).slice(0, 4), 10);
      if (!y || y < new Date().getFullYear() - parseInt(state.period, 10) + 1) return false;
    }
    if (state.newOnly && !withinMonths(it, 6)) return false;
    if (state.koOnly && it.lang !== "ko") return false;
    if (state.q) {
      const hay = (it.title + " " + it.societies.map((s) => (SOCS[s]?.name || "") + " " + (SOCS[s]?.short || "")).join(" ") + " " + it.divisions.join(" ") + " " + effDate(it)).toLowerCase();
      for (const w of state.q.toLowerCase().split(/\s+/).filter(Boolean)) if (!hay.includes(w)) return false;
    }
    return true;
  }

  // ── 렌더: 컨트롤 ──────────────────────
  const pressed = (on) => `aria-pressed="${on ? "true" : "false"}"`;
  function renderControls() {
    const groups = (TAX.groups || []).filter((g) => DATA.some((it) => it.groups.includes(g)));
    const gCount = (g) => DATA.filter((it) => (!g || it.groups.includes(g)) && matches(it, "group")).length;
    $("#groups").innerHTML = [["", "전체"], ...groups.map((g) => [g, g])]
      .map(([v, label]) => `<button type="button" class="chip" data-group="${esc(v)}" ${pressed(state.group === v)}>${esc(label)}<span class="n">${gCount(v)}</span></button>`)
      .join("");

    const order = state.group ? TAX.divisions?.[state.group] || [] : Object.values(TAX.divisions || {}).flat();
    const present = new Set(DATA.filter((it) => !state.group || it.groups.includes(state.group)).flatMap((it) => it.divisions));
    const divs = [...order.filter((d) => present.has(d)), ...[...present].filter((d) => !order.includes(d)).sort()];
    const base = DATA.filter((it) => matches(it, "div"));
    $("#divisions").innerHTML = divs
      .map((d) => `<button type="button" class="chip" data-div="${esc(d)}" ${pressed(state.divs.has(d))}>${esc(d)}<span class="n">${base.filter((it) => it.divisions.includes(d)).length}</span></button>`)
      .join("");

    for (const b of document.querySelectorAll("#views button")) b.setAttribute("aria-pressed", b.dataset.view === state.view);
    if (document.activeElement !== $("#q")) $("#q").value = state.q;
    $("#q-clear").hidden = !$("#q").value;
    $("#period").value = state.period;
    $("#sort").value = state.sort;
    $("#society").value = state.society;
    $("#newOnly").checked = state.newOnly;
    $("#koOnly").checked = state.koOnly;
    renderApplied();
  }

  // 적용된 조건: 조건마다 삭제 버튼 + 전체 초기화 (KRDS 필터링·정렬)
  function appliedList() {
    const a = [];
    if (state.group) a.push(["group", "", `계열: ${state.group}`]);
    for (const d of state.divs) a.push(["div", d, `분과: ${d}`]);
    if (state.society) a.push(["society", "", `학회: ${SOCS[state.society]?.short || SOCS[state.society]?.name || state.society}`]);
    if (state.period) a.push(["period", "", `최근 ${state.period}년`]);
    if (state.newOnly) a.push(["newOnly", "", "최근 6개월 이내"]);
    if (state.koOnly) a.push(["koOnly", "", "국문 제목만"]);
    return a;
  }
  function renderApplied() {
    const a = appliedList();
    const el = $("#applied");
    $("#filter-n").textContent = a.length ? a.length : "";
    el.hidden = !a.length && !state.q;
    if (el.hidden) { el.innerHTML = ""; return; }
    el.innerHTML = `<span class="applied-h">적용된 조건 ${a.length + (state.q ? 1 : 0)}개</span>` +
      (state.q ? `<button type="button" class="applied-tag" data-rm="q">검색어: ${esc(state.q)}<i class="ico ico-delete" aria-hidden="true"></i><span class="sr-only"> 조건 삭제</span></button>` : "") +
      a.map(([k, v, label]) => `<button type="button" class="applied-tag" data-rm="${k}" data-v="${esc(v)}">${esc(label)}<i class="ico ico-delete" aria-hidden="true"></i><span class="sr-only"> 조건 삭제</span></button>`).join("") +
      `<button type="button" class="text-btn" data-rm="all"><i class="ico ico-reset" aria-hidden="true"></i>전체 초기화</button>`;
  }
  function removeCondition(k, v) {
    if (k === "all") { state.q = ""; state.group = ""; state.divs.clear(); state.society = ""; state.period = ""; state.newOnly = false; state.koOnly = false; $("#q").value = ""; }
    else if (k === "q") { state.q = ""; $("#q").value = ""; }
    else if (k === "div") state.divs.delete(v);
    else if (k === "newOnly" || k === "koOnly") state[k] = false;
    else state[k] = "";
    state.shown = PAGE;
  }

  // ── 렌더: 목록 ────────────────────────
  const NW = '<span class="sr-only"> (새 창)</span>';
  const terms = () => state.q.split(/\s+/).filter(Boolean);
  // 검색어 강조: 원문을 검색어 기준으로 나눈 뒤 조각마다 이스케이프
  function hl(text) {
    const ts = terms();
    if (!ts.length) return esc(text);
    const re = new RegExp(`(${ts.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
    return String(text).split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join("");
  }
  // 제목: 학술지 제목에 섞여 온 기울임·위첨자 태그(<i>, &lt;i&gt;)는 서식으로 살리고 나머지 태그는 지운다
  const unent = (t) => String(t).replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  function titleHTML(t) {
    return unent(t).split(/(<\/?(?:i|em|sup|sub)>)/i)
      .map((p, i) => (i % 2 ? p.toLowerCase() : hl(p.replace(/<\/?[a-z][^>]*>/gi, ""))))
      .join("");
  }
  const EXT = /\.(pdf|hwpx?|docx?|pptx?|xlsx?|zip)(?:$|[?#&])/i;
  function fileInfo(f) {
    const k = (f.kind || "").toLowerCase();
    if (k === "원문" || k === "pubmed") return { link: true, label: f.kind === "원문" ? "원문" : "PubMed" };
    let ext = k !== "file" ? k : "";
    if (!ext) {
      let raw = f.name + " " + f.url;
      try { const m = /filename=([^&]+)/.exec(f.url); if (m) raw += " " + atob(decodeURIComponent(m[1])); } catch (e) { /* 파일명 해석 실패는 무시 */ }
      const m = EXT.exec(raw.split(/\s+/).find((w) => EXT.test(w)) || "");
      ext = m ? m[1].toLowerCase() : "";
    }
    return { link: false, label: ext ? ext.toUpperCase() : "첨부" };
  }
  function itemHTML(it) {
    const main = it.sources[0];
    const isNew = isNewItem(it);
    const seen = new Set();
    const socs = it.sources
      .filter((s) => { const k = `${s.society}|${s.journal ? 1 : 0}`; return seen.has(k) ? false : seen.add(k); })
      .map((s) => {
        const name = SOCS[s.society]?.name || s.society;
        const where = s.journal ? `학회지 ${s.board_name || ""}`.trim() : "진료지침 게시판";
        return `<a class="tag-link" href="${esc(s.board || SOCS[s.society]?.board || s.url)}" target="_blank" rel="noopener" title="${esc(name)} ${esc(where)}">${hl(SOCS[s.society]?.short || s.society)}${s.manual ? ' ✎<span class="sr-only">(수동 등록)</span>' : ""}${s.journal ? " · 학회지" : ""}<span class="sr-only"> ${esc(where)}로 이동</span>${NW}</a>`;
      })
      .join("");
    const files = it.sources.flatMap((s) => s.attachments || []).filter((f, i, a) => a.findIndex((g) => g.url === f.url) === i).slice(0, 4)
      .map((f) => {
        const x = fileInfo(f);
        return `<a class="file" href="${esc(f.url)}" target="_blank" rel="noopener" title="${esc(f.name)}"><i class="ico ${x.link ? "ico-external" : "ico-download"}" aria-hidden="true"></i>${esc(x.label)}<span class="sr-only">${x.link ? " 보기" : " 내려받기"}</span>${NW}</a>`;
      })
      .join("");
    const tags = (it.tags || []).map((t) => `<span class="badge type">${esc(t)}</span>`).join("");
    const divs = it.divisions.map((d) => `<span class="badge">${hl(d)}</span>`).join("");
    const note = dateNote(it);
    const dt = it.date || it.first_seen || "";
    return `<li class="item">
      <div class="date"><time${dt ? ` datetime="${esc(dt)}"` : ""}>${esc(fmtDate(it))}</time>${note ? `<span class="src"${it.date_ref ? ` title="${esc(it.date_ref)}"` : ""}>${esc(note)}</span>` : ""}</div>
      <div>
        ${isNew ? '<span class="badge-new">NEW<span class="sr-only"> 새 지침</span></span>' : ""}<a class="title" href="${esc(main.url)}" target="_blank" rel="noopener">${titleHTML(it.title)}${NW}</a>
        <div class="meta">${socs}${divs}${tags}${files ? `<span class="files">${files}</span>` : ""}</div>
      </div>
    </li>`;
  }

  function groupBy(list) {
    const groups = new Map();
    const push = (k, it) => (groups.has(k) ? groups.get(k) : groups.set(k, []).get(k)).push(it);
    if (state.view === "timeline") {
      for (const it of list) {
        const d = state.sort === "seen" ? it.first_seen : it.date;
        push(d ? d.slice(0, 4) + "년" : "연도 미상", it);
      }
      return [...groups.entries()].sort((a, b) => (a[0] === "연도 미상") - (b[0] === "연도 미상") || b[0].localeCompare(a[0]));
    }
    if (state.view === "division") {
      for (const it of list) for (const d of it.divisions) if (!state.divs.size || state.divs.has(d)) push(d, it);
      const order = Object.values(TAX.divisions || {}).flat();
      const idx = (k) => (order.indexOf(k) === -1 ? 999 : order.indexOf(k));
      return [...groups.entries()].sort((a, b) => idx(a[0]) - idx(b[0]) || a[0].localeCompare(b[0]));
    }
    for (const it of list) for (const s of it.societies) if (!state.society || s === state.society) push(SOCS[s]?.name || s, it);
    return [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0], "ko"));
  }

  function renderList() {
    const list = DATA.filter((it) => matches(it)).sort((a, b) => sortKey(b).localeCompare(sortKey(a)));
    // 결과 수는 필수 표시 + 바뀔 때 화면낭독기가 읽도록 aria-live (KRDS 검색 결과 확인)
    $("#count").innerHTML = (state.q ? `<span class="q">‘${esc(state.q)}’ 검색 결과 </span>` : "") + `총 <em>${list.length.toLocaleString()}</em>건`;
    if (!list.length) {
      const hasCond = appliedList().length > 0;
      $("#results").innerHTML = `<div class="empty">
        <h3>조건에 맞는 진료지침이 없습니다.</h3>
        ${state.q ? `<p>검색어의 철자를 확인하거나, 더 짧은 단어(예: ‘심부전’)로 다시 검색해 보세요.</p>` : ""}
        ${hasCond ? `<p>선택한 조건을 줄이면 더 많은 결과를 볼 수 있습니다.</p>` : ""}
        <p>학회 게시판의 회원 전용 지침은 수집하지 않습니다.</p>
        <div class="acts">
          ${state.q ? '<button type="button" class="btn-2" data-rm="q"><i class="ico ico-delete" aria-hidden="true"></i>검색어 지우기</button>' : ""}
          ${hasCond ? '<button type="button" class="btn-2" data-rm="all"><i class="ico ico-reset" aria-hidden="true"></i>조건 모두 초기화</button>' : ""}
        </div>
      </div>`;
      return;
    }
    let budget = state.shown;
    const html = [];
    for (const [k, items] of groupBy(list)) {
      if (budget <= 0) break;
      const part = items.slice(0, budget);
      budget -= part.length;
      html.push(`<h3 class="group-h">${esc(k)}<span class="n">${items.length}건</span></h3><ul class="list">${part.map(itemHTML).join("")}</ul>`);
    }
    const total = state.view === "timeline" ? list.length : groupBy(list).reduce((n, [, v]) => n + v.length, 0);
    if (state.shown < total) html.push(`<button type="button" class="more" id="more">더 보기 (${(total - state.shown).toLocaleString()}건 남음)</button>`);
    $("#results").innerHTML = html.join("");
    $("#more")?.addEventListener("click", () => {
      // 더 보기 후에는 새로 나온 첫 항목으로 초점을 옮겨 이어 읽을 수 있게
      const before = document.querySelectorAll("#results .item").length;
      state.shown += PAGE * 2;
      renderList();
      document.querySelectorAll("#results .item .title")[before]?.focus();
    });
  }

  // 다시 그린 뒤에도 키보드 초점을 누른 컨트롤에 그대로 둔다 (KRDS: 초점을 예측할 수 없는 곳으로 옮기지 않음)
  function focusKey(el) {
    if (!el || !el.dataset) return null;
    for (const k of ["group", "div", "view"]) if (el.dataset[k] !== undefined) return `[data-${k}="${CSS.escape(el.dataset[k])}"]`;
    return null;
  }
  function render() {
    const key = focusKey(document.activeElement);
    renderControls();
    renderList();
    writeHash();
    if (key) document.querySelector(`.controls ${key}, .result-bar ${key}`)?.focus();
  }

  // ── 수집 현황 ─────────────────────────
  function renderStatus() {
    const st = STATUS.societies || {};
    const count = {};
    for (const it of DATA) for (const s of it.societies) count[s] = (count[s] || 0) + 1;
    const gOrder = TAX.groups || [];
    const dOrder = Object.values(TAX.divisions || {}).flat();
    const rank = (arr, v) => (arr.indexOf(v) === -1 ? 999 : arr.indexOf(v));
    const rows = Object.values(SOCS)
      .sort((a, b) => rank(gOrder, a.group) - rank(gOrder, b.group) || rank(dOrder, a.division) - rank(dOrder, b.division) || a.name.localeCompare(b.name, "ko"))
      .map((s) => {
        const r = st[s.id];
        const journalOnly = s.enabled === false && (s.journals || []).length > 0;
        // 이번 수집만 접속에 실패하고 최근(RECENT_OK_DAYS일 안)에 정상 수집된 학회는 '정상'으로 두고 사유만 적는다
        const lastOk = r?.last_ok ? new Date(r.last_ok.length === 10 ? r.last_ok + "T00:00:00+09:00" : r.last_ok) : null;
        const blip = r && !r.ok && lastOk && (Date.now() - lastOk) / 864e5 <= RECENT_OK_DAYS && count[s.id] > 0;
        let badge = '<span class="st-warn">미수집</span>';
        if (s.enabled === false) badge = s.manual ? '<span class="st-ok">수동 등록</span>' : '<span class="st-warn">제외</span>';
        else if (r?.ok) badge = '<span class="st-ok">정상</span>';
        else if (blip) badge = `<span class="st-ok">정상</span><div class="st-board">이번 수집은 접속 실패 · 마지막 정상 수집 ${esc(r.last_ok.slice(0, 10).replace(/-/g, "."))}</div>`;
        else if (r) badge = '<span class="st-err">확인 필요</span>';
        if (s.enabled === false && !s.manual) badge = s.excluded ? `<span class="st-warn">수집 불가</span><div class="st-board">${esc(s.excluded)}</div>` : '<span class="st-warn">제외</span>';
        if (journalOnly) badge = `<span class="${r?.ok || blip ? "st-ok" : r ? "st-err" : "st-warn"}">학회지만 수집</span><div class="st-board">게시판: ${esc(s.excluded || "제외")}</div>`;
        const off = s.enabled === false && !journalOnly;
        const boards = (off ? [] : (r?.boards || []).filter((b) => !(blip && b.error)))
          .map((b) => `<div class="st-board">${b.source === "discovered" ? "자동탐색 · " : b.source === "journal" ? "학회지 · " : ""}<a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.name || b.url)}${NW}</a>${b.error ? " — " + esc(b.error) : ` — ${b.matched ?? 0}건`}${routedNote(b)}</div>`)
          .join("");
        const err = !off && !blip && r?.error ? `<div class="st-board">${esc(r.error)}</div>` : "";
        return `<tr><td>${esc(s.division)}</td><td><a href="${esc(s.board || s.homepage)}" target="_blank" rel="noopener">${esc(s.name)}${NW}</a>${s.note ? `<div class="st-board">${esc(s.note)}</div>` : ""}</td>
          <td>${badge}${err}${boards}</td><td class="num">${s.excluded && !journalOnly ? "—" : count[s.id] || 0}</td><td class="st-board">${off ? "" : esc((r?.run_at || "").replace("T", " ").slice(0, 16))}</td></tr>`;
      })
      .join("");
    $("#status").innerHTML = renderManual() + `<h2>학회별 수집 현황</h2><div class="tbl-wrap"><table class="st"><caption class="sr-only">학회별 수집 현황: 분과, 학회, 상태와 게시판, 건수, 마지막 수집 시각</caption><thead><tr><th scope="col">분과</th><th scope="col">학회</th><th scope="col">상태 · 게시판</th><th scope="col" class="num">건수</th><th scope="col">마지막 수집</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">회원 전용으로 판정되어 제외된 게시물: ${(STATUS.hidden_members_only || 0).toLocaleString()}건 · 학회 배지의 ✎ 표시는 자동 수집이 막혀 손으로 등록한 항목입니다.</p>
      ${(STATUS.manual_errors || []).length ? `<p class="note st-err">수동 등록 오류: ${STATUS.manual_errors.map(esc).join(" / ")}</p>` : ""}`;
    recheckLoaded = false;
    loadRecheckRequests();
  }

  // 학회지에 실린 다른 학회 지침은 그 학회로 넘긴다 (예: 내분비학회지의 갑상선학회 지침)
  function routedNote(b) {
    const r = Object.entries(b.routed || {});
    return r.length ? ` (그중 ${r.map(([id, n]) => `${esc(SOCS[id]?.short || id)} ${n}건`).join(", ")}은 해당 학회로)` : "";
  }

  // ── 수동 등록 재확인 요청 ─────────────
  // 자동 수집이 막힌 학회는 관리자가 페이지를 직접 열어 확인한다. 방문자는 GitHub 이슈로 재확인을 요청한다.
  function renderManual() {
    const count = {};
    for (const it of DATA) for (const src of it.sources || []) if (src.manual) count[src.society] = (count[src.society] || 0) + 1;
    const today = new Date();
    const rows = Object.values(SOCS)
      .filter((s) => s.manual)
      // 지침이 등록된 학회 → 아직 등록 전 순
      .sort((a, b) => {
        const g = (s) => (count[s.id] ? 0 : 1);
        return g(a) - g(b) || a.name.localeCompare(b.name, "ko");
      })
      .map((s) => {
        const d = s.manual_checked;
        const days = d ? Math.floor((today - new Date(d + "T00:00:00+09:00")) / 864e5) : null;
        const when = d ? `${d.replace(/-/g, ".")}${days > 90 ? ' <span class="st-warn">· 오래됨</span>' : ""}` : '<span class="st-warn">아직 등록 전</span>';
        return `<tr><td><a href="${esc(s.board || s.homepage)}" target="_blank" rel="noopener">${esc(s.name)}${NW}</a></td><td class="num">${count[s.id] || 0}</td><td class="st-board">${when}</td></tr>`;
      })
      .join("");
    return `<div class="recheck">
      <h2>수동 등록 학회</h2>
      <p class="note">아래 학회는 robots.txt·웹 방화벽·해외 접속 차단 때문에 자동으로 모으지 않고, 관리자가 학회 페이지를 직접 열어 확인한 지침만 올립니다.
        새 지침이 빠진 것 같으면 재확인을 요청해 주세요. 관리자가 확인해 반영합니다 (GitHub 로그인 필요).</p>
      <div class="tbl-wrap"><table class="st"><caption class="sr-only">수동 등록 학회: 학회, 건수, 마지막 확인일</caption><thead><tr><th scope="col">학회</th><th scope="col" class="num">건수</th><th scope="col">마지막 확인</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="recheck-act"><a class="btn" href="${RECHECK_URL}" target="_blank" rel="noopener"><i class="ico ico-external" aria-hidden="true"></i>수동등록 재확인 요청${NW}</a><span id="recheck-open" class="st-board"></span></div>
    </div>`;
  }

  let recheckLoaded = false;
  async function loadRecheckRequests() {
    if (recheckLoaded) return;
    recheckLoaded = true;
    const el = $("#recheck-open");
    try {
      const r = await fetch(`https://api.github.com/repos/${REPO}/issues?state=open&per_page=5&labels=${encodeURIComponent(RECHECK_LABEL)}`);
      if (!r.ok) return;
      const open = (await r.json()).filter((x) => !x.pull_request);
      if (el && open.length)
        el.innerHTML = `접수된 요청 ${open.length}건 처리 대기 중: ` +
          open.map((x) => `<a href="${esc(x.html_url)}" target="_blank" rel="noopener">#${x.number} (${esc(x.created_at.slice(0, 10).replace(/-/g, "."))})</a>`).join(", ");
    } catch (e) { /* 요청 현황은 보조 정보라 실패해도 무시 */ }
  }

  // ── 이벤트 ───────────────────────────
  function bind() {
    let t;
    const applyQ = () => { clearTimeout(t); state.q = $("#q").value.trim(); state.shown = PAGE; render(); };
    $("#q").addEventListener("input", (e) => { $("#q-clear").hidden = !e.target.value; clearTimeout(t); t = setTimeout(applyQ, 150); });
    // 검색 버튼·Enter: 바로 적용하고 초점은 검색창에 둔다 (모바일 키보드는 닫힘)
    $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); applyQ(); if (matchMedia("(max-width: 767px)").matches) $("#q").blur(); });
    $("#q-clear").addEventListener("click", () => { $("#q").value = ""; applyQ(); $("#q").focus(); });
    $("#q").addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#q").value) { e.preventDefault(); $("#q").value = ""; applyQ(); } });
    // 좁은 화면에서는 검색창 안내 문구가 잘리지 않게 짧은 문구로 (KRDS: 검색어 입력 필드는 말줄임 없이)
    const q = $("#q"), longPh = q.placeholder, mq = matchMedia("(max-width: 767px)");
    const setPh = () => { q.placeholder = mq.matches ? q.dataset.short : longPh; };
    setPh(); mq.addEventListener?.("change", setPh);
    // 모바일: 필터 접기/펴기
    $("#filter-toggle").addEventListener("click", (e) => {
      const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
      e.currentTarget.setAttribute("aria-expanded", open);
      $(".controls").classList.toggle("open", open);
    });
    // 적용된 조건 삭제 · 결과 없음 화면의 초기화 버튼
    document.addEventListener("click", (e) => {
      const b = e.target.closest("#applied [data-rm], .empty [data-rm]"); if (!b) return;
      const tags = [...document.querySelectorAll("#applied [data-rm]")];
      const idx = tags.indexOf(b);
      removeCondition(b.dataset.rm, b.dataset.v || "");
      render();
      // 지운 조건 다음 버튼으로 초점 이동, 남은 조건이 없으면 검색창으로
      const next = [...document.querySelectorAll("#applied [data-rm]")];
      (idx >= 0 && next[Math.min(idx, next.length - 2)]) ? next[Math.min(idx, next.length - 2)].focus() : $("#q").focus();
    });
    $("#groups").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      state.group = b.dataset.group; state.divs.clear(); state.shown = PAGE; render();
    });
    $("#divisions").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const d = b.dataset.div; state.divs.has(d) ? state.divs.delete(d) : state.divs.add(d); state.shown = PAGE; render();
    });
    $("#views").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      state.view = b.dataset.view; state.shown = PAGE; render();
    });
    for (const [id, key] of [["#society", "society"], ["#period", "period"], ["#sort", "sort"]])
      $(id).addEventListener("change", (e) => { state[key] = e.target.value; state.shown = PAGE; render(); });
    for (const [id, key] of [["#newOnly", "newOnly"], ["#koOnly", "koOnly"]])
      $(id).addEventListener("change", (e) => { state[key] = e.target.checked; state.shown = PAGE; render(); });
    // 탭: 선택 탭만 Tab 순서에 두고 ←/→로 이동 (WAI-ARIA 탭 패턴)
    const tabBtns = [...document.querySelectorAll(".tabs [role=tab]")];
    $(".tabs [role=tablist]").addEventListener("keydown", (e) => {
      const i = tabBtns.indexOf(document.activeElement);
      if (i < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const n = e.key === "Home" ? 0 : e.key === "End" ? tabBtns.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabBtns.length) % tabBtns.length;
      tabBtns[n].focus(); tabBtns[n].click();
    });
    for (const b of tabBtns)
      b.addEventListener("click", () => {
        for (const x of tabBtns) { x.setAttribute("aria-selected", x === b); x.tabIndex = x === b ? 0 : -1; }
        $("#tab-list").hidden = b.dataset.tab !== "list";
        $("#tab-status").hidden = b.dataset.tab !== "status";
        if (b.dataset.tab === "status") renderStatus();
      });
    window.addEventListener("hashchange", () => { readHash(); render(); });
    $("#foot-recheck").addEventListener("click", (e) => {
      e.preventDefault();
      document.querySelector('.tabs button[data-tab="status"]').click();
      $("#status .recheck")?.scrollIntoView({ behavior: "smooth" });
    });
  }

  // ── 시작 ─────────────────────────────
  async function main() {
    try {
      const [g, s, st] = await Promise.all(["data/guidelines.json", "data/societies.json", "data/status.json"].map((u) => fetch(u, { cache: "no-cache" }).then((r) => r.json())));
      DATA = g.items || [];
      UPDATED = g.updated;
      TAX = s.taxonomy || {};
      for (const x of s.societies || []) SOCS[x.id] = x;
      STATUS = st || {};
    } catch (e) {
      $("#sub").textContent = "데이터를 불러오지 못했습니다.";
      $("#results").innerHTML = `<div class="empty">data/guidelines.json을 읽을 수 없습니다. 첫 수집이 끝났는지 확인하세요.</div>`;
      return;
    }
    const nSoc = new Set(DATA.flatMap((it) => it.societies)).size;
    const when = UPDATED ? UPDATED.replace("T", " ").slice(0, 16) : "—";
    $("#sub").textContent = `대한의학회 회원학회 ${nSoc}곳 · 진료지침 ${DATA.length.toLocaleString()}건 · 마지막 수집 ${when} (KST)`;
    const opts = Object.values(SOCS).filter((x) => DATA.some((it) => it.societies.includes(x.id))).sort((a, b) => a.name.localeCompare(b.name, "ko"));
    $("#society").insertAdjacentHTML("beforeend", opts.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join(""));
    readHash();
    bind();
    render();
  }
  main();
})();

// 고정 헤더 높이에 맞춰 그룹 제목(sticky) 위치 조정 — 글자 크기·화면 폭이 바뀌어도 겹치지 않게
(() => {
  const top = document.querySelector(".top");
  if (!top) return;
  const set = () => document.documentElement.style.setProperty("--top-h", `${top.offsetHeight}px`);
  set();
  if ("ResizeObserver" in window) new ResizeObserver(set).observe(top);
})();

// ── 화면 설정: 글자 크기 · 화면 모드 (KRDS 화면 크기 조절 · 선명한 화면 모드) ──
(() => {
  const root = document.documentElement;
  const load = () => { try { return JSON.parse(localStorage.getItem("view-settings") || "{}"); } catch (e) { return {}; } };
  const save = (v) => { try { localStorage.setItem("view-settings", JSON.stringify(v)); } catch (e) { /* 저장이 막힌 브라우저는 이번 방문에만 적용 */ } };
  let cur = Object.assign({ scale: 1, mode: "theme" }, load());
  const apply = () => {
    root.dataset.mode = cur.mode;
    root.style.fontSize = cur.scale === 1 ? "" : `${62.5 * cur.scale}%`;
    for (const r of document.querySelectorAll('#settings-menu input[name="scale"]')) r.checked = Number(r.value) === Number(cur.scale);
    for (const r of document.querySelectorAll('#settings-menu input[name="mode"]')) r.checked = r.value === cur.mode;
  };
  apply();
  document.querySelector("#settings-menu")?.addEventListener("change", (e) => {
    if (e.target.name === "scale") cur.scale = Number(e.target.value);
    if (e.target.name === "mode") cur.mode = e.target.value;
    apply(); save(cur);
  });
  document.querySelector("#settings-reset")?.addEventListener("click", () => { cur = { scale: 1, mode: "theme" }; apply(); save(cur); });
})();

// ── 펼침 메뉴 공통: 화면 설정 · 도움말 (Esc·바깥 클릭으로 닫고 초점은 버튼으로) ──
(() => {
  const pairs = [["#settings", ".util-btn", "#settings-menu"], ["#help", ".help-btn", "#help-pop"]]
    .map(([w, b, m]) => ({ wrap: document.querySelector(w), btn: document.querySelector(`${w} ${b}`), menu: document.querySelector(m) }))
    .filter((p) => p.wrap && p.btn && p.menu);
  const close = (p, refocus) => { if (p.menu.hidden) return; p.menu.hidden = true; p.btn.setAttribute("aria-expanded", "false"); if (refocus) p.btn.focus(); };
  for (const p of pairs) {
    p.btn.addEventListener("click", () => {
      const open = p.menu.hidden;
      for (const q of pairs) close(q, false);
      if (open) { p.menu.hidden = false; p.btn.setAttribute("aria-expanded", "true"); p.menu.querySelector("input:checked, button, a")?.focus(); }
    });
    p.menu.querySelector(".help-close")?.addEventListener("click", () => close(p, true));
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") for (const p of pairs) if (!p.menu.hidden && p.wrap.contains(document.activeElement)) close(p, true); });
  document.addEventListener("click", (e) => { for (const p of pairs) if (!p.wrap.contains(e.target)) close(p, false); });
  // 메뉴 밖으로 Tab이 나가면 닫기
  document.addEventListener("focusin", (e) => { for (const p of pairs) if (!p.wrap.contains(e.target)) close(p, false); });
})();

// ── 맨 위로: 한 화면 이상 내려가면 표시, 누르면 위로 이동하고 초점은 본문 시작으로 ──
(() => {
  const btn = document.querySelector("#go-top");
  if (!btn) return;
  const onScroll = () => { btn.hidden = window.scrollY < window.innerHeight; };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  btn.addEventListener("click", () => { window.scrollTo({ top: 0 }); document.querySelector("#main")?.focus({ preventScroll: true }); });
})();
