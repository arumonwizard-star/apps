(async () => {
  // Reads the calendar's events through TimeTree's own web API (the same call its page makes) - one or two
  // requests - and keeps the ones of the next DAYS days (today included) that have a memo.
  const TODAY = "__TODAY__", DAYS = __N__, MINMEMO = 10;
  window.__ttCancel = false;
  const W = (window.__tt = {state: "run", items: [], msg: "", opened: 0, checked: [], t: Date.now()});
  try {
    for (let i = 0; i < 60 && !document.querySelector('meta[name="csrf-token"]'); i++) await new Promise(r => setTimeout(r, 100));
    const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
    const mod = (location.pathname.match(/\/calendars\/([^/?#]+)/) || [])[1];
    if (!mod) { W.state = "error"; W.msg = "カレンダーが開いていません"; return; }
    const ver = (document.querySelector('script[src*="main-"]') && "2.1.0") || "2.1.0";
    const H = {"Content-Type": "application/json", "X-TimeTreeA": "web/" + ver + "/ja", "X-CSRF-Token": csrf};
    const get = async p => { const r = await fetch(p, {credentials: "include", headers: H}); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); };
    const cals = (await get("/api/v1/calendars")).calendars || [];
    const cal = cals.find(c => c.alias_code === mod || String(c.id) === mod) || cals[0];
    if (!cal) { W.state = "error"; W.msg = "カレンダーが見つかりませんでした"; return; }
    const day = d => { const x = new Date(d.getTime()); return x.getFullYear() + "-" + String(x.getMonth() + 1).padStart(2, "0") + "-" + String(x.getDate()).padStart(2, "0"); };
    const t0 = new Date(TODAY + "T00:00:00");
    const keys = []; for (let i = 0; i < DAYS; i++) keys.push(day(new Date(t0.getFullYear(), t0.getMonth(), t0.getDate() + i)));
    const dayOf = e => e.all_day ? new Date(e.start_at).toISOString().slice(0, 10)
      : new Date(e.start_at).toLocaleDateString("sv-SE", {timeZone: e.start_timezone || "Asia/Tokyo"});
    const seen = new Set(); let items = [];
    // events are paged by their last-update time; a script is usually written recently, so look at the recent ones first
    for (const back of [45, 400, null]) {      // 45 days first (one request); older only if nothing was found
      let since = back === null ? 0 : Date.now() - back * 86400000, chunk = true, pages = 0;
      while (chunk && pages < 40) {
        const j = await get("/api/v1/calendar/" + cal.id + "/events?since=" + since);
        pages++; W.opened += (j.events || []).length;
        for (const e of (j.events || [])) {
          if (seen.has(e.id) || e.deactivated_at) continue;
          seen.add(e.id);
          const k = dayOf(e);
          if (keys.includes(k) && (e.note || "").trim().length >= MINMEMO) {
            const [, m, d] = k.split("-");
            items.push({date: +m + "/" + +d, iso: k, title: e.title || "", memo: e.note.trim()});
          }
        }
        since = j.since; chunk = !!j.chunk; W.items = items.slice();
        if (window.__ttCancel) break;
      }
      if (items.length || window.__ttCancel) break;
    }
    items.sort((a, b) => a.iso.localeCompare(b.iso));
    W.items = items; W.checked.push(items.length ? "" : "今日から" + DAYS + "日間にメモのある予定はありません"); W.state = "done";
  } catch (e) { W.state = "error"; W.msg = "タイムツリーを読み取れませんでした: " + String(e); }
})();
"started"
