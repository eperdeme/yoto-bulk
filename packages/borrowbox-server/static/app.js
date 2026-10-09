/**
 * Alternative frontend — task-oriented, built around one pipeline.
 *
 * Design premise
 * --------------
 * The existing UI exposes six sibling tabs, each a dump of one data source
 * (search / loans / history / archives / yoto cards / purchased). But a user
 * never wants "the archives list"; they want to get a book onto a Yoto card.
 * A single book moves through a pipeline:
 *
 *   findable → on loan → downloaded → in Audiobookshelf → pushed to Yoto
 *
 * and the API already carries the join keys to reconstruct that state
 * (`LoanInfo.in_abs` / `abs_item_id`, archive `in_abs`, `YotoCardSummary.
 * needs_push` / `source`). So this UI derives one state per book and organises
 * around the work, not the data source.
 *
 * "Today" is the home screen: everything stalled in the pipeline, grouped by
 * what action would unblock it. The other places are for browsing and setup.
 */
(function () {
  "use strict";

  const API = "";
  const YOTO = window.YOTO_API_URL || `${location.protocol}//${location.hostname}:8001`;

  // ── tiny helpers ────────────────────────────────────────────────────────
  const $ = sel => document.querySelector(sel);
  const el = (tag, attrs = {}, kids = []) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "html") n.innerHTML = v;
      else if (k === "dataset") Object.assign(n.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of [].concat(kids)) if (kid) n.append(kid);
    return n;
  };
  const fmtAuthors = a => (Array.isArray(a) ? a.filter(Boolean).join(", ") : a || "");
  const announce = msg => { const l = $("#live"); if (l) { l.textContent = ""; setTimeout(() => (l.textContent = msg), 40); } };

  function toast(message, tone = "info") {
    const host = $("#toasts");
    const node = el("div", { class: "toast", dataset: { tone }, text: String(message) });
    const x = el("button", { class: "toast-x", type: "button", "aria-label": "Dismiss", text: "✕" });
    const kill = () => node.remove();
    x.addEventListener("click", kill);
    node.append(x);
    host.append(node);
    if (tone !== "error") setTimeout(kill, 6000);
    announce(String(message));
  }

  function activeModal() {
    return [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')]
      .filter(node => !node.hidden && node.getClientRects().length).at(-1);
  }

  function syncModalState() {
    const modal = activeModal();
    $(".shell").inert = Boolean(modal);
    $("#drawer").inert = Boolean(modal && modal !== $("#drawer"));
    $("#tasks").inert = Boolean(modal);
    document.body.classList.toggle("modal-open", Boolean(modal));
    document.body.style.overflow = modal ? "hidden" : "";
  }

  function trapModalFocus(event) {
    if (event.key !== "Tab" || event.defaultPrevented) return;
    const modal = activeModal();
    if (!modal) return;
    const controls = [...modal.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')]
      .filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const target = event.shiftKey ? controls.at(-1) : controls[0];
    if (!target) { event.preventDefault(); return; }
    const edge = event.shiftKey ? controls[0] : controls.at(-1);
    if (document.activeElement === edge || !modal.contains(document.activeElement)) {
      event.preventDefault();
      target.focus();
    }
  }

  function confirmDialog({ title, text, confirmLabel = "Confirm", danger = false }) {
    return new Promise(resolve => {
      const scrim = el("div", { class: "confirm-scrim" });
      const yes = el("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "button", text: confirmLabel });
      const no = el("button", { class: "btn", type: "button", text: "Cancel" });
      const box = el("div", { class: "confirm", role: "dialog", "aria-modal": "true", "aria-label": title }, [
        el("h2", { class: "confirm-title", text: title }),
        el("p", { class: "confirm-text", text: text || "" }),
        el("div", { class: "confirm-actions" }, [no, yes]),
      ]);
      scrim.append(box);
      const prev = document.activeElement;
      const done = v => {
        scrim.remove();
        document.removeEventListener("keydown", onKey, true);
        syncModalState();
        if (prev?.isConnected) prev.focus();
        resolve(v);
      };
      const onKey = e => {
        if (e.key === "Escape") { e.stopPropagation(); done(false); }
      };
      yes.addEventListener("click", () => done(true));
      no.addEventListener("click", () => done(false));
      scrim.addEventListener("click", e => { if (e.target === scrim) done(false); });
      document.addEventListener("keydown", onKey, true);
      document.body.append(scrim);
      syncModalState();
      (danger ? no : yes).focus();
    });
  }

  async function getJSON(url, options = {}) {
    const timeout = AbortSignal.timeout(30000);
    const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    const res = await fetch(url, { ...options, signal });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
  }
  async function send(url, method = "POST", body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
    return data;
  }

  // ── state ───────────────────────────────────────────────────────────────
  const state = {
    libraries: [],
    credentials: null,  // {writable, libraries:[CredentialStatus]}
    duplicates: [],     // books cached more than once
    history: {},        // library -> LoanInfo[] (returned loans)
    purchased: [],      // Yoto family library (bought / shared cards)
    credits: {},        // library -> number
    loans: {},          // library -> LoanInfo[]
    archives: [],
    cards: [],          // YotoCardSummary[] (ABS-backed + yoto-sourced)
    orphans: [],
    yotoAuth: null,
    loadedAt: null,
    loading: true,
    errors: [],
    sources: {},
    syncStatus: {},
    yotoJobs: [],
    borrowboxJobs: [],
    syncingAbs: false,
  };

  const DATA_SOURCES = {
    libraries: { url: `${API}/api/catalog/libraries`, field: "libraries", label: "BorrowBox libraries" },
    loans: { url: `${API}/api/auth/all-loans`, field: "loans", label: "Loans" },
    archives: { url: `${API}/api/auth/archives`, field: "archives", label: "Import archive" },
    cards: { url: `${YOTO}/api/yoto/cards`, field: "cards", label: "Shelf" },
    orphans: { url: `${YOTO}/api/yoto/orphans`, field: "orphans", label: "Yoto-only cards", normalize: data => Array.isArray(data) ? data : data.orphans || [] },
    auth: { url: `${YOTO}/api/yoto/auth/status`, field: "yotoAuth", label: "Yoto connection" },
    credentials: { url: `${API}/api/credentials`, field: "credentials", label: "Library connections" },
    history: { url: `${API}/api/auth/all-history`, field: "history", label: "Loan history" },
    purchased: { url: `${YOTO}/api/yoto/family-library`, field: "purchased", label: "Purchased cards", normalize: data => Array.isArray(data) ? data : data.cards || [] },
    duplicates: { url: `${YOTO}/api/yoto/duplicates`, field: "duplicates", label: "Duplicate checks", normalize: data => data.groups || [] },
    syncStatus: { url: `${YOTO}/api/yoto/sync/status`, field: "syncStatus", label: "Sync timestamps" },
    yotoJobs: { url: `${YOTO}/api/yoto/jobs`, field: "yotoJobs", label: "Yoto tasks" },
    borrowboxJobs: { url: `${API}/api/jobs`, field: "borrowboxJobs", label: "Import tasks" },
  };

  const sourcePending = name => !state.sources[name]?.loadedAt && !state.sources[name]?.error;
  const sourceFailed = name => Boolean(state.sources[name]?.error);
  let renderScheduled = false;
  function scheduleRender() {
    if (renderScheduled) return;
    renderScheduled = true;
    setTimeout(() => { renderScheduled = false; render(); }, 0);
  }

  function sourceNotices(root, names) {
    for (const name of names) {
      const status = state.sources[name];
      if (!status || (!status.loading && !status.error)) continue;
      const source = DATA_SOURCES[name];
      const text = status.error
        ? `${source.label}: ${status.error}${status.loadedAt ? " (last loaded data retained)" : ""}`
        : `${status.loadedAt ? "Refreshing" : "Loading"} ${source.label.toLowerCase()}...`;
      const notice = el("div", { class: `source-notice${status.error ? " source-error" : ""}`, role: status.error ? "alert" : "status" }, [el("span", { text })]);
      if (status.error) notice.append(el("button", { class: "btn btn-sm", type: "button", text: "Retry", "aria-label": `Retry ${source.label}`, onclick: () => loadSource(name) }));
      root.append(notice);
    }
  }

  /** Days until an ISO-ish date string, or null if unparseable. */
  function daysUntil(dateStr) {
    if (!dateStr) return null;
    const t = Date.parse(dateStr);
    if (Number.isNaN(t)) return null;
    return Math.round((t - Date.now()) / 86400000);
  }

  const isAudio = fmt => {
    const f = String(fmt || "").toLowerCase();
    return !f || f.includes("audio");
  };

  // BorrowBox issues credits per loan format, not as a single number. The
  // audiobook allowance is the only one relevant here.
  const AUDIO_CREDIT_FORMAT = "eAudiobooks";

  /**
   * Normalise `GET /api/auth/credits/{library}`, which answers
   * `{library, credits: [{loanFormat, amount, granted}, ...]}`.
   */
  function parseCredits(payload) {
    const list = Array.isArray(payload) ? payload : payload?.credits;
    if (!Array.isArray(list) || list.length === 0) return { error: "unavailable" };

    const entry =
      list.find(c => c.loanFormat === AUDIO_CREDIT_FORMAT) ||
      list.find(c => /audio/i.test(c.loanFormat || ""));
    const breakdown = list
      .map(c => `${c.loanFormat || "unknown"}: ${c.amount}${c.granted !== undefined && c.granted !== null ? ` of ${c.granted}` : ""}`)
      .join("\n");

    if (!entry || typeof entry.amount !== "number") return { error: "no audiobook allowance", breakdown };
    return { value: entry.amount, format: entry.loanFormat, breakdown };
  }

  /**
   * Turn an upstream failure into something short and human.
   * Raw messages here are httpx errors that carry a full URL and an MDN link,
   * which must not be dumped into a chip.
   */
  function friendlyError(message) {
    const raw = String(message || "");
    if (/\b401\b/.test(raw)) return "sign-in rejected";
    if (/\b403\b/.test(raw)) return "access denied";
    if (/\b404\b/.test(raw)) return "not found";
    if (/\b50\d\b|bad gateway/i.test(raw)) return "library unreachable";
    if (/timeout|timed out/i.test(raw)) return "timed out";
    const first = raw.split("\n")[0].trim();
    if (!first) return "unavailable";
    return first.length > 48 ? `${first.slice(0, 45)}…` : first;
  }

  /**
   * Present a library's credit lookup as one of four distinct states.
   * "Loading" and "failed" must not look alike, or a library with expired
   * credentials appears to be permanently loading.
   */
  function creditInfo(library) {
    const c = state.credits[library];
    if (c === undefined) return { status: "loading", label: "checking…", title: "Checking remaining credits" };
    if (c.error) {
      const detail = c.detail || `Could not read credits for ${library}.`;
      return { status: "error", label: c.error, detail, title: detail };
    }
    const none = c.value === 0;
    return {
      status: none ? "empty" : "ok",
      label: none ? "no credits left" : `${c.value} credit${c.value === 1 ? "" : "s"}`,
      title: c.breakdown || `${c.value} borrows remaining`,
    };
  }

  // ── the derived work queue ──────────────────────────────────────────────
  /**
   * Build the prioritised list of stalled pipeline items.
   *
   * This is the heart of the redesign: rather than making the user visit six
   * tabs and mentally diff them, each group names a blockage and carries the
   * single action that clears it.
   */
  function buildQueue() {
    const groups = [];
    const allLoans = Object.entries(state.loans).flatMap(([library, items]) =>
      (items || []).map(l => ({ ...l, library }))
    );

    // 1. Loans expiring soon — genuinely time-critical, so it leads.
    const dueSoon = allLoans
      .filter(l => l.loan_type === "loan" && daysUntil(l.end_date) !== null && daysUntil(l.end_date) <= 3)
      .sort((a, b) => daysUntil(a.end_date) - daysUntil(b.end_date));
    if (dueSoon.length) {
      groups.push({
        key: "due",
        title: "Due back soon",
        note: "Download before the loan expires, or return it now to free a credit.",
        items: dueSoon.map(l => ({
          id: `loan:${l.library}:${l.loan_id}`,
          title: l.title,
          meta: `${fmtAuthors(l.authors)} · ${l.library}`,
          cover: l.cover_url,
          chip: { label: dueLabel(daysUntil(l.end_date)), tone: "due" },
          primary: l.in_abs
            ? { label: "Return", run: () => returnLoan(l), danger: true }
            : { label: "Download", run: () => downloadLoan(l) },
          book: l,
        })),
      });
    }

    // 2. Borrowed but not yet in Audiobookshelf.
    const toImport = allLoans.filter(
      l => l.loan_type === "loan" && l.loan_id && !l.in_abs && isAudio(l.format) && !dueSoon.includes(l)
    );
    if (toImport.length) {
      groups.push({
        key: "import",
        title: "Borrowed, not in your library",
        note: "Import into Audiobookshelf so the audio is yours to keep and push.",
        bulk: { label: `Import all (${toImport.length})`, run: () => importMany(toImport) },
        items: toImport.map(l => ({
          id: `imp:${l.library}:${l.loan_id}`,
          title: l.title,
          meta: `${fmtAuthors(l.authors)} · ${l.library}`,
          cover: l.cover_url,
          chip: { label: "On loan", tone: "loan" },
          queueKey: `import:${l.library}:${l.loan_id}`,
          primary: { label: "Import", run: btn => importLoan(l, btn) },
          book: l,
        })),
      });
    }

    // 3. Downloaded ZIP present but never imported — a retry surface.
    const staleArchives = state.archives.filter(a => !a.in_abs);
    if (staleArchives.length) {
      groups.push({
        key: "archive",
        title: "Downloaded, not imported",
        note: "The audio is already on disk, so importing will not re-download.",
        items: staleArchives.map(a => ({
          id: `arc:${a.library}:${a.loan_id}`,
          title: a.title || a.loan_id,
          meta: `${a.library}${a.size_mb ? ` · ${a.size_mb} MB` : ""}`,
          chip: { label: "Downloaded", tone: "downloaded" },
          queueKey: `import:${a.library}:${a.loan_id}`,
          primary: { label: "Import", run: btn => importArchive(a, btn) },
        })),
      });
    }

    // 4. In Audiobookshelf but the Yoto card is stale.
    const needsPush = state.cards.filter(c => c.needs_push);
    if (needsPush.length) {
      groups.push({
        key: "push",
        title: "Ready to push to Yoto",
        note: "These differ from what is on the card, or have never been pushed.",
        bulk: { label: `Push all (${needsPush.length})`, run: () => pushMany(needsPush) },
        items: needsPush.map(c => ({
          id: `push:${c.card_id}`,
          title: c.title,
          meta: [fmtAuthors(c.authors), c.series].filter(Boolean).join(" · "),
          cover: c.cover_url,
          chip: { label: "Needs push", tone: "needspush" },
          queueKey: `push:${c.card_id}`,
          primary: { label: "Push", run: btn => pushCard(c, btn) },
          card: c,
        })),
      });
    }

    // 5. On Yoto with no Audiobookshelf backing — reconcile or remove.
    if (state.orphans.length) {
      groups.push({
        key: "orphan",
        title: "On Yoto only",
        note: "Cards with no matching book in Audiobookshelf. Adopt them or remove them.",
        items: state.orphans.map(o => ({
          id: `orph:${o.card_id}`,
          title: o.title || o.card_id,
          meta: fmtAuthors(o.authors || o.author),
          cover: o.cover_url,
          chip: { label: "Orphan", tone: "orphan" },
          queueKey: `adopt:${o.card_id}`,
          primary: { label: "Adopt into ABS", run: btn => adoptOrphan(o, btn) },
          secondary: { label: "Remove", run: () => removeOrphan(o), danger: true },
        })),
      });
    }

    // 6. The same book cached twice — usually an ABS card and a pulled Yoto
    //    card the matcher would not join. Linking is non-destructive.
    const dupes = state.duplicates.filter(g => g.resolution !== "none");
    if (dupes.length) {
      groups.push({
        key: "duplicates",
        title: "Duplicate copies",
        note: "The same book is cached more than once. Linking a pair tells the app they are one book; nothing is deleted.",
        items: dupes.map(group => ({
          id: `dup:${group.key}`,
          title: group.title,
          meta: `${group.cards.length} copies · ${group.reason}`,
          cover: group.cards.find(c => c.cover_url)?.cover_url,
          chip: { label: DUPLICATE_CHIP[group.kind] || "Duplicate", tone: "orphan" },
          primary:
            group.resolution === "link"
              ? { label: "Link as one", run: btn => linkDuplicate(group, btn) }
              : null,
          duplicate: group,
        })),
      });
    }

    // 7. Reservations, informational.
    const reserved = allLoans.filter(l => l.loan_type === "reserve");
    if (reserved.length) {
      groups.push({
        key: "reserved",
        title: "Reserved",
        note: "Waiting on availability. Nothing to do yet.",
        items: reserved.map(l => ({
          id: `res:${l.library}:${l.product_id}`,
          title: l.title,
          meta: `${fmtAuthors(l.authors)} · ${l.library}`,
          cover: l.cover_url,
          chip: { label: l.end_date ? `Ready ${l.end_date}` : "Waiting", tone: "reserved" },
          book: l,
        })),
      });
    }

    return groups;
  }

  const dueLabel = d => (d < 0 ? `${Math.abs(d)}d overdue` : d === 0 ? "Due today" : `${d}d left`);

  const DUPLICATE_CHIP = {
    unlinked_pair: "Not linked",
    duplicate_on_yoto: "Two Yoto cards",
    duplicate_in_audiobookshelf: "Two ABS copies",
  };

  /**
   * Join an ABS card and a Yoto card that are the same book.
   * Non-destructive: it records the relationship the matcher failed to spot.
   */
  async function linkDuplicate(group, btn) {
    if (!group.abs_card_id || !group.yoto_card_id) {
      toast("This duplicate cannot be linked automatically.", "error");
      return;
    }
    await withButton(btn, "Linking…", async () => {
      await send(`${YOTO}/api/yoto/duplicates/link`, "POST", {
        abs_card_id: group.abs_card_id,
        yoto_card_id: group.yoto_card_id,
      });
      toast(`“${group.title}” is now one book`, "ok");
      await loadAll();
    });
  }

  const actionableCount = () =>
    buildQueue()
      .filter(g => !["reserved"].includes(g.key))
      .reduce((n, g) => n + g.items.length, 0);

  // ── data loading ────────────────────────────────────────────────────────
  /**
   * Refresh that actually refreshes.
   *
   * Loan data is cached server-side, so re-fetching alone can return the same
   * stale answer for up to an hour — a loan returned elsewhere would keep
   * showing. Drop the per-library cache first, as the previous UI did.
   */
  async function hardRefresh(btn) {
    failedCovers.clear();
    const original = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "Refreshing…"; }
    await Promise.allSettled(
      state.libraries.map(lib =>
        fetch(`${API}/api/auth/cache/loans/${encodeURIComponent(lib.name)}`, { method: "DELETE" }).catch(
          () => {}
        )
      )
    );
    await loadAll();
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }

  async function refreshCredits() {
    await Promise.allSettled(state.libraries.map(async library => {
      try {
        state.credits[library.name] = parseCredits(await getJSON(`${API}/api/auth/credits/${encodeURIComponent(library.name)}`));
      } catch (err) {
        state.credits[library.name] = { error: friendlyError(err.message), detail: String(err.message || "").split("\n")[0] };
      }
      scheduleRender();
    }));
  }

  async function loadSource(name) {
    const source = DATA_SOURCES[name];
    const status = state.sources[name] ||= { loading: false, error: null, loadedAt: null };
    if (status.request) return status.request;
    status.loading = true;
    status.error = null;
    scheduleRender();
    status.request = (async () => {
      try {
        const data = await getJSON(source.url);
        state[source.field] = source.normalize ? source.normalize(data) : data;
        if (name === "yotoJobs" || name === "borrowboxJobs") reconcileJobs();
        status.loadedAt = new Date();
        if (!["syncStatus", "yotoJobs", "borrowboxJobs"].includes(name)) state.loadedAt = status.loadedAt;
        if (name === "libraries") refreshCredits();
      } catch (err) {
        status.error = err.name === "TimeoutError" ? "Request timed out" : err.message;
      } finally {
        status.loading = false;
        status.request = null;
        state.loading = Object.values(state.sources).some(item => item.loading);
        state.errors = Object.entries(state.sources).filter(([, item]) => item.error).map(([key, item]) => `${DATA_SOURCES[key].label}: ${item.error}`);
        scheduleRender();
      }
    })();
    return status.request;
  }

  async function loadAll() {
    await Promise.allSettled(Object.keys(DATA_SOURCES).map(loadSource));
  }

  // ── task queue ──────────────────────────────────────────────────────────
  /**
   * Serial queue for long-running operations, with a visible task list.
   *
   * Previously each push ran independently and called loadAll() on success. Hit
   * five push buttons and you got five parallel requests, and the first to
   * finish re-rendered the whole view — wiping the other four buttons' "Pushing…"
   * state mid-flight, four more times over. There was no way to tell what had
   * finished.
   *
   * Now every unit of work is a job: queued, run one at a time, each with its own
   * visible status, and the data refresh happens exactly once after the queue
   * drains.
   */
  const jobs = { items: [], seq: 0, dismissTimer: null, pollTimer: null, polling: false };
  let dismissedJobs = new Set();
  try { dismissedJobs = new Set(JSON.parse(localStorage.getItem("dismissed-workflows") || "[]")); } catch {}

  const JOB_ACTIVE = new Set(["queued", "running"]);

  /** True when a job for this key is already queued or running. */
  function isQueued(key) {
    return jobs.items.some(j => j.key === key && JOB_ACTIVE.has(j.status));
  }

  /**
   * Add work to the queue.
   *
   * @param {{label: string, group: string, key?: string, run: Function}} spec
   * @returns the job, or null if an identical one is already pending
   */
  const workflowKey = spec => spec.action === "import"
    ? `import:${spec.library}:${spec.loan_id}`
    : spec.action === "sync" || spec.action === "pull" ? spec.action : `${spec.action}:${spec.card_id}`;

  function rememberDismissal(id) {
    dismissedJobs.add(id);
    dismissedJobs = new Set([...dismissedJobs].slice(-200));
    try { localStorage.setItem("dismissed-workflows", JSON.stringify([...dismissedJobs])); } catch {}
  }

  function reconcileJobs() {
    const old = new Map(jobs.items.map(job => [job.id, job.status]));
    const remote = [
      ...state.yotoJobs.map(job => ({ ...job, id: `yoto:${job.id}`, service: "yoto", key: workflowKey(job.spec) })),
      ...state.borrowboxJobs.map(job => ({ ...job, id: `borrowbox:${job.id}`, service: "borrowbox", key: workflowKey(job.spec) })),
    ].filter(job => !dismissedJobs.has(job.id));
    const local = jobs.items.filter(job => job.localOnly && !remote.some(item => item.key === job.key));
    jobs.items = [...local, ...remote].sort((first, second) => (first.created_at || "").localeCompare(second.created_at || ""));
    state.syncingAbs = jobs.items.some(job => job.spec?.action === "sync" && JOB_ACTIVE.has(job.status));
    if (remote.some(job => old.has(job.id) && JOB_ACTIVE.has(old.get(job.id)) && !JOB_ACTIVE.has(job.status))) {
      ["cards", "loans", "archives", "orphans", "syncStatus"].forEach(loadSource);
    }
    renderTasks();
    if (jobs.items.length && jobs.items.every(job => job.status === "done")) {
      if (!jobs.dismissTimer) jobs.dismissTimer = setTimeout(() => { clearTasks(); jobs.dismissTimer = null; }, 8000);
    } else {
      clearTimeout(jobs.dismissTimer);
      jobs.dismissTimer = null;
    }
  }

  async function pollJobs() {
    if (jobs.polling) return;
    jobs.polling = true;
    clearTimeout(jobs.pollTimer);
    try {
      await Promise.allSettled([loadSource("yotoJobs"), loadSource("borrowboxJobs")]);
    } finally {
      jobs.polling = false;
      jobs.pollTimer = setTimeout(pollJobs, jobs.items.some(job => JOB_ACTIVE.has(job.status)) ? 1500 : 15000);
    }
  }

  async function startWorkflow(spec, service = "yoto") {
    const key = workflowKey(spec);
    if (key && isQueued(key)) return null;
    const job = { id: `submitting:${++jobs.seq}`, spec, service, key, label: spec.label || spec.action, group: "Submitting task", stage: "Submitting", status: "queued", localOnly: true, created_at: new Date().toISOString() };
    jobs.items.push(job);
    renderTasks();
    scheduleRender();
    try {
      const data = await send(service === "borrowbox" ? `${API}/api/jobs` : `${YOTO}/api/yoto/jobs`, "POST", spec);
      Object.assign(job, data, { id: `${service}:${data.id}`, service, key, localOnly: false });
      const field = service === "borrowbox" ? "borrowboxJobs" : "yotoJobs";
      state[field] = [...state[field].filter(item => item.id !== data.id), data];
      reconcileJobs();
      pollJobs();
      if (!JOB_ACTIVE.has(data.status)) ["cards", "loans", "archives", "syncStatus"].forEach(loadSource);
      return job;
    } catch (err) {
      job.status = "error";
      job.stage = "Submission failed";
      job.error = err.message;
      renderTasks();
      toast(`Task could not be submitted: ${err.message}`, "error");
      return null;
    } finally {
      state.syncingAbs = jobs.items.some(item => item.spec?.action === "sync" && JOB_ACTIVE.has(item.status));
      scheduleRender();
    }
  }

  function clearTasks() {
    window.clearTimeout(jobs.dismissTimer);
    jobs.items.filter(job => !JOB_ACTIVE.has(job.status)).forEach(job => rememberDismissal(job.id));
    jobs.items = jobs.items.filter(j => JOB_ACTIVE.has(j.status));
    renderTasks();
  }

  const JOB_LABELS = {
    queued: { text: "Queued", tone: "plain" },
    running: { text: "Working…", tone: "loan" },
    done: { text: "Done", tone: "synced" },
    error: { text: "Failed", tone: "orphan" },
  };

  function renderTasks() {
    const host = $("#tasks");
    if (!host) return;
    const expanded = new Set([...host.querySelectorAll(".task-details[open]")].map(details => details.closest(".task").dataset.jobId));
    const focusedRetry = host.contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") : null;

    if (!jobs.items.length) {
      host.hidden = true;
      host.innerHTML = "";
      return;
    }

    const total = jobs.items.length;
    const done = jobs.items.filter(j => j.status === "done").length;
    const failed = jobs.items.filter(j => j.status === "error").length;
    const settled = done + failed;
    const active = settled < total;
    const percent = total ? Math.round((settled / total) * 100) : 0;
    // All jobs in a batch share a group label ("Pushing to Yoto").
    const groups = new Set(jobs.items.map(job => job.group));
    const group = groups.size === 1 ? jobs.items[0].group : "Background tasks";

    host.hidden = false;
    host.setAttribute("aria-busy", String(active));
    host.innerHTML = "";

    const heading = active
      ? `${group} · ${settled} of ${total}`
      : failed
        ? `${group} · ${done} done, ${failed} failed`
        : `${group} · all ${total} done`;

    const head = el("div", { class: "tasks-head" }, [
      el("span", { class: "tasks-title", text: heading }),
    ]);
    if (!active) {
      const close = el("button", {
        class: "btn btn-icon btn-ghost",
        type: "button",
        "aria-label": "Dismiss task list",
        text: "✕",
      });
      close.addEventListener("click", clearTasks);
      head.append(close);
    }

    // Single live region for the summary; per-row updates would be too chatty.
    const status = el("div", { class: "sr-only", role: "status", "aria-live": "polite", text: heading });

    const track = el("div", { class: "tasks-track" }, [
      el("div", {
        class: `tasks-fill${failed && !active ? " has-failures" : ""}`,
        role: "progressbar",
        "aria-valuemin": "0",
        "aria-valuemax": "100",
        "aria-valuenow": String(percent),
        "aria-valuetext": heading,
        style: `width:${percent}%`,
      }),
    ]);

    const list = el("ul", { class: "tasks-list" });
    jobs.items.forEach(job => {
      const meta = JOB_LABELS[job.status];
      const stage = job.stage || meta.text;
      const progress = job.progress?.tracks_total ? `${job.progress.tracks_done || 0}/${job.progress.tracks_total} tracks` : "";
      const row = el("li", { class: `task task-${job.status}`, dataset: { jobId: job.id } }, [
        el("div", { class: "task-row" }, [
          el("span", { class: "task-label", title: job.label, text: job.label }),
          el("span", {
            class: `chip chip-${meta.tone}`,
            title: job.error || stage,
            text: meta.text,
          }),
        ]),
        el("div", { class: "task-stage", text: [stage, progress].filter(Boolean).join(" · ") }),
      ]);
      if (job.error) {
        const details = el("details", { class: "task-details", open: expanded.has(job.id) }, [el("summary", { text: "Failure details" }), el("p", { text: job.error })]);
        details.append(el("button", { class: "btn btn-sm", type: "button", text: "Retry", "aria-label": `Retry ${job.label}`, onclick: () => {
          rememberDismissal(job.id);
          jobs.items = jobs.items.filter(item => item !== job);
          startWorkflow(job.spec, job.service);
        } }));
        row.append(details);
      }
      list.append(row);
    });

    host.append(head, status, track, list);
    if (focusedRetry) [...host.querySelectorAll("button[aria-label]")].find(button => button.getAttribute("aria-label") === focusedRetry)?.focus({ preventScroll: true });
  }

  /** Keep failure text short enough for a chip; full text goes in the tooltip. */
  function shortError(message) {
    const first = String(message).split("\n")[0].trim();
    return first.length > 32 ? `${first.slice(0, 29)}…` : first;
  }

  // ── actions ─────────────────────────────────────────────────────────────
  async function withButton(btn, label, fn) {
    const original = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = label; }
    try {
      await fn();
    } catch (err) {
      toast(err.message, "error");
      if (btn) { btn.disabled = false; btn.textContent = original; }
      throw err;
    }
  }

  /** Import is the slowest operation here, so it always shows in the task list. */
  function importLoan(loan, btn) {
    if (isQueued(`import:${loan.library}:${loan.loan_id}`)) {
      toast(`“${loan.title}” is already queued.`, "info");
      return;
    }
    startWorkflow({ action: "import", library: loan.library, loan_id: loan.loan_id, product_id: loan.product_id || null, label: loan.title || loan.loan_id }, "borrowbox");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Queued";
    }
  }

  async function importMany(loans) {
    const ok = await confirmDialog({
      title: `Import ${loans.length} audiobook${loans.length === 1 ? "" : "s"}?`,
      text: "Each is downloaded from BorrowBox and added to Audiobookshelf, one at a time. Progress is shown as it goes.",
      confirmLabel: "Start import",
    });
    if (!ok) return;
    loans.forEach(loan => importLoan(loan, null));
  }

  function importArchive(arc, btn) {
    // Archives share the import endpoint, so they share the queue and group.
    importLoan({ library: arc.library, loan_id: arc.loan_id, title: arc.title || arc.loan_id }, btn);
  }

  async function downloadLoan(loan) {
    try {
      const data = await getJSON(
        `${API}/api/auth/download/${encodeURIComponent(loan.library)}/${encodeURIComponent(loan.loan_id)}` +
          `?total_tracks=${encodeURIComponent(loan.total_tracks || 1)}`
      );
      if (data.download_url) {
        window.open(data.download_url, "_blank", "noopener");
        toast("Download opened in a new tab", "ok");
      } else {
        toast("No download link returned", "error");
      }
    } catch (err) {
      toast(`Download failed: ${err.message}`, "error");
    }
  }

  async function returnLoan(loan) {
    const ok = await confirmDialog({
      title: "Return this loan?",
      text: `“${loan.title}” goes back to ${loan.library} and a credit is freed.`,
      confirmLabel: "Return loan",
      danger: true,
    });
    if (!ok) return;
    try {
      const data = await send(`${API}/api/auth/return`, "POST", {
        product_id: loan.product_id,
        loan_id: loan.loan_id,
        library: loan.library,
      });
      if (data.success) toast(`Returned “${loan.title}”`, "ok");
      else toast(data.message || "Return failed", "error");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /** The single push operation. Everything else queues this. */
  function pushCard(card, btn) {
    if (isQueued(`push:${card.card_id}`)) {
      toast(`“${card.title}” is already queued.`, "info");
      return;
    }
    startWorkflow({ action: "push", card_id: card.card_id, label: card.title || card.card_id });
    // The button reflects the queue rather than a private in-flight flag, so a
    // refresh part-way through a batch cannot leave it stuck on "Pushing…".
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Queued";
    }
  }

  async function pushMany(cards) {
    const ok = await confirmDialog({
      title: `Push ${cards.length} card${cards.length === 1 ? "" : "s"} to Yoto?`,
      text: "Each card is created or updated on your Yoto account, one at a time. Progress is shown as it goes.",
      confirmLabel: "Push all",
    });
    if (!ok) return;
    // Enqueued individually so the task list names each book.
    cards.forEach(card => pushCard(card, null));
  }

  function adoptOrphan(orphan, btn) {
    // Also queued: adopting pulls audio into Audiobookshelf and is slow enough
    // to want progress, and several can be adopted in a row.
    if (isQueued(`adopt:${orphan.card_id}`)) {
      toast(`“${orphan.title}” is already queued.`, "info");
      return;
    }
    startWorkflow({ action: "adopt", card_id: orphan.card_id, label: orphan.title || orphan.card_id });
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Queued";
    }
  }

  async function removeOrphan(orphan) {
    const ok = await confirmDialog({
      title: "Remove from Yoto?",
      text: `“${orphan.title || orphan.card_id}” is deleted from Yoto. This cannot be undone.`,
      confirmLabel: "Remove card",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`${YOTO}/api/yoto/orphans/${encodeURIComponent(orphan.card_id)}`, "DELETE");
      toast("Card removed", "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function borrow(productId, library, btn) {
    await withButton(btn, "Borrowing…", async () => {
      const data = await send(`${API}/api/auth/borrow`, "POST", { product_id: productId, library });
      if (!data.success) throw new Error(data.message || "Borrow failed");
      toast("Borrowed. It will appear in Today, ready to import.", "ok");
      await loadAll();
    });
  }

  async function reserve(productId, library, btn) {
    await withButton(btn, "Reserving…", async () => {
      const data = await send(`${API}/api/auth/reserve`, "POST", { product_id: productId, library });
      if (!data.success) throw new Error(data.message || "Reserve failed");
      toast("Reserved.", "ok");
      await loadAll();
    });
  }

  // ── shared row component ────────────────────────────────────────────────
  const failedCovers = new Set();
  function coverNode(url, glyph = "♪") {
    const placeholder = () => el("div", { class: "cover cover-placeholder", "aria-hidden": "true", title: url ? "Cover unavailable" : "No cover", text: glyph });
    if (!url || failedCovers.has(url)) return placeholder();
    const image = el("img", { class: "cover", src: url, alt: "", loading: "lazy" });
    image.addEventListener("error", () => { failedCovers.add(url); image.replaceWith(placeholder()); });
    return image;
  }

  function rowNode(item) {
    const actions = el("div", { class: "row-actions" });
    if (item.primary) {
      // If work for this row is already in the queue, say so instead of
      // offering a button that would be rejected as a duplicate.
      const pending = item.queueKey && isQueued(item.queueKey);
      const b = el("button", {
        class: "btn btn-sm btn-primary",
        type: "button",
        text: pending ? "Queued" : item.primary.label,
        disabled: pending || null,
      });
      if (!pending) {
        b.addEventListener("click", e => { e.stopPropagation(); item.primary.run(b); });
      }
      actions.append(b);
    }
    if (item.secondary) {
      const b = el("button", {
        class: `btn btn-sm ${item.secondary.danger ? "btn-danger" : ""}`,
        type: "button",
        text: item.secondary.label,
      });
      b.addEventListener("click", e => { e.stopPropagation(); item.secondary.run(b); });
      actions.append(b);
    }

    const titleLine = el("div", { class: "row-title-line" }, [
      el("span", { class: "row-title", text: item.title || "Untitled" }),
      item.typeLabel ? el("span", { class: "chip chip-podcast", text: item.typeLabel }) : null,
    ]);
    const main = el("div", { class: "row-main" }, [
      titleLine,
      el("div", { class: "row-meta", text: item.meta || "" }),
    ]);

    // Only cards have a detail view worth opening.
    const openable = Boolean(item.card);
    const mainWrap = openable
      ? el("button", { class: "row-open", type: "button", "aria-label": `Details for ${item.title}`, onclick: () => openDrawer(item.card) }, [main])
      : main;

    const selection = item.selectable ? el("input", { class: "row-select", type: "checkbox", "aria-label": `Select ${item.title}`, checked: item.selected }) : null;
    if (selection) selection.addEventListener("change", () => item.selectable(selection.checked));
    return el("div", { class: `row${selection ? " row-selectable" : ""}` }, [
      selection,
      coverNode(item.cover),
      mainWrap,
      item.chip ? el("span", { class: `chip chip-${item.chip.tone}`, text: item.chip.label }) : el("span"),
      actions,
    ]);
  }

  function groupNode(group) {
    const head = el("div", { class: "group-head" }, [
      el("h2", { class: "group-title", text: group.title }),
      el("span", { class: "chip chip-plain", text: String(group.items.length) }),
      el("span", { class: "group-rule" }),
    ]);
    if (group.bulk) {
      const b = el("button", { class: "btn btn-sm", type: "button", text: group.bulk.label });
      b.addEventListener("click", () => group.bulk.run(b));
      head.append(b);
    }
    return el("section", { class: "group" }, [
      head,
      group.note ? el("p", { class: "group-note", style: "margin-bottom:12px", text: group.note }) : null,
      el("div", { class: "rows" }, group.items.map(rowNode)),
    ]);
  }

  function emptyNode({ glyph = "◍", title, text, actions = [] }) {
    return el("div", { class: "empty" }, [
      el("div", { class: "empty-glyph", "aria-hidden": "true", text: glyph }),
      el("div", { class: "empty-title", text: title }),
      text ? el("div", { class: "empty-text", text }) : null,
      actions.length
        ? el(
            "div",
            { class: "empty-actions" },
            actions.map(a => el("button", { class: "btn btn-primary", type: "button", text: a.label, onclick: a.run }))
          )
        : null,
    ]);
  }

  function skeleton(n = 4) {
    return el("div", {}, Array.from({ length: n }, () => el("div", { class: "skeleton skeleton-row" })));
  }

  function pageHead(title, sub, aside) {
    return el("header", { class: "page-head" }, [
      el("div", { class: "page-head-row" }, [
        el("div", {}, [el("h1", { class: "page-title", text: title }), sub ? el("p", { class: "page-sub", text: sub }) : null]),
        aside || null,
      ]),
    ]);
  }

  // ── views ───────────────────────────────────────────────────────────────
  function viewToday(root) {
    root.append(pageHead("Today", "Everything waiting on you, in the order it matters."));

    const queueSources = ["loans", "archives", "cards", "orphans", "duplicates"];
    sourceNotices(root, queueSources);
    if (queueSources.every(sourcePending)) return root.append(skeleton(5));

    if (state.errors.length) {
      root.append(
        el("div", { class: "panel", style: "border-color:var(--danger);margin-bottom:24px" }, [
          el("div", { class: "panel-title", text: "Some data could not be loaded" }),
          el("p", { class: "panel-note", text: state.errors.join(" · ") }),
        ])
      );
    }

    // A library whose credentials are rejected returns no loans at all, so the
    // queue below would silently under-report. Say so rather than looking
    // complete when it is not.
    const brokenLibs = state.libraries.filter(l => creditInfo(l.name).status === "error").map(l => l.name);
    if (brokenLibs.length) {
      root.append(
        el("div", { class: "panel", style: "border-color:var(--warn);margin-bottom:24px" }, [
          el("div", { class: "panel-title", text: `${brokenLibs.length} library sign-in failing` }),
          el("p", {
            class: "panel-note",
            text: `${brokenLibs.join(", ")} rejected the stored credentials, so loans from ${brokenLibs.length === 1 ? "it are" : "them are"} missing below.`,
          }),
          el("div", { style: "margin-top:12px" }, [
            el("button", { class: "btn btn-sm", type: "button", text: "Open settings", onclick: () => (location.hash = "#/settings") }),
          ]),
        ])
      );
    }

    // At-a-glance pipeline counts, each a shortcut into the work.
    const allLoans = Object.values(state.loans).flat();
    root.append(
      el("div", { class: "stat-grid", style: "margin-bottom:32px" }, [
        statNode("On loan", allLoans.filter(l => l.loan_type === "loan").length),
        statNode("Not imported", state.archives.filter(a => !a.in_abs).length, "warn"),
        statNode("In library", state.cards.length),
        statNode("Needs push", state.cards.filter(c => c.needs_push).length, "warn"),
        statNode("Yoto only", state.orphans.length, state.orphans.length ? "danger" : null),
        statNode("Duplicates", state.duplicates.length, state.duplicates.length ? "warn" : null),
      ])
    );

    const groups = buildQueue();
    if (!groups.length) {
      if (queueSources.some(name => sourcePending(name) || sourceFailed(name))) return;
      return root.append(
        emptyNode({
          glyph: "✓",
          title: "All caught up",
          text: "Nothing is stalled in the pipeline. Find something new to borrow, or browse your shelf.",
          actions: [{ label: "Find a book", run: () => (location.hash = "#/find") }],
        })
      );
    }
    root.append(el("div", { class: "queue" }, groups.map(groupNode)));
  }

  function statNode(label, value, tone) {
    return el("div", { class: "stat", dataset: tone ? { tone } : {} }, [
      el("div", { class: "stat-value", text: String(value) }),
      el("div", { class: "stat-label", text: label }),
    ]);
  }

  // ── Find ────────────────────────────────────────────────────────────────
  const findState = { query: "", results: null, busy: false, error: null };

  function viewFind(root) {
    root.append(
      pageHead(
        "Find",
        state.libraries.length
          ? `Searches all ${state.libraries.length} ${state.libraries.length === 1 ? "library" : "libraries"} at once and shows where each title is free.`
          : "No libraries are configured yet. Add BorrowBox credentials in Settings."
      )
    );
    sourceNotices(root, ["libraries"]);

    const input = el("input", {
      class: "input",
      id: "q",
      type: "search",
      placeholder: "Title, author, or series…",
      autocomplete: "off",
      value: findState.query,
    });
    const form = el("form", { class: "search-hero", role: "search" }, [
      el("div", { class: "field" }, [el("span", { class: "field-icon", "aria-hidden": "true", text: "⌕" }), input]),
      el("button", { class: "btn btn-primary", type: "submit", text: "Search" }),
    ]);
    form.append(el("label", { class: "sr-only", for: "q", text: "Search audiobooks" }));
    form.addEventListener("submit", e => {
      e.preventDefault();
      runSearch(input.value);
    });
    root.append(form);

    // Credits are decision-relevant here: you cannot borrow without one.
    if (state.libraries.length) {
      root.append(
        el(
          "div",
          { class: "avail", style: "margin-bottom:24px" },
          state.libraries.map(lib =>
            el("span", {
              class: "avail-cell",
              dataset: creditInfo(lib.name).status === "ok" ? { status: "available" } : {},
              title: creditInfo(lib.name).title,
              text: `${lib.name}: ${creditInfo(lib.name).label}`,
            })
          )
        )
      );
    }

    const out = el("div", { id: "find-results" });
    root.append(out);
    renderFindResults(out);
  }

  async function runSearch(q) {
    findState.query = q.trim();
    if (!findState.query) return;
    findState.busy = true;
    findState.error = null;
    renderFindResults($("#find-results"));
    try {
      findState.results = await getJSON(`${API}/api/catalog/search-all?q=${encodeURIComponent(findState.query)}`);
      announce(`${findState.results.length} results for ${findState.query}`);
    } catch (err) {
      findState.error = err.message;
      findState.results = null;
    }
    findState.busy = false;
    renderFindResults($("#find-results"));
  }

  function renderFindResults(out) {
    if (!out) return;
    out.innerHTML = "";
    if (findState.busy) return out.append(skeleton(4));
    if (findState.error) {
      return out.append(emptyNode({ glyph: "!", title: "Search failed", text: findState.error }));
    }
    if (findState.results === null) {
      return out.append(
        emptyNode({
          glyph: "⌕",
          title: "Search to begin",
          text: "One query checks every library, so you can borrow from whichever has it free.",
        })
      );
    }
    if (!findState.results.length) {
      return out.append(emptyNode({ glyph: "∅", title: "No matches", text: `Nothing found for “${findState.query}”.` }));
    }

    const rows = el("div", { class: "rows" });
    for (const item of findState.results) {
      const libs = Object.entries(item.libraries || {});
      const matrix = el(
        "div",
        { class: "avail" },
        libs.map(([name, info]) =>
          el("span", {
            class: "avail-cell",
            dataset: { status: info.status },
            title: `${name}: ${info.status}${info.available_date ? ` — ${info.available_date}` : ""}`,
            text: info.status === "available" ? name : `${name} ${info.available_date || "—"}`,
          })
        )
      );

      // One decisive action: borrow where it is free, else reserve the soonest.
      const best = item.best_library;
      const actions = el("div", { class: "row-actions" });
      if (best && item.best_status === "available" && isAudio(item.format)) {
        const b = el("button", { class: "btn btn-sm btn-primary", type: "button", text: `Borrow · ${best}` });
        b.addEventListener("click", () => borrow(item.product_id, best, b));
        actions.append(b);
      } else if (best && item.best_status === "on_loan") {
        const b = el("button", { class: "btn btn-sm", type: "button", text: `Reserve · ${best}` });
        b.addEventListener("click", () => reserve(item.product_id, best, b));
        actions.append(b);
      }

      rows.append(
        el("div", { class: "row" }, [
          coverNode(item.cover_url, "▤"),
          el("div", { class: "row-main" }, [
            el("div", { class: "row-title", text: item.title }),
            el("div", { class: "row-meta", text: [fmtAuthors(item.authors), item.series_name].filter(Boolean).join(" · ") }),
            matrix,
          ]),
          el("span"),
          actions,
        ])
      );
    }
    out.append(rows);
  }

  // ── Shelf ───────────────────────────────────────────────────────────────
  const shelfState = { filter: "all", query: "", type: "all", sort: "title", selected: new Set() };

  const stampTime = value => value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not recorded";
  const cardTime = card => Number(card.updated_at) || Date.parse(card.updated_at || "") || 0;
  function sortedShelfCards() {
    return state.cards.filter(shelfMatches).sort((first, second) => {
      if (shelfState.sort === "recent") return cardTime(second) - cardTime(first) || first.title.localeCompare(second.title);
      if (shelfState.sort === "series") return (first.series || "\uffff").localeCompare(second.series || "\uffff") || (first.sequence_number ?? Infinity) - (second.sequence_number ?? Infinity) || first.title.localeCompare(second.title);
      return first.title.localeCompare(second.title, undefined, { numeric: true, sensitivity: "base" });
    });
  }

  function viewShelf(root) {
    root.append(pageHead("Shelf", "Audiobookshelf books and podcasts.", el("button", {
      class: "btn btn-primary", type: "button", text: state.syncingAbs ? "Syncing..." : "Sync from Audiobookshelf",
      disabled: state.syncingAbs, onclick: () => syncAbs(),
    })));
    root.append(el("p", { class: "sync-stamps", text: `ABS sync: ${stampTime(state.syncStatus.last_abs_sync)} · Yoto push: ${stampTime(state.syncStatus.last_yoto_push)}` }));

    sourceNotices(root, ["cards"]);
    if (sourcePending("cards") && !state.cards.length) return root.append(skeleton(6));
    if (!state.cards.length) {
      if (sourceFailed("cards")) return;
      return root.append(
        emptyNode({
          glyph: "▤",
          title: "Your shelf is empty",
          text: "Sync from Audiobookshelf to populate it, or borrow something first.",
        })
      );
    }

    const counts = {
      all: state.cards.length,
      needspush: state.cards.filter(c => c.needs_push).length,
      synced: state.cards.filter(c => !c.needs_push).length,
      noseries: state.cards.filter(c => !c.series).length,
    };
    const labels = { all: "All", needspush: "Needs Yoto", synced: "Yoto up to date", noseries: "No series" };

    const segs = el("div", { class: "segments" });
    for (const key of ["all", "needspush", "synced", "noseries"]) {
      const b = el("button", {
        class: "segment",
        type: "button",
        "aria-pressed": String(shelfState.filter === key),
      }, [el("span", { text: labels[key] }), el("span", { class: "segment-count", text: String(counts[key]) })]);
      b.addEventListener("click", () => {
        shelfState.filter = key;
        render();
      });
      segs.append(b);
    }

    const search = el("input", { class: "input input-plain", id: "shelf-query", "aria-label": "Filter shelf", type: "search", placeholder: "Filter shelf…", value: shelfState.query, style: "max-width:260px" });
    let t = null;
    search.addEventListener("input", () => {
      // Debounced and applied by re-filtering data, never by rebuilding the
      // input, so the caret stays where the user put it.
      clearTimeout(t);
      const v = search.value;
      t = setTimeout(() => {
        shelfState.query = v;
        renderShelfRows();
      }, 120);
    });

    const mode = el("select", { id: "shelf-type", "aria-label": "Media type" }, [
      el("option", { value: "all", text: "Books and podcasts" }),
      el("option", { value: "audiobook", text: "Audiobooks" }),
      el("option", { value: "podcast", text: "Podcasts" }),
    ]);
    mode.value = shelfState.type;
    mode.addEventListener("change", () => { shelfState.type = mode.value; render(); });
    const sort = el("select", { id: "shelf-sort", "aria-label": "Sort shelf" }, [
      el("option", { value: "title", text: "Title A-Z" }),
      el("option", { value: "recent", text: "Recently updated" }),
      el("option", { value: "series", text: "Series order" }),
    ]);
    sort.value = shelfState.sort;
    sort.addEventListener("change", () => { shelfState.sort = sort.value; renderShelfRows(); });
    root.append(el("div", { class: "shelf-toolbar" }, [segs, search]), el("div", { class: "shelf-controls", style: "margin-bottom:16px" }, [mode, sort]), el("div", { id: "shelf-selection", class: "selection-bar" }), el("div", { id: "shelf-rows" }));
    renderShelfRows();
  }

  function shelfMatches(c) {
    if (shelfState.type === "podcast" && c.media_type !== "podcast") return false;
    if (shelfState.type === "audiobook" && c.media_type === "podcast") return false;
    if (shelfState.filter === "needspush" && !c.needs_push) return false;
    if (shelfState.filter === "synced" && c.needs_push) return false;
    if (shelfState.filter === "noseries" && c.series) return false;
    const q = shelfState.query.toLowerCase().trim();
    if (!q) return true;
    return [c.title, fmtAuthors(c.authors), c.series, c.media_type].filter(Boolean).join(" ").toLowerCase().includes(q);
  }

  function renderShelfRows() {
    const host = $("#shelf-rows");
    if (!host) return;
    host.innerHTML = "";
    const items = sortedShelfCards();
    const selected = state.cards.filter(card => shelfState.selected.has(card.card_id));
    const selectAll = el("input", { type: "checkbox", class: "row-select", "aria-label": "Select visible books", checked: items.length > 0 && items.every(card => shelfState.selected.has(card.card_id)), disabled: !items.length });
    selectAll.indeterminate = items.some(card => shelfState.selected.has(card.card_id)) && !items.every(card => shelfState.selected.has(card.card_id));
    selectAll.addEventListener("change", () => { items.forEach(card => selectAll.checked ? shelfState.selected.add(card.card_id) : shelfState.selected.delete(card.card_id)); renderShelfRows(); });
    $("#shelf-selection").replaceChildren(
      el("label", {}, [selectAll, el("span", { text: "Select visible" })]),
      el("span", { role: "status", text: `${selected.length} selected` }),
      el("button", { class: "btn btn-sm", type: "button", text: "Push selected", disabled: !selected.length, onclick: () => pushMany(selected) }),
      el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Clear selection", disabled: !selected.length, onclick: () => { shelfState.selected.clear(); renderShelfRows(); } }),
    );
    if (!items.length) {
      return host.append(emptyNode({ glyph: "∅", title: "Nothing matches", text: "Try a different filter or search." }));
    }
    host.append(
      el(
        "div",
        { class: "rows" },
        items.map(c =>
          rowNode({
            id: c.card_id,
            title: c.title,
            selectable: checked => { checked ? shelfState.selected.add(c.card_id) : shelfState.selected.delete(c.card_id); renderShelfRows(); },
            selected: shelfState.selected.has(c.card_id),
            typeLabel: c.media_type === "podcast" ? "Podcast" : null,
            meta: [fmtAuthors(c.authors), c.series ? `${c.series}${c.sequence_number ? ` #${c.sequence_number}` : ""}` : null, `${c.chapter_count} ${c.media_type === "podcast" ? "episodes" : "chapters"}`]
              .filter(Boolean)
              .join(" · "),
            cover: c.cover_url,
            chip: c.needs_push ? { label: c.yoto_card_id ? "Yoto update needed" : "Not on Yoto", tone: "needspush" } : { label: "Yoto up to date", tone: "synced" },
            queueKey: `push:${c.card_id}`,
            primary: c.needs_push ? { label: "Push", run: btn => pushCard(c, btn) } : null,
            card: c,
          })
        )
      )
    );
  }

  // ── Series completeness ─────────────────────────────────────────────────
  // Ported from the retired UI. Local sequence gaps only reveal holes between
  // books you already own; they cannot know about volumes you have never had.
  // Two sources fill that in:
  //
  //   /api/yoto/series/completeness  what the series contains, per metadata
  //                                  providers (Audible and friends) via ABS
  //   /api/catalog/series-lookup     what BorrowBox has, which is what you can
  //                                  actually borrow
  //
  // They are merged, matched against what you own, and the remainder is what is
  // missing.
  const seriesCache = new Map();
  const seriesChecking = new Set();

  const normTitle = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const seriesKey = s => normTitle(s);

  /** Most common author across the owned cards, used to disambiguate lookups. */
  function primarySeriesAuthor(cards) {
    const counts = new Map();
    for (const card of cards) {
      for (const author of card.authors || []) {
        const name = String(author || "").trim();
        if (name) counts.set(name, (counts.get(name) || 0) + 1);
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] || "";
  }

  async function loadExpectedSeries(name, ownedCards) {
    const author = primarySeriesAuthor(ownedCards);
    const params = new URLSearchParams({ name });
    if (author) params.set("author", author);
    try {
      return await getJSON(`${YOTO}/api/yoto/series/completeness?${params}`);
    } catch (err) {
      return { series_id: "unknown", name, source: "abs_provider", items: [], lookup_failed: true, lookup_error: err.message };
    }
  }

  async function loadBorrowBoxSeries(name) {
    try {
      return { data: await getJSON(`${API}/api/catalog/series-lookup?name=${encodeURIComponent(name)}`), failed: false };
    } catch (err) {
      return { data: null, failed: true, error: err.message };
    }
  }

  /**
   * Combine both sources into one list of titles the series contains.
   *
   * Keyed by normalised title, but sequence number wins when present: the two
   * sources often spell the same book differently ("Book 3: Foo" vs "Foo").
   */
  function mergeSeriesData(seriesName, expected, bb) {
    const merged = new Map();
    const seqToKey = new Map();

    const add = item => {
      if (!item || !item.title) return;
      let key = normTitle(item.title);
      if (item.series_number !== null && item.series_number !== undefined) {
        const seqKey = String(item.series_number);
        if (seqToKey.has(seqKey)) key = seqToKey.get(seqKey);
        else seqToKey.set(seqKey, key);
      }
      const prev = merged.get(key) || {};
      merged.set(key, {
        ...prev,
        ...item,
        authors: (item.authors?.length ? item.authors : prev.authors) || [],
        series_name: item.series_name || prev.series_name || expected?.name || bb?.name || seriesName,
        series_number: item.series_number ?? prev.series_number ?? null,
        cover_url: item.cover_url || prev.cover_url || null,
        // Keep both provenances when a title appears in each source.
        source:
          prev.source && item.source && prev.source !== item.source
            ? `${prev.source}+${item.source}`
            : item.source || prev.source,
        product_id: item.product_id || prev.product_id || null,
        availability: item.availability || prev.availability || null,
        available_date: item.available_date || prev.available_date || null,
        format: item.format || prev.format || null,
      });
    };

    (expected?.items || []).forEach(i => add({ ...i, source: i.source || "abs_provider" }));
    (bb?.items || []).forEach(i => add({ ...i, source: "borrowbox" }));

    return {
      series_id: bb?.series_id || expected?.series_id || "unknown",
      name: expected?.name || bb?.name || seriesName,
      items: [...merged.values()],
      sources: [...new Set([
        ...(expected?.items || []).map(item => item.provider || (expected.source === "abs" ? "Audiobookshelf metadata" : "Metadata providers")),
        ...((bb?.items || []).length ? ["BorrowBox"] : []),
      ])].map(source => source.startsWith("audible") ? "Audible" : source),
    };
  }

  /**
   * Titles from the series that are not on the shelf.
   *
   * Fuzzy on purpose: providers and BorrowBox punctuate and subtitle
   * inconsistently, so an exact match would report books you already own as
   * missing.
   */
  function findMissingSeriesItems(items, ownedCards) {
    const ownedNorms = ownedCards.map(c => normTitle(c.title));
    return items
      .filter(item => {
        if (item.series_number !== null && item.series_number !== undefined && ownedCards.some(card => card.sequence_number !== null && card.sequence_number !== undefined && Number(card.sequence_number) === Number(item.series_number))) return false;
        const norm = normTitle(item.title);
        for (const owned of ownedNorms) {
          if (owned === norm) return false;
          if (!owned || !norm) continue;

          const shorter = owned.length < norm.length ? owned : norm;
          const longer = owned.length < norm.length ? norm : owned;

          // Ported rule: similar lengths and one contains the other.
          if (shorter.length / longer.length > 0.6 && longer.includes(shorter)) return false;

          // Added rule: a locally subtitled copy ("Book Three: The Reckoning")
          // is much longer than the catalogue title ("Book Three"), so the ratio
          // test misses it and the book gets reported as missing when it is on
          // the shelf. A prefix match catches that; the length floor keeps short
          // generic titles from matching everything.
          if (shorter.length >= 8 && longer.startsWith(shorter)) return false;
        }
        return true;
      })
      .sort((a, b) => (a.series_number || 999) - (b.series_number || 999));
  }

  /** Cross-library availability for a title, so Borrow can name a library. */
  async function availabilityFor(productId) {
    try {
      return await getJSON(`${API}/api/catalog/availability/${encodeURIComponent(productId)}`);
    } catch {
      return null;
    }
  }

  async function checkSeries(seriesName, ownedCards, host, btn) {
    if (seriesChecking.has(seriesName)) return;
    seriesChecking.add(seriesName);
    if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
    host.innerHTML = "";
    host.append(el("div", { class: "panel-note", text: "Looking up the full series…" }));

    try {
      const [expected, bb] = await Promise.all([
        loadExpectedSeries(seriesName, ownedCards),
        loadBorrowBoxSeries(seriesName),
      ]);
      const data = mergeSeriesData(seriesName, expected, bb.data);
      data.borrowbox_failed = bb.failed;
      data.provider_failed = Boolean(expected.lookup_failed);
      data.lookup_errors = [expected.lookup_error, bb.error].filter(Boolean);
      data.checked_at = new Date().toISOString();
      data.availability_checking = true;
      data.availability = new Map();
      seriesCache.set(seriesName, data);

      const missing = findMissingSeriesItems(data.items, ownedCards);
      // Availability is only known per-library, so fetch it for borrowable
      // titles. Capped concurrency to avoid a burst of requests.
      const borrowable = missing.filter(m => m.product_id);
      const avail = data.availability;
      renderSeriesMissing(host, data, missing, avail);
      for (let i = 0; i < borrowable.length; i += 4) {
        const batch = borrowable.slice(i, i + 4);
        const got = await Promise.all(batch.map(m => availabilityFor(m.product_id)));
        batch.forEach((m, idx) => avail.set(m.product_id, got[idx] || { error: "Availability unavailable" }));
        renderSeriesMissing(host, data, missing, avail);
      }
      data.availability_checking = false;
      renderSeriesMissing(host, data, missing, avail);
    } catch (err) {
      host.innerHTML = "";
      host.append(el("div", { class: "panel-note", text: `Series lookup failed: ${err.message}` }));
    } finally {
      seriesChecking.delete(seriesName);
      if (btn) { btn.disabled = false; btn.textContent = "Re-check"; }
      scheduleRender();
    }
  }

  function renderSeriesMissing(host, data, missing, avail) {
    host.innerHTML = "";

    const summary = el("div", { class: "panel-note", style: "margin-bottom:8px" }, [
      el("span", {
        text:
          `${data.items.length ? "Partial catalogue" : "Unknown"} · ${data.items.length} known title${data.items.length === 1 ? "" : "s"}` +
          (data.sources?.length ? ` · ${[...new Set(data.sources)].join(", ")}` : "") +
          (data.checked_at ? ` · Checked ${new Date(data.checked_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : "") +
          (data.borrowbox_failed ? " · BorrowBox unavailable" : "") +
          (data.provider_failed ? " · Metadata catalogue unavailable" : ""),
      }),
    ]);
    host.append(summary);

    if (!data.items.length) {
      host.append(el("div", { class: "source-notice source-error", role: "status", text: "Missing books could not be determined." }));
      if (data.lookup_errors?.length) host.append(el("p", { class: "panel-note", text: data.lookup_errors.join(" · ") }));
      return;
    }
    if (!missing.length) {
      host.append(el("div", { class: "chip chip-plain", text: "No known gaps in this catalogue" }));
      return;
    }

    const rows = el("div", { class: "rows" });
    missing.forEach(item => {
      const availability = avail.get(item.product_id);
      const libs = Object.entries(availability?.libraries || {});
      const best = libs.find(([, info]) => info.status === "available");

      const matrix = el(
        "div",
        { class: "avail" },
        libs.length
          ? libs.map(([name, info]) =>
              el("span", {
                class: "avail-cell",
                dataset: { status: info.status },
                title: `${name}: ${info.status}${info.available_date ? ` — ${info.available_date}` : ""}`,
                text: info.status === "available" ? name : `${name} ${info.available_date || "—"}`,
              })
            )
          : [
              el("span", {
                class: "avail-cell",
                text: item.product_id ? availability?.error || (data.availability_checking ? "Checking availability..." : "Availability unknown") : "No BorrowBox match",
              }),
            ]
      );

      const actions = el("div", { class: "row-actions" });
      if (best && isAudio(item.format)) {
        const b = el("button", { class: "btn btn-sm btn-primary", type: "button", text: `Borrow · ${best[0]}` });
        b.addEventListener("click", () => borrow(item.product_id, best[0], b));
        actions.append(b);
      } else if (item.product_id && libs.length) {
        const soonest = libs.find(([, i]) => i.status === "on_loan");
        if (soonest) {
          const b = el("button", { class: "btn btn-sm", type: "button", text: `Reserve · ${soonest[0]}` });
          b.addEventListener("click", () => reserve(item.product_id, soonest[0], b));
          actions.append(b);
        }
      }

      rows.append(
        el("div", { class: "row" }, [
          coverNode(item.cover_url, "?"),
          el("div", { class: "row-main" }, [
            el("div", {
              class: "row-title",
              text: `${item.series_number ? `#${item.series_number} · ` : ""}${item.title}`,
            }),
            el("div", { class: "row-meta", text: [fmtAuthors(item.authors), item.source].filter(Boolean).join(" · ") }),
            matrix,
          ]),
          el("span", { class: "chip chip-needspush", text: "Missing" }),
          actions,
        ])
      );
    });

    host.append(
      el("div", { class: "group-head", style: "margin-top:4px" }, [
        el("h3", { class: "group-title", style: "font-size:0.875rem", text: "Missing from your shelf" }),
        el("span", { class: "chip chip-plain", text: String(missing.length) }),
        el("span", { class: "group-rule" }),
      ]),
      rows
    );
  }

  // ── Series ──────────────────────────────────────────────────────────────
  function viewSeries(root) {
    root.append(pageHead("Series", "Sequence gaps in your shelf, so you can see what is missing."));
    sourceNotices(root, ["cards"]);
    if (sourcePending("cards") && !state.cards.length) return root.append(skeleton(4));
    if (sourceFailed("cards") && !state.cards.length) return;

    const grouped = new Map();
    for (const c of state.cards) {
      if (!c.series) continue;
      if (!grouped.has(c.series)) grouped.set(c.series, []);
      grouped.get(c.series).push(c);
    }
    if (!grouped.size) {
      return root.append(
        emptyNode({
          glyph: "⋮⋮",
          title: "No series yet",
          text: "Group your books into series to track which volumes you are missing.",
          actions: [{ label: "Discover series", run: () => discoverSeries() }],
        })
      );
    }

    const unsorted = state.cards.filter(c => !c.series).length;
    if (unsorted) {
      root.append(
        el("div", { class: "panel", style: "margin-bottom:24px" }, [
          el("div", { class: "panel-title", text: `${unsorted} book${unsorted === 1 ? "" : "s"} not in a series` }),
          el("p", { class: "panel-note", text: "Series can be inferred from Audiobookshelf metadata, falling back to AI for the rest." }),
          el("div", { style: "margin-top:12px" }, [
            el("button", { class: "btn btn-primary btn-sm", type: "button", text: "Discover series", onclick: () => discoverSeries() }),
          ]),
        ])
      );
    }

    const list = el("div", { style: "display:flex;flex-direction:column;gap:16px" });
    for (const [name, cards] of [...grouped].sort((a, b) => a[0].localeCompare(b[0]))) {
      const numbered = cards.filter(c => typeof c.sequence_number === "number").map(c => c.sequence_number);
      const max = numbered.length ? Math.max(...numbered) : 0;
      const have = new Set(numbered);
      const gaps = [];
      for (let i = 1; i <= max; i++) if (!have.has(i)) gaps.push(i);

      const head = el("div", { class: "group-head" }, [
        el("h2", { class: "group-title", text: name }),
        el("span", { class: "chip chip-plain", text: `${cards.length} owned` }),
        gaps.length
          ? el("span", { class: "chip chip-needspush", text: `missing #${gaps.join(", #")}` })
          : el("span", { class: "chip chip-plain", text: numbered.length ? "No internal gaps" : "Numbering unknown" }),
        el("span", { class: "group-rule" }),
      ]);

      // Looks up the real series contents rather than guessing from gaps.
      const missingHost = el("div", { class: "series-missing" });
      const cached = seriesCache.get(name);
      if (cached) renderSeriesMissing(missingHost, cached, findMissingSeriesItems(cached.items, cards), cached.availability || new Map());
      const check = el("button", {
        class: "btn btn-sm",
        type: "button",
        text: seriesChecking.has(name) ? "Checking..." : cached ? "Re-check" : "Check for missing books",
        disabled: seriesChecking.has(name),
      });
      check.addEventListener("click", () => checkSeries(name, cards, missingHost, check));
      head.append(check);

      list.append(
        el("section", { class: "group" }, [
          head,
          el(
            "div",
            { class: "rows" },
            [...cards]
              .sort((a, b) => (a.sequence_number || 0) - (b.sequence_number || 0))
              .map(c =>
                rowNode({
                  title: `${c.sequence_number ? `#${c.sequence_number} · ` : ""}${c.title}`,
                  meta: fmtAuthors(c.authors),
                  cover: c.cover_url,
                  chip: c.needs_push ? { label: "Needs push", tone: "needspush" } : { label: "Synced", tone: "synced" },
                  queueKey: `push:${c.card_id}`,
                  primary: c.needs_push ? { label: "Push", run: btn => pushCard(c, btn) } : null,
                  card: c,
                })
              )
          ),
          missingHost,
        ])
      );
    }
    root.append(list);
  }

  // ── History ─────────────────────────────────────────────────────────────
  const historyState = { query: "" };

  function viewHistory(root) {
    root.append(pageHead("History", "Loans you have returned. Borrow again from here."));
    sourceNotices(root, ["history"]);
    if (sourcePending("history")) return root.append(skeleton(5));
    if (sourceFailed("history") && !Object.keys(state.history).length) return;

    const total = Object.values(state.history).reduce((n, a) => n + (a?.length || 0), 0);
    if (!total) {
      return root.append(
        emptyNode({
          glyph: "\u{1F553}",
          title: "No history yet",
          text: "Returned loans show up here, so you can re-borrow without searching again.",
        })
      );
    }

    const search = el("input", {
      class: "input input-plain",
      id: "history-query",
      "aria-label": "Filter history",
      type: "search",
      placeholder: "Filter history…",
      value: historyState.query,
      style: "max-width:280px",
    });
    let t = null;
    search.addEventListener("input", () => {
      // Debounced re-filter of the list only, so the caret is never disturbed.
      window.clearTimeout(t);
      const v = search.value;
      t = window.setTimeout(() => {
        historyState.query = v;
        renderHistoryRows();
      }, 120);
    });

    root.append(
      el("div", { style: "display:flex;gap:12px;align-items:center;margin-bottom:20px" }, [search]),
      el("div", { id: "history-rows" })
    );
    renderHistoryRows();
  }

  function renderHistoryRows() {
    const host = $("#history-rows");
    if (!host) return;
    const q = historyState.query.toLowerCase().trim();
    host.innerHTML = "";

    let shown = 0;
    const wrap = el("div", { style: "display:flex;flex-direction:column;gap:24px" });
    for (const [library, loans] of Object.entries(state.history)) {
      const matching = (loans || []).filter(l =>
        !q || [l.title, fmtAuthors(l.authors)].filter(Boolean).join(" ").toLowerCase().includes(q)
      );
      if (!matching.length) continue;
      shown += matching.length;
      wrap.append(
        el("section", { class: "group" }, [
          el("div", { class: "group-head" }, [
            el("h2", { class: "group-title", text: library }),
            el("span", { class: "chip chip-plain", text: String(matching.length) }),
            el("span", { class: "group-rule" }),
          ]),
          el(
            "div",
            { class: "rows" },
            matching.map(loan =>
              rowNode({
                title: loan.title,
                meta: [fmtAuthors(loan.authors), loan.end_date ? `returned ${loan.end_date}` : null]
                  .filter(Boolean)
                  .join(" · "),
                cover: loan.cover_url,
                chip: loan.in_abs
                  ? { label: "In library", tone: "library" }
                  : { label: "Not kept", tone: "plain" },
                // Re-borrowing needs the availability check, so send the title
                // to Find rather than guessing a library here.
                primary: {
                  label: "Find again",
                  run: () => {
                    location.hash = "#/find";
                    window.setTimeout(() => runSearch(loan.title), 0);
                  },
                },
              })
            )
          ),
        ])
      );
    }

    if (!shown) {
      return host.append(emptyNode({ glyph: "\u2205", title: "No matches", text: `Nothing matching “${q}”.` }));
    }
    host.append(wrap);
  }

  // ── Purchased ───────────────────────────────────────────────────────────
  function viewPurchased(root) {
    root.append(
      pageHead("Purchased", "Cards bought from Yoto or shared with you. These are read-only.")
    );
    sourceNotices(root, ["purchased"]);
    if (sourcePending("purchased")) return root.append(skeleton(4));
    if (sourceFailed("purchased") && !state.purchased.length) return;

    if (!state.purchased.length) {
      return root.append(
        emptyNode({
          glyph: "\u{1F3A7}",
          title: "No purchased cards",
          text: "Official Yoto cards and cards shared with you appear here. They are not managed by Audiobookshelf, so they cannot be edited or pushed.",
        })
      );
    }

    root.append(
      el(
        "div",
        { class: "rows" },
        state.purchased.map(card =>
          rowNode({
            title: card.title || card.card_id,
            meta: [card.author, card.category, card.share_type].filter(Boolean).join(" · "),
            cover: card.cover_url,
            chip: {
              label: card.share_type && card.share_type !== "yoto" ? "Shared" : "Purchased",
              tone: "library",
            },
          })
        )
      )
    );
  }

  // ── Settings ────────────────────────────────────────────────────────────
  function viewSettings(root) {
    root.append(pageHead("Settings", "Connections, credits, and maintenance."));
    sourceNotices(root, ["libraries", "credentials", "auth"]);

    root.append(librariesPanel());

    root.append(
      el("div", { class: "panel" }, [
        el("div", { class: "panel-title", text: "Audiobookshelf" }),
        el("div", { style: "margin-top:16px;display:flex;gap:8px;flex-wrap:wrap" }, [
          el("button", { class: "btn btn-sm", type: "button", text: "Sync from Audiobookshelf", onclick: () => syncAbs() }),
        ]),
      ])
    );

    root.append(
      el("div", { class: "panel" }, [
        el("div", { class: "panel-title", text: "Yoto" }),
        el("p", {
          class: "panel-note",
          text: state.yotoAuth
            ? state.yotoAuth.authenticated
              ? `Signed in${state.yotoAuth.user_id ? ` as ${state.yotoAuth.user_id}` : ""}.`
              : "Not signed in. Run the CLI auth flow to obtain a token."
            : "Status unavailable — the Yoto service may be unreachable.",
        }),
        el("div", { style: "margin-top:16px;display:flex;gap:8px;flex-wrap:wrap" }, [
          el("button", { class: "btn btn-sm", type: "button", text: "Pull from Yoto", onclick: () => pullYoto() }),
        ]),
      ])
    );

    root.append(
      el("div", { class: "panel", style: "border-color:var(--danger)" }, [
        el("div", { class: "panel-title", text: "Maintenance" }),
        el("p", { class: "panel-note", text: "Destructive operations. Kept here, away from everyday actions." }),
        el("div", { style: "margin-top:16px;display:flex;gap:8px;flex-wrap:wrap" }, [
          el("button", { class: "btn btn-sm btn-danger", type: "button", text: "Reset library cache", onclick: () => resetLibrary() }),
        ]),
      ])
    );
  }

  // ── credential management ───────────────────────────────────────────────
  /** Credential status for one library, from GET /api/credentials. */
  function credentialStatus(library) {
    return (state.credentials?.libraries || []).find(c => c.library === library) || null;
  }

  /**
   * Libraries panel: sign-in state plus an inline form per library.
   *
   * Credentials are held encrypted server-side, so the password is write-only —
   * it is never sent back to the browser and the field always starts empty.
   */
  function librariesPanel() {
    const panel = el("div", { class: "panel" }, [
      el("div", { class: "panel-title", text: "BorrowBox libraries" }),
      el("p", { class: "panel-note", text: "Searches run across every configured library at once. Credentials are encrypted before being stored." }),
    ]);

    if (state.credentials && state.credentials.writable === false) {
      panel.append(
        el("div", { class: "panel", style: "margin-top:16px;border-color:var(--warn)" }, [
          el("div", { class: "panel-title", text: "Credentials are read-only" }),
          el("p", {
            class: "panel-note",
            text: "BORROWBOX_SECRET_KEY is not set, so credentials cannot be encrypted and saved from here. Set it and restart; existing environment credentials keep working meanwhile.",
          }),
        ])
      );
    }

    if (!state.libraries.length) {
      panel.append(
        el("div", { class: "empty", style: "padding:24px 8px" }, [
          el("div", { class: "empty-glyph", "aria-hidden": "true", text: "◍" }),
          el("div", { class: "empty-title", text: "No libraries yet" }),
          el("div", {
            class: "empty-text",
            text: "Add the BorrowBox library your card is registered with. You can find its subdomain in the URL you normally sign in at, e.g. hounslow.borrowbox.com.",
          }),
        ])
      );
    } else {
      const rows = el("div", { class: "rows", style: "margin-top:16px" });
      state.libraries.forEach(lib => rows.append(libraryRow(lib)));
      panel.append(rows);
    }

    panel.append(addLibraryForm());
    return panel;
  }

  /**
   * Add a library without touching the environment or restarting.
   * The registry lives in Redis; this takes effect immediately.
   */
  function addLibraryForm() {
    const nameInput = el("input", {
      class: "input input-plain",
      id: "new-lib-name",
      type: "text",
      placeholder: "hounslow",
      autocomplete: "off",
    });
    const subInput = el("input", {
      class: "input input-plain",
      id: "new-lib-subdomain",
      type: "text",
      placeholder: "hounslow",
      autocomplete: "off",
    });
    const submit = el("button", { class: "btn btn-primary btn-sm", type: "submit", text: "Add library" });

    // A name is almost always the same as the subdomain, so mirror it until the
    // operator types a different one.
    let nameEdited = false;
    nameInput.addEventListener("input", () => { nameEdited = true; });
    subInput.addEventListener("input", () => {
      if (!nameEdited) nameInput.value = subInput.value;
    });

    const form = el("form", { class: "cred-fields", style: "margin-top:20px;max-width:520px" }, [
      el("div", { class: "panel-title", style: "font-size:0.875rem", text: "Add a library" }),
      el("div", { class: "form-row" }, [
        el("label", { for: "new-lib-subdomain", text: "BorrowBox subdomain" }),
        subInput,
        el("span", { class: "panel-note", text: "The part before .borrowbox.com" }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { for: "new-lib-name", text: "Name shown in the app" }),
        nameInput,
      ]),
      el("div", { class: "cred-actions" }, [submit]),
    ]);

    form.addEventListener("submit", async e => {
      e.preventDefault();
      const subdomain = subInput.value.trim();
      const name = (nameInput.value.trim() || subdomain).trim();
      if (!subdomain) {
        toast("A subdomain is required.", "error");
        subInput.focus();
        return;
      }
      await withButton(submit, "Adding…", async () => {
        await send(`${API}/api/libraries`, "POST", { name, subdomain });
        subInput.value = "";
        nameInput.value = "";
        nameEdited = false;
        toast(`Added ${name}. Sign in to start borrowing.`, "ok");
        await loadAll();
      });
    });
    return form;
  }

  async function removeLibrary(name) {
    const ok = await confirmDialog({
      title: `Remove ${name}?`,
      text: "The library is deregistered and its stored credentials are deleted. Loans already imported into Audiobookshelf are unaffected.",
      confirmLabel: "Remove library",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`${API}/api/libraries/${encodeURIComponent(name)}`, "DELETE");
      toast(`${name} removed`, "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function libraryRow(lib) {
    const cred = credentialStatus(lib.name);
    const credit = creditInfo(lib.name);

    // Sign-in health comes from the server's recorded verification, falling
    // back to the credits lookup as live evidence the session works.
    let health;
    if (!cred || !cred.has_credentials) health = { chip: "chip-orphan", label: "No credentials" };
    else if (cred.verified === false || credit.status === "error") health = { chip: "chip-orphan", label: "Sign-in failing" };
    else if (cred.verified === true) health = { chip: "chip-synced", label: "Signed in" };
    else health = { chip: "chip-plain", label: "Not verified" };

    const bad = health.chip === "chip-orphan";
    const metaBits = [lib.subdomain || lib.name];
    if (cred?.username) metaBits.push(cred.username);
    if (cred?.source === "env") metaBits.push("from environment");
    if (cred?.error) metaBits.push(cred.error);
    else if (credit.status === "error") metaBits.push(credit.label);

    const form = el("div", { class: "cred-form", hidden: true });
    const toggle = el("button", {
      class: "btn btn-sm",
      type: "button",
      "aria-expanded": "false",
      text: cred?.has_credentials ? "Change" : "Sign in",
    });
    toggle.addEventListener("click", () => {
      const open = form.hidden;
      form.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) {
        if (!form.childElementCount) form.append(credentialForm(lib, cred));
        form.querySelector("input")?.focus();
      }
    });

    const actions = el("div", { class: "row-actions" }, [toggle]);
    if (cred?.has_credentials) {
      const verify = el("button", { class: "btn btn-sm", type: "button", text: "Test" });
      verify.addEventListener("click", () => verifyLibrary(lib.name, verify));
      actions.append(verify);
    }
    const remove = el("button", {
      class: "btn btn-sm btn-icon",
      type: "button",
      "aria-label": `Remove ${lib.name}`,
      title: `Remove ${lib.name}`,
      text: "✕",
    });
    remove.addEventListener("click", () => removeLibrary(lib.name));
    actions.append(remove);

    return el("div", { class: "cred-block" }, [
      el("div", { class: "row" }, [
        el("div", { class: "cover", "aria-hidden": "true", text: bad ? "!" : "◍" }),
        el("div", { class: "row-main" }, [
          el("div", { class: "row-title", text: lib.name }),
          el("div", { class: "row-meta", title: credit.detail || "", text: metaBits.join(" · ") }),
        ]),
        el("span", { class: `chip ${health.chip}`, text: health.label }),
        el("span", {
          class: `chip ${credit.status === "error" ? "chip-orphan" : credit.status === "empty" ? "chip-needspush" : "chip-plain"}`,
          title: credit.title,
          text: credit.label,
        }),
        actions,
      ]),
      form,
    ]);
  }

  function credentialForm(lib, cred) {
    const writable = state.credentials?.writable !== false;
    const userId = `cred-user-${lib.name}`;
    const passId = `cred-pass-${lib.name}`;

    const username = el("input", {
      class: "input input-plain",
      id: userId,
      type: "text",
      autocomplete: "username",
      value: cred?.username || "",
      disabled: !writable || null,
    });
    // Never prefilled: the server does not return stored passwords.
    const password = el("input", {
      class: "input input-plain",
      id: passId,
      type: "password",
      autocomplete: "new-password",
      placeholder: cred?.has_credentials ? "Enter to replace" : "",
      disabled: !writable || null,
    });

    const save = el("button", { class: "btn btn-primary btn-sm", type: "submit", text: "Verify and save", disabled: !writable || null });
    const wrap = el("form", { class: "cred-fields" }, [
      el("div", { class: "form-row" }, [el("label", { for: userId, text: "Library card / username" }), username]),
      el("div", { class: "form-row" }, [el("label", { for: passId, text: "PIN / password" }), password]),
    ]);

    const buttons = el("div", { class: "cred-actions" }, [save]);
    if (cred?.source === "store") {
      const forget = el("button", { class: "btn btn-sm btn-danger", type: "button", text: "Forget" });
      forget.addEventListener("click", () => forgetLibrary(lib.name));
      buttons.append(forget);
    }
    wrap.append(buttons);

    wrap.addEventListener("submit", async e => {
      e.preventDefault();
      if (!username.value.trim() || !password.value) {
        toast("Both username and password are required.", "error");
        return;
      }
      await withButton(save, "Verifying…", async () => {
        // The server verifies against the library before persisting, so a typo
        // cannot be saved.
        await send(`${API}/api/credentials/${encodeURIComponent(lib.name)}`, "PUT", {
          username: username.value.trim(),
          password: password.value,
        });
        password.value = "";
        toast(`${lib.name} signed in and saved`, "ok");
        await loadAll();
      });
    });
    return wrap;
  }

  async function verifyLibrary(library, btn) {
    await withButton(btn, "Testing…", async () => {
      const data = await send(`${API}/api/credentials/${encodeURIComponent(library)}/verify`);
      if (data.status === "ok") toast(`${library} signed in successfully`, "ok");
      else toast(`${library}: ${data.error || "sign-in failed"}`, "error");
      await loadAll();
    });
  }

  async function forgetLibrary(library) {
    const ok = await confirmDialog({
      title: `Forget credentials for ${library}?`,
      text: "The stored credentials are deleted and the cached session dropped. Any environment-provided credentials become active again.",
      confirmLabel: "Forget credentials",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`${API}/api/credentials/${encodeURIComponent(library)}`, "DELETE");
      toast(`Credentials for ${library} removed`, "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function syncAbs() {
    if (state.syncingAbs) return;
    state.syncingAbs = true;
    scheduleRender();
    await startWorkflow({ action: "sync", label: "Audiobookshelf library" });
  }

  async function pullYoto() {
    await startWorkflow({ action: "pull", label: "Yoto library" });
  }

  /**
   * Copy-out / paste-back dialog for the manual AI grouping fallback.
   *
   * When the server cannot group the remaining cards itself it returns a ready
   * prompt to run in an external LLM. Resolves with the pasted reply, or null if
   * skipped.
   */
  function pastePromptDialog({ title, description, promptText }) {
    return new Promise(resolve => {
      const scrim = el("div", { class: "confirm-scrim" });
      const promptArea = el("textarea", { class: "input", rows: 6, readonly: true, "aria-label": "Prompt to copy" });
      // Assigned as a value so the prompt is never parsed as markup.
      promptArea.value = promptText;
      const resultArea = el("textarea", {
        class: "input",
        rows: 5,
        id: "paste-result",
        placeholder: "Paste the model's reply here…",
      });

      const copy = el("button", { class: "btn btn-sm", type: "button", text: "Copy prompt" });
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(promptText);
          copy.textContent = "Copied";
          window.setTimeout(() => (copy.textContent = "Copy prompt"), 1500);
        } catch {
          // Clipboard access can be blocked; selecting is the fallback.
          promptArea.focus();
          promptArea.select();
        }
      });

      const use = el("button", { class: "btn btn-primary", type: "button", text: "Use this grouping" });
      const skip = el("button", { class: "btn", type: "button", text: "Skip" });

      const box = el("div", {
        class: "confirm",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": title,
        style: "width:min(640px,100%)",
      }, [
        el("h2", { class: "confirm-title", text: title }),
        el("p", { class: "confirm-text", text: description }),
        el("div", { style: "margin-top:16px;display:flex;flex-direction:column;gap:6px" }, [
          el("label", { class: "panel-note", text: "1. Copy this into an AI chat" }),
          promptArea,
          copy,
        ]),
        el("div", { style: "margin-top:16px;display:flex;flex-direction:column;gap:6px" }, [
          el("label", { class: "panel-note", for: "paste-result", text: "2. Paste its reply back" }),
          resultArea,
        ]),
        el("div", { class: "confirm-actions" }, [skip, use]),
      ]);
      scrim.append(box);

      const previous = document.activeElement;
      const finish = value => {
        scrim.remove();
        document.removeEventListener("keydown", onKey, true);
        syncModalState();
        if (previous?.isConnected) previous.focus();
        resolve(value);
      };
      const onKey = e => {
        if (e.key === "Escape") { e.stopPropagation(); finish(null); }
      };
      use.addEventListener("click", () => finish(resultArea.value.trim() || null));
      skip.addEventListener("click", () => finish(null));
      scrim.addEventListener("click", e => { if (e.target === scrim) finish(null); });
      document.addEventListener("keydown", onKey, true);

      document.body.append(scrim);
      syncModalState();
      copy.focus();
    });
  }

  async function discoverSeries() {
    toast("Looking for series groupings…");
    try {
      const data = await send(`${YOTO}/api/yoto/series/discover`);
      let suggestions = data.series || [];

      // Cards the server could not group come back as a prompt for an external
      // model. Without this the residue is silently dropped.
      if (data.manual_prompt) {
        const pasted = await pastePromptDialog({
          title: "Group the remaining books with AI",
          description:
            `${suggestions.length} series were grouped automatically. The rest could not be, so run this prompt in any AI chat and paste the reply back.`,
          promptText: data.manual_prompt,
        });
        if (pasted) {
          try {
            const manual = await send(`${YOTO}/api/yoto/series/discover/manual`, "POST", { response: pasted });
            suggestions = suggestions.concat(manual.series || []);
            toast(`Parsed ${(manual.series || []).length} more series from the reply`, "ok");
          } catch (err) {
            toast(`Could not parse the reply: ${err.message}`, "error");
          }
        }
      }

      if (!suggestions.length) {
        toast("No new series found", "ok");
        return;
      }
      const ok = await confirmDialog({
        title: `Apply ${suggestions.length} series grouping${suggestions.length === 1 ? "" : "s"}?`,
        text: suggestions.slice(0, 6).map(s => `${s.series_name} (${s.card_ids.length})`).join(", ") + (suggestions.length > 6 ? "…" : ""),
        confirmLabel: "Apply",
      });
      if (!ok) return;
      await send(`${YOTO}/api/yoto/series/apply`, "POST", { suggestions });
      toast("Series applied", "ok");
      await loadAll();
    } catch (err) {
      toast(`Discovery failed: ${err.message}`, "error");
    }
  }

  async function resetLibrary() {
    const ok = await confirmDialog({
      title: "Reset the library cache?",
      text: "Every cached book is cleared and must be re-synced from Audiobookshelf. Cards already on Yoto are not deleted.",
      confirmLabel: "Reset cache",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`${YOTO}/api/yoto/cards`, "DELETE");
      toast("Cache cleared. Run a sync to repopulate.", "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ── drawer ──────────────────────────────────────────────────────────────
  let drawerReturnFocus = null;
  let drawerRequest = null;
  let drawerEditor = null;

  async function mayDiscardDrawer() {
    if (drawerEditor?.saving) return false;
    if (!drawerEditor?.dirty()) return true;
    return confirmDialog({
      title: "Discard unsaved changes?",
      text: `Unsaved changes to "${$("#drawer-title").textContent}" will be discarded.`,
      confirmLabel: "Discard changes",
      danger: true,
    });
  }

  async function openDrawer(card) {
    const drawer = $("#drawer");
    const scrim = $("#drawer-scrim");
    const body = $("#drawer-body");
    if (!drawer.hidden && !(await mayDiscardDrawer())) return;
    if (drawer.hidden) drawerReturnFocus = document.activeElement;
    drawerEditor = null;
    drawerRequest?.abort();
    const request = new AbortController();
    drawerRequest = request;

    $("#drawer-title").textContent = card.title || "Details";
    body.innerHTML = "";
    $("#drawer-actions").replaceChildren();
    body.append(skeleton(3));
    drawer.hidden = false;
    scrim.hidden = false;
    syncModalState();
    $("#drawer-close").focus();

    try {
      const detail = await getJSON(`${YOTO}/api/yoto/cards/${encodeURIComponent(card.card_id)}`, { signal: request.signal });
      if (drawerRequest !== request || drawer.hidden) return;
      body.innerHTML = "";
      body.append(drawerContent(detail));
    } catch (err) {
      if (err.name === "AbortError" || drawerRequest !== request || drawer.hidden) return;
      body.innerHTML = "";
      body.append(emptyNode({
        glyph: "!",
        title: "Details unavailable",
        text: err.message,
        actions: [{ label: "Retry", run: () => openDrawer(card) }],
      }));
    }
  }

  async function closeDrawer(force = false) {
    if (force !== true && !(await mayDiscardDrawer())) return false;
    drawerRequest?.abort();
    drawerRequest = null;
    drawerEditor = null;
    $("#drawer").hidden = true;
    $("#drawer-scrim").hidden = true;
    syncModalState();
    if (drawerReturnFocus?.isConnected) drawerReturnFocus.focus();
    return true;
  }

  function drawerContent(d) {
    const wrap = el("div");
    const isPodcast = d.media_type === "podcast";

    if (isPodcast) {
      wrap.append(
        el("div", { class: "drawer-type" }, [
          el("span", { class: "chip chip-podcast", text: "Podcast" }),
        ])
      );
    }

    // Pipeline position, made explicit rather than inferred from which tab
    // the book happened to appear in.
    const steps = [
      { label: "In library", done: true },
      ...(!isPodcast ? [{ label: "Has series", done: Boolean(d.series) }] : []),
      { label: "On Yoto", done: Boolean(d.yoto_card_id) },
      { label: "Yoto up to date", done: Boolean(d.yoto_card_id) && !d.needs_push },
    ];
    const currentIdx = steps.findIndex(s => !s.done);
    wrap.append(
      el(
        "div",
        { class: "pipeline" },
        steps.map((s, i) =>
          el("div", { class: "pipe-step", dataset: { done: String(s.done), current: String(i === currentIdx) } }, [
            el("div", { class: "pipe-bar" }),
            el("div", { class: "pipe-label", text: s.label }),
          ])
        )
      )
    );

    if ((d.diff_fields || []).length) {
      wrap.append(
        el("div", { class: "panel", style: "margin-bottom:16px;border-color:var(--warn)" }, [
          el("div", { class: "panel-title", text: "Differs from the card on Yoto" }),
          el("p", { class: "panel-note", text: d.diff_fields.join(", ") }),
        ])
      );
    }

    const isAbsBacked = d.source === "abs";
    const summary = el("div", { class: "book-summary" }, [
      el("h3", { class: "detail-book-title", text: d.title || "Untitled" }),
      el("p", { class: "detail-book-meta", text: [fmtAuthors(d.authors), d.series ? `${d.series}${d.sequence_number ? ` #${d.sequence_number}` : ""}` : null].filter(Boolean).join(" · ") }),
      el("details", { hidden: !d.description }, [el("summary", { text: "Description" }), el("p", { class: "book-description", text: d.description || "" })]),
    ]);
    const fields = el("div", { class: "book-editor", hidden: true });
    wrap.append(summary, fields);
    const f = {};
    const field = (key, label, opts = {}) => {
      const input = opts.textarea
        ? el("textarea", { class: "input", rows: 3, id: `f-${key}` })
        : el("input", { class: "input input-plain", type: opts.type || "text", id: `f-${key}` });
      input.value = d[key] ?? "";
      input.disabled = true;
      f[key] = input;
      return el("div", { class: "form-row" }, [el("label", { for: `f-${key}`, text: label }), input]);
    };

    fields.append(field("title", "Title"));
    // The API takes a comma-separated author string.
    const authorInput = el("input", { class: "input input-plain", type: "text", id: "f-author" });
    authorInput.value = fmtAuthors(d.authors);
    authorInput.disabled = true;
    f.author = authorInput;
    fields.append(el("div", { class: "form-row" }, [el("label", { for: "f-author", text: "Author" }), authorInput]));

    const seriesInput = el("input", { class: "input input-plain", type: "text", id: "f-series" });
    seriesInput.value = d.series ?? "";
    seriesInput.disabled = true;
    f.series = seriesInput;
    const seqInput = el("input", { class: "input input-plain", type: "number", min: "0", step: "1", id: "f-seq" });
    seqInput.value = d.sequence_number ?? "";
    seqInput.disabled = true;
    f.sequence_number = seqInput;
    fields.append(
      el("div", { class: "form-grid" }, [
        el("div", { class: "form-row" }, [el("label", { for: "f-series", text: "Series" }), seriesInput]),
        el("div", { class: "form-row" }, [el("label", { for: "f-seq", text: "#" }), seqInput]),
      ])
    );
    fields.append(field("description", "Description", { textarea: true }));

    if (!isAbsBacked) {
      wrap.append(
        el("p", { class: "panel-note", style: "margin-bottom:16px", text: "This card came from Yoto and has no Audiobookshelf record, so its metadata is read-only here." })
      );
    }

    const actions = el("div", { class: "drawer-actions" });
    const readValues = () => ({
      title: f.title.value.trim(), author: f.author.value.trim(), series: f.series.value.trim(),
      sequence_number: f.sequence_number.value === "" ? null : Number(f.sequence_number.value),
      description: f.description.value.trim(),
    });
    let savedValues = readValues();
    let editing = false;
    const editor = { saving: false, dirty: () => JSON.stringify(readValues()) !== JSON.stringify(savedValues) };
    drawerEditor = editor;
    const edit = el("button", { class: "btn", type: "button", text: "Edit metadata", hidden: !isAbsBacked });
    const save = el("button", { class: "btn btn-primary", type: "button", text: "Save", hidden: true });
    const discard = el("button", { class: "btn", type: "button", text: "Discard", hidden: true });
    const savePush = el("button", { class: "btn", type: "button", text: "Save and Push", hidden: true });
    const status = el("span", { class: "editor-status", role: "status" });
    const push = el("button", { class: "btn", type: "button", text: "Push to Yoto" });
    const updateEditor = () => {
      summary.hidden = editing;
      fields.hidden = !editing;
      edit.hidden = editing || !isAbsBacked;
      save.hidden = discard.hidden = savePush.hidden = !editing;
      save.disabled = savePush.disabled = !editor.dirty() || editor.saving;
      discard.disabled = editor.saving;
      discard.textContent = editor.dirty() ? "Discard" : "Cancel edit";
      $("#drawer-close").disabled = editor.saving;
      push.disabled = editor.dirty() || editor.saving;
      push.hidden = editing;
      Object.values(f).forEach(input => { input.disabled = !editing || editor.saving; });
      status.textContent = editor.saving ? "Saving..." : editor.dirty() ? "Unsaved changes" : "";
    };
    const saveMetadata = async () => {
      if (!isAbsBacked || editor.saving) return false;
      const values = readValues();
      if (!values.title || (values.sequence_number !== null && (!Number.isFinite(values.sequence_number) || values.sequence_number < 0))) {
        toast("A title and a valid series number are required.", "error");
        return false;
      }
      editor.saving = true;
      updateEditor();
      try {
        await send(`${YOTO}/api/yoto/cards/${encodeURIComponent(d.card_id)}/metadata`, "PATCH", values);
        savedValues = values;
        Object.assign(d, values, { authors: values.author.split(",").map(author => author.trim()).filter(Boolean) });
        $("#drawer-title").textContent = d.title;
        summary.querySelector(".detail-book-title").textContent = d.title;
        summary.querySelector(".detail-book-meta").textContent = [fmtAuthors(d.authors), d.series ? `${d.series}${d.sequence_number ? ` #${d.sequence_number}` : ""}` : null].filter(Boolean).join(" · ");
        const description = summary.querySelector(".book-description");
        description.textContent = d.description;
        description.closest("details").hidden = !d.description;
        toast("Saved to Audiobookshelf", "ok");
        editing = false;
        loadAll();
        return true;
      } catch (err) {
        toast(`Save failed: ${err.message}`, "error");
        return false;
      } finally {
        editor.saving = false;
        updateEditor();
      }
    };
    Object.values(f).forEach(input => input.addEventListener("input", updateEditor));
    edit.addEventListener("click", () => { editing = true; updateEditor(); f.title.focus(); });
    save.addEventListener("click", saveMetadata);
    discard.addEventListener("click", async () => {
      if (!(await mayDiscardDrawer())) return;
      Object.entries(savedValues).forEach(([key, value]) => { f[key].value = value ?? ""; });
      editing = false;
      updateEditor();
    });
    savePush.addEventListener("click", async () => {
      if (!(await saveMetadata())) return;
      pushCard(d, null);
      closeDrawer(true);
    });
    push.addEventListener("click", () => {
      if (editor.dirty() || editor.saving) return;
      pushCard(d, push);
      closeDrawer(true);
    });
    actions.append(edit, save, discard, savePush, push, status);
    updateEditor();

    if (d.external_url) {
      actions.append(el("a", { class: "btn", href: d.external_url, target: "_blank", rel: "noopener noreferrer", text: "Open in ABS ↗" }));
    }
    $("#drawer-actions").replaceChildren(...actions.childNodes);

    if ((d.chapters || []).length) {
      wrap.append(
        el("div", { style: "margin-top:24px" }, [
          el("div", { class: "panel-title", text: `${d.chapter_count || d.chapters.length} ${isPodcast ? "episodes" : "chapters"}` }),
          el(
            "ul",
            { class: "chapter-list" },
            d.chapters.map(ch =>
              el("li", {}, [
                el("span", { text: ch.title || "Untitled" }),
                el("span", { text: ch.duration ? `${Math.round(ch.duration / 60)}m` : "" }),
              ])
            )
          ),
        ])
      );
    }

    wrap.append(
      el("dl", { class: "kv", style: "margin-top:24px" }, [
        el("dt", { text: "Card ID" }), el("dd", { text: d.card_id }),
        el("dt", { text: "Yoto ID" }), el("dd", { text: d.yoto_card_id || "not pushed" }),
        el("dt", { text: "Source" }), el("dd", { text: d.source || "unknown" }),
        el("dt", { text: "Type" }), el("dd", { text: isPodcast ? "Podcast" : "Audiobook" }),
      ])
    );
    return wrap;
  }

  // ── routing ─────────────────────────────────────────────────────────────
  const ROUTES = {
    today: viewToday,
    find: viewFind,
    shelf: viewShelf,
    series: viewSeries,
    history: viewHistory,
    purchased: viewPurchased,
    settings: viewSettings,
  };
  const currentRoute = () => (location.hash.replace(/^#\//, "") || "today").split("?")[0];

  function render() {
    const route = ROUTES[currentRoute()] ? currentRoute() : "today";
    const root = $("#view-root");
    const focused = root.contains(document.activeElement) ? document.activeElement.id : null;
    const drafts = [...root.querySelectorAll("input[id], textarea[id], select[id]")].map(input => ({ id: input.id, value: input.value, checked: input.checked, start: input.selectionStart, end: input.selectionEnd }));
    root.innerHTML = "";
    ROUTES[route](root);
    for (const draft of drafts) {
      const input = document.getElementById(draft.id);
      if (!input || !root.contains(input)) continue;
      input.value = draft.value;
      if (typeof draft.checked === "boolean") input.checked = draft.checked;
      if (draft.id === focused) {
        input.focus({ preventScroll: true });
        if (draft.start !== null && draft.start !== undefined) input.setSelectionRange(draft.start, draft.end);
      }
    }

    document.querySelectorAll(".rail-item").forEach(a => {
      if (a.dataset.route === route) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });

    setCount("today", actionableCount());
    setCount("shelf", state.cards.length, "quiet");
    setCount("series", new Set(state.cards.filter(c => c.series).map(c => c.series)).size, "quiet");
    setCount("purchased", state.purchased.length, "quiet");

    const stamp = $("#last-updated");
    if (stamp) {
      stamp.textContent = state.loadedAt
        ? `View refreshed ${state.loadedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
        : "";
    }
  }

  function setCount(name, value, tone) {
    const node = document.querySelector(`[data-count="${name}"]`);
    if (!node) return;
    node.hidden = !value;
    node.textContent = String(value);
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  // ── boot ────────────────────────────────────────────────────────────────
  window.addEventListener("hashchange", () => {
    $("#nav-more").open = false;
    render();
    $("#view").focus();
  });

  document.addEventListener("DOMContentLoaded", () => {
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-scrim").addEventListener("click", closeDrawer);
    $("#refresh-all").addEventListener("click", event => hardRefresh(event.currentTarget));
    document.addEventListener("click", event => {
      const menu = $("#nav-more");
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", trapModalFocus, true);
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && activeModal() === $("#drawer")) closeDrawer();
    });
    window.addEventListener("beforeunload", event => {
      if (!drawerEditor?.dirty()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    if (!location.hash) location.hash = "#/today";
    render();
    loadAll();
    jobs.pollTimer = setTimeout(pollJobs, 1500);
  });

  // Exposed for the test harness.
  window.__v2 = {
    state, buildQueue, render, loadAll, shelfMatches, shelfState, findState,
    openDrawer, closeDrawer, confirmDialog, toast,
    jobs, startWorkflow, pollJobs, isQueued, renderTasks, clearTasks, pushCard, pushMany,
    hardRefresh, historyState, loadSource,
    mergeSeriesData, findMissingSeriesItems, checkSeries, seriesCache,
    discoverSeries, pastePromptDialog,
  };
})();
