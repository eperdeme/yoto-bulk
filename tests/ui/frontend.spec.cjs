const { test, expect } = require("@playwright/test");

const books = [
  { card_id: "book-a", title: "Review Book A", authors: ["Review Author"], source: "abs", media_type: "audiobook", chapter_count: 1, needs_push: true, series: "A very long series name that must fit on a phone without horizontal scrolling", sequence_number: 1, updated_at: "2026-10-01T00:00:00Z", cover_url: "/unavailable-cover.jpg" },
  { card_id: "book-b", title: "Review Book B", authors: ["Review Author"], source: "abs", media_type: "podcast", chapter_count: 1, needs_push: false, yoto_card_id: "yoto-b", updated_at: "2026-10-09T00:00:00Z" },
];

const details = card => ({ ...card, description: "Original description", diff_fields: [], chapters: [{ title: "Opening", duration: 60 }] });

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", async route => {
    const endpoint = new URL(route.request().url()).pathname;
    const fixtures = {
      "/api/catalog/libraries": [],
      "/api/auth/all-loans": {},
      "/api/auth/archives": [],
      "/api/yoto/cards": books,
      "/api/yoto/orphans": [],
      "/api/yoto/auth/status": { authenticated: true },
      "/api/credentials": { writable: true, libraries: [] },
      "/api/auth/all-history": {},
      "/api/yoto/family-library": [],
      "/api/yoto/duplicates": { groups: [] },
      "/api/yoto/sync/status": { last_abs_sync: null, last_yoto_push: null },
      "/api/yoto/jobs": [],
      "/api/jobs": [],
      "/api/yoto/cards/book-a": details(books[0]),
      "/api/yoto/cards/book-b": details(books[1]),
    };
    if (route.request().method() !== "GET" || !(endpoint in fixtures)) {
      await route.fulfill({ status: 501, json: { detail: `Unexpected test request: ${endpoint}` } });
      return;
    }
    await route.fulfill({ json: fixtures[endpoint] });
  });
  await page.goto("/#/shelf");
  await expect(page.locator(".row-open")).toHaveCount(2);
});

test("late detail response cannot overwrite the active book", async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route("**/api/yoto/cards/book-a", async route => {
    await gate;
    await route.fulfill({ json: details(books[0]) }).catch(() => {});
  });
  const requested = page.waitForRequest("**/api/yoto/cards/book-a");
  const opening = page.evaluate(() => window.__v2.openDrawer(window.__v2.state.cards[0]));
  try {
    await requested;
    await page.locator("#drawer-close").click();
    await page.getByRole("button", { name: "Details for Review Book B", exact: true }).click();
    await expect(page.locator("#f-title")).toHaveValue("Review Book B");
    release();
    await opening;
    await expect(page.locator("#drawer-title")).toHaveText("Review Book B");
    await expect(page.locator("#f-title")).toHaveValue("Review Book B");
  } finally {
    release();
    await opening;
  }
});

test("failed detail loading never exposes an incomplete editor", async ({ page }) => {
  await page.route("**/api/yoto/cards/book-a", route => route.fulfill({ status: 503, json: { detail: "Audiobookshelf unavailable" } }));
  await page.getByRole("button", { name: "Details for Review Book A", exact: true }).click();
  await expect(page.getByText("Details unavailable", { exact: true })).toBeVisible();
  await expect(page.locator("#f-title")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
});

test("drawer traps keyboard focus and restores it on close", async ({ page }) => {
  const opener = page.getByRole("button", { name: "Details for Review Book A", exact: true });
  await opener.click();
  await expect(page.locator("#f-title")).toHaveValue("Review Book A");
  await page.locator("#drawer-close").focus();
  await page.keyboard.press("Shift+Tab");
  expect(await page.evaluate(() => Boolean(document.activeElement.closest("#drawer")))).toBe(true);
  expect(await page.locator(".shell").evaluate(node => node.inert)).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.locator("#drawer")).toBeHidden();
  await expect(opener).toBeFocused();
});

test("unsaved edits require explicit discard", async ({ page }) => {
  await page.getByRole("button", { name: "Details for Review Book A", exact: true }).click();
  await page.getByRole("button", { name: "Edit metadata", exact: true }).click();
  await page.locator("#f-title").fill("Unsaved title");
  await expect(page.getByRole("button", { name: "Push to Yoto", exact: true, includeHidden: true })).toBeDisabled();
  await page.locator("#drawer-close").click();
  await expect(page.getByRole("dialog", { name: "Discard unsaved changes?" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.locator("#f-title")).toHaveValue("Unsaved title");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await expect(page.locator("#drawer")).toBeHidden();
});

test("save and push persists metadata before enqueueing the push", async ({ page }) => {
  const calls = [];
  await page.route("**/api/yoto/cards/book-a/metadata", async route => {
    calls.push({ action: "save", data: route.request().postDataJSON() });
    await route.fulfill({ json: { status: "updated" } });
  });
  await page.route("**/api/yoto/jobs", async route => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    const spec = route.request().postDataJSON();
    calls.push({ action: spec.action });
    await route.fulfill({ status: 202, json: { id: "push-test", status: "queued", stage: "Queued", spec, label: spec.label, group: "Pushing to Yoto", progress: {} } });
  });
  await page.getByRole("button", { name: "Details for Review Book A", exact: true }).click();
  await page.getByRole("button", { name: "Edit metadata", exact: true }).click();
  await page.locator("#f-title").fill("Saved title");
  await page.getByRole("button", { name: "Save and Push", exact: true }).click();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls.map(call => call.action)).toEqual(["save", "push"]);
  expect(calls[0].data.title).toBe("Saved title");
  await expect(page.locator("#drawer")).toBeHidden();
});

