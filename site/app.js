(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const PAGE = 120;

  const state = { q: "", group: "", divs: new Set(), society: "", period: "", view: "timeline", sort: "date", newOnly: false, koOnly: false, shown: PAGE };
  let DATA = [], SOCS = {}, TAX = {}, STATUS = {}, UPDATED = null;

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
    if ((it.date_source || "").startsWith("file")) {
      return { "file-pdf": "PDF 작성일", "file-filename": "파일명 연도", "file-lastmod": "파일 등록일" }[it.date_source] || "파일 날짜";
    }
    if (it.date_precision === "year") {
      const base = it.date_source === "title" ? "제목 연도" : "연도";
      return it.posted ? `${base} · 게시 ${it.posted.replace(/-/g, ".")}` : base;
    }
    return "";
  };
  // 발행 시점이 최근 n개월 이내인지 (연·월 단위 날짜는 그 기간의 마지막 날 기준)
  const withinMonths = (it, n) => {
    const d = it.date;
    if (!d) return false;
    const [y, m, dd] = d.split("-").map(Number);
    const end = dd ? new Date(y, m - 1, dd) : m ? new Date(y, m, 0) : new Date(y, 11, 31);
    const cutoff = new Date();
    cutoff.setMonth(cutoff.getMonth() - n);
    return end >= cutoff;
  };
  const daysAgo = (iso) => (Date.now() - new Date(iso + "T00:00:00+09:00").getTime()) / 864e5;

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
  function renderControls() {
    const groups = (TAX.groups || []).filter((g) => DATA.some((it) => it.groups.includes(g)));
    const gCount = (g) => DATA.filter((it) => (!g || it.groups.includes(g)) && matches(it, "group")).length;
    $("#groups").innerHTML = [["", "전체"], ...groups.map((g) => [g, g])]
      .map(([v, label]) => `<button data-group="${esc(v)}" class="${state.group === v ? "on" : ""}">${esc(label)}<span class="n">${gCount(v)}</span></button>`)
      .join("");

    const order = state.group ? TAX.divisions?.[state.group] || [] : Object.values(TAX.divisions || {}).flat();
    const present = new Set(DATA.filter((it) => !state.group || it.groups.includes(state.group)).flatMap((it) => it.divisions));
    const divs = [...order.filter((d) => present.has(d)), ...[...present].filter((d) => !order.includes(d)).sort()];
    const base = DATA.filter((it) => matches(it, "div"));
    $("#divisions").innerHTML = divs
      .map((d) => `<button class="chip ${state.divs.has(d) ? "on" : ""}" data-div="${esc(d)}">${esc(d)}<span class="n">${base.filter((it) => it.divisions.includes(d)).length}</span></button>`)
      .join("");

    for (const b of document.querySelectorAll("#views button")) b.classList.toggle("on", b.dataset.view === state.view);
    $("#q").value = state.q;
    $("#period").value = state.period;
    $("#sort").value = state.sort;
    $("#society").value = state.society;
    $("#newOnly").checked = state.newOnly;
    $("#koOnly").checked = state.koOnly;
  }

  // ── 렌더: 목록 ────────────────────────
  function itemHTML(it) {
    const main = it.sources[0];
    const isNew = !it.backfill && it.first_seen && daysAgo(it.first_seen) <= 14;
    const socs = it.sources
      .map((s) => `<a class="tag soc" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(SOCS[s.society]?.name)}${s.board_name ? " · " + esc(s.board_name) : ""}">${esc(SOCS[s.society]?.short || s.society)}${s.manual ? " ✎" : ""}</a>`)
      .join("");
    const files = it.sources.flatMap((s) => s.attachments || []).slice(0, 4)
      .map((f) => `<a class="file" href="${esc(f.url)}" target="_blank" rel="noopener" title="${esc(f.name)}">${esc(f.kind === "file" ? "첨부" : f.kind)}</a>`)
      .join("");
    const tags = (it.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("");
    const divs = it.divisions.map((d) => `<span class="tag div">${esc(d)}</span>`).join("");
    const note = dateNote(it);
    return `<div class="item">
      <div class="date">${esc(fmtDate(it))}${note ? `<span class="src">${esc(note)}</span>` : ""}</div>
      <div>
        ${isNew ? '<span class="badge-new">NEW</span>' : ""}<a class="title" href="${esc(main.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>
        <div class="meta">${socs}${divs}${tags}${files}</div>
      </div>
    </div>`;
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
    $("#count").textContent = `${list.length.toLocaleString()}건`;
    if (!list.length) {
      $("#results").innerHTML = `<div class="empty">조건에 맞는 진료지침이 없습니다.</div>`;
      return;
    }
    let budget = state.shown;
    const html = [];
    for (const [k, items] of groupBy(list)) {
      if (budget <= 0) break;
      const part = items.slice(0, budget);
      budget -= part.length;
      html.push(`<div class="group-h">${esc(k)}<span class="n">${items.length}건</span></div><div class="list">${part.map(itemHTML).join("")}</div>`);
    }
    const total = state.view === "timeline" ? list.length : groupBy(list).reduce((n, [, v]) => n + v.length, 0);
    if (state.shown < total) html.push(`<button class="more" id="more">더 보기 (${(total - state.shown).toLocaleString()}건 남음)</button>`);
    $("#results").innerHTML = html.join("");
    $("#more")?.addEventListener("click", () => { state.shown += PAGE * 2; renderList(); });
  }

  function render() {
    renderControls();
    renderList();
    writeHash();
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
        let badge = '<span class="st-warn">미수집</span>';
        if (s.enabled === false) badge = s.manual ? '<span class="st-ok">수동 등록</span>' : '<span class="st-warn">제외</span>';
        else if (r?.ok) badge = '<span class="st-ok">정상</span>';
        else if (r) badge = '<span class="st-err">확인 필요</span>';
        if (s.enabled === false && !s.manual) badge = '<span class="st-warn">제외</span>';
        const off = s.enabled === false;
        const boards = (off ? [] : r?.boards || [])
          .map((b) => `<div class="st-board">${b.source === "discovered" ? "자동탐색 · " : ""}<a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.name || b.url)}</a>${b.error ? " — " + esc(b.error) : ` — ${b.matched ?? 0}건`}</div>`)
          .join("");
        const err = !off && r?.error ? `<div class="st-board">${esc(r.error)}</div>` : "";
        return `<tr><td>${esc(s.division)}</td><td><a href="${esc(s.homepage)}" target="_blank" rel="noopener">${esc(s.name)}</a>${s.note ? `<div class="st-board">${esc(s.note)}</div>` : ""}</td>
          <td>${badge}${err}${boards}</td><td class="num">${count[s.id] || 0}</td><td class="st-board">${off ? "" : esc((r?.run_at || "").replace("T", " ").slice(0, 16))}</td></tr>`;
      })
      .join("");
    $("#status").innerHTML = `<table class="st"><thead><tr><th>분과</th><th>학회</th><th>상태 · 게시판</th><th>건수</th><th>마지막 수집</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="note">회원 전용으로 판정되어 제외된 게시물: ${(STATUS.hidden_members_only || 0).toLocaleString()}건 · 학회 배지의 ✎ 표시는 자동 수집이 막혀 손으로 등록한 항목입니다.</p>
      ${(STATUS.manual_errors || []).length ? `<p class="note st-err">수동 등록 오류: ${STATUS.manual_errors.map(esc).join(" / ")}</p>` : ""}`;
  }

  // ── 이벤트 ───────────────────────────
  function bind() {
    let t;
    $("#q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); state.shown = PAGE; render(); }, 150); });
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
    for (const b of document.querySelectorAll(".tabs button"))
      b.addEventListener("click", () => {
        for (const x of document.querySelectorAll(".tabs button")) x.setAttribute("aria-selected", x === b);
        $("#tab-list").hidden = b.dataset.tab !== "list";
        $("#tab-status").hidden = b.dataset.tab !== "status";
        if (b.dataset.tab === "status") renderStatus();
      });
    window.addEventListener("hashchange", () => { readHash(); render(); });
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