test("shelf renders while an unrelated history request is pending", async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route("**/api/auth/all-history", async route => {
    await gate;
    await route.fulfill({ json: {} }).catch(() => {});
  });
  try {
    await page.reload();
    await expect(page.locator(".row-open")).toHaveCount(2);
    expect(await page.evaluate(() => window.__v2.state.sources.history.loading)).toBe(true);
  } finally { release(); }
});

test("failed shelf loading shows an error rather than an empty library", async ({ page }) => {
  await page.route("**/api/yoto/cards", route => route.fulfill({ status: 503, json: { detail: "Service unavailable" } }));
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Service unavailable");
  await expect(page.getByText("Your shelf is empty", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry Shelf", exact: true })).toBeVisible();
});

test("failed refresh retains books and the current filter draft", async ({ page }) => {
  await page.getByRole("searchbox", { name: "Filter shelf", exact: true }).fill("Review");
  await page.route("**/api/yoto/cards", route => route.fulfill({ status: 503, json: { detail: "Service unavailable" } }));
  await page.evaluate(() => window.__v2.loadSource("cards"));
  await expect(page.getByRole("alert")).toContainText("last loaded data retained");
  await expect(page.locator(".row-open")).toHaveCount(2);
  await expect(page.getByRole("searchbox", { name: "Filter shelf", exact: true })).toHaveValue("Review");
});

test("catalogue failures remain unknown and re-check fetches fresh data", async ({ page }) => {
  let requests = 0;
  await page.route("**/api/yoto/series/completeness?*", route => { requests++; return route.fulfill({ status: 503, json: { detail: "Catalogue unavailable" } }); });
  await page.route("**/api/catalog/series-lookup?*", route => { requests++; return route.fulfill({ status: 503, json: { detail: "BorrowBox unavailable" } }); });
  const result = await page.evaluate(async () => {
    const host = document.createElement("div");
    const button = document.createElement("button");
    await window.__v2.checkSeries("Review Series", [], host, button);
    const first = host.textContent;
    await window.__v2.checkSeries("Review Series", [], host, button);
    return { first, second: host.textContent };
  });
  expect(requests).toBe(4);
  expect(result.first).toContain("Unknown");
  expect(result.second).toContain("Missing books could not be determined");
  expect(result.second).not.toContain("Nothing missing");
});

test("matching catalogue titles do not imply a complete series", async ({ page }) => {
  await page.route("**/api/yoto/series/completeness?*", route => route.fulfill({ json: { name: "Review Series", source: "abs_provider", items: [{ title: "Review Book A", provider: "audible", series_number: 1 }] } }));
  await page.route("**/api/catalog/series-lookup?*", route => route.fulfill({ json: { name: "Review Series", items: [] } }));
  const text = await page.evaluate(async () => {
    const host = document.createElement("div");
    await window.__v2.checkSeries("Review Series", [window.__v2.state.cards[0]], host, document.createElement("button"));
    return host.textContent;
  });
  expect(text).toContain("Partial catalogue");
  expect(text).toContain("Audible");
  expect(text).toContain("Checked");
  expect(text).toContain("No known gaps in this catalogue");
});

test("mobile navigation and series headings fit without page overflow", async ({ page }) => {
  await expect(page.getByRole("button", { name: "Refresh view", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync from Audiobookshelf", exact: true })).toBeVisible();
  await page.goto("/#/series");
  await expect(page.getByText(books[0].series, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("shelf filters media and sorts by update time", async ({ page }) => {
  await page.getByLabel("Sort shelf", { exact: true }).selectOption("recent");
  await expect(page.locator(".row-title").first()).toHaveText("Review Book B");
  await page.getByLabel("Media type", { exact: true }).selectOption("audiobook");
  await expect(page.locator(".row-open")).toHaveCount(1);
  await expect(page.locator(".row-title").first()).toHaveText("Review Book A");
  await page.getByLabel("Media type", { exact: true }).selectOption("podcast");
  await expect(page.locator(".row-title").first()).toHaveText("Review Book B");
});

test("bulk selection is explicit and confirmed before pushing", async ({ page }) => {
  await page.getByRole("checkbox", { name: "Select visible books", exact: true }).check();
  await expect(page.getByText("2 selected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Push selected", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Push 2 cards to Yoto?" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Clear selection", exact: true }).click();
  await expect(page.getByRole("button", { name: "Push selected", exact: true })).toBeDisabled();
});

test("broken artwork gets a visible local fallback", async ({ page }) => {
  await expect(page.locator('.cover-placeholder[title="Cover unavailable"]')).toBeVisible();
  await expect(page.locator('.cover[src="/unavailable-cover.jpg"]')).toHaveCount(0);
});

test("running tasks and progress recover after a page reload", async ({ page }) => {
  const task = { id: "running-1", status: "running", stage: "Transcoding audio", spec: { action: "push", card_id: "book-a" }, label: "Review Book A", group: "Pushing to Yoto", progress: { tracks_done: 2, tracks_total: 5 }, created_at: "2026-10-09T10:00:00Z" };
  await page.route("**/api/yoto/jobs", route => route.fulfill({ json: [task] }));
  await page.reload();
  await expect(page.locator("#tasks")).toBeVisible();
  await expect(page.locator("#tasks")).toContainText("Transcoding audio");
  await expect(page.locator("#tasks")).toContainText("2/5 tracks");
  await page.reload();
  await expect(page.locator("#tasks")).toContainText("Review Book A");
});

test("failed tasks have readable details and explicit retry", async ({ page }) => {
  let retried = false;
  const task = { id: "failed-1", status: "error", stage: "Failed", error: "Audiobookshelf TLS certificate has expired", spec: { action: "push", card_id: "book-a", label: "Review Book A" }, label: "Review Book A", group: "Pushing to Yoto", progress: {}, created_at: "2026-10-09T10:00:00Z" };
  await page.route("**/api/yoto/jobs", route => {
    if (route.request().method() === "GET") return route.fulfill({ json: retried ? [] : [task] });
    retried = true;
    return route.fulfill({ status: 202, json: { ...task, id: "retry-1", status: "queued", stage: "Queued", error: null } });
  });
  await page.reload();
  await page.getByText("Failure details", { exact: true }).click();
  await expect(page.locator("#tasks")).toContainText("TLS certificate has expired");
  await page.getByRole("button", { name: "Retry Review Book A", exact: true }).click();
  await expect.poll(() => retried).toBe(true);
  await expect(page.locator("#tasks")).not.toContainText("TLS certificate has expired");
});

test("failed metadata save retains edits and prevents an accidental push", async ({ page }) => {
  await page.route("**/api/yoto/cards/book-a/metadata", route => route.fulfill({ status: 503, json: { detail: "Audiobookshelf unavailable" } }));
  await page.getByRole("button", { name: "Details for Review Book A", exact: true }).click();
  await page.getByRole("button", { name: "Edit metadata", exact: true }).click();
  await page.locator("#f-title").fill("Keep this draft");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator("#f-title")).toHaveValue("Keep this draft");
  await expect(page.getByText("Save failed: Audiobookshelf unavailable", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Push to Yoto", exact: true, includeHidden: true })).toBeDisabled();
});

test("all returned chapters remain visible in long books", async ({ page }) => {
  const book = { ...details(books[0]), chapter_count: 47, chapters: Array.from({ length: 47 }, (_, index) => ({ title: `Chapter ${index + 1}`, duration: 60 })) };
  await page.route("**/api/yoto/cards/book-a", route => route.fulfill({ json: book }));
  await page.getByRole("button", { name: "Details for Review Book A", exact: true }).click();
  await expect(page.locator(".chapter-list li")).toHaveCount(47);
  await page.locator(".chapter-list li").last().scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "Push to Yoto", exact: true })).toBeInViewport();
});

test("data renders even when animation frames are paused", async ({ page }) => {
  await page.addInitScript(() => { window.requestAnimationFrame = () => 0; });
  await page.reload();
  await expect(page.locator(".row-open")).toHaveCount(2);
});

test("series with many missing volumes cannot widen the phone layout", async ({ page }) => {
  await page.evaluate(() => {
    window.__v2.state.cards.push({ ...window.__v2.state.cards[0], card_id: "later-book", title: "A later volume", sequence_number: 25 });
    location.hash = "#/series";
  });
  await expect(page.getByText(/missing #2, #3/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("task polling does not advance library freshness", async ({ page }) => {
  const result = await page.evaluate(async () => {
    await window.__v2.loadAll();
    window.__v2.state.loadedAt = new Date("2026-10-01T00:00:00Z");
    await window.__v2.loadSource("yotoJobs");
    return window.__v2.state.loadedAt.toISOString();
  });
  expect(result).toBe("2026-10-01T00:00:00.000Z");
});