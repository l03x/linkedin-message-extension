// Runs inside the LinkedIn tab via chrome.scripting.executeScript, only when you click Generate.
// It only READS the page: no clicks, no typing, no network. It must stay self-contained
// (executeScript serializes the function source, so it can't use anything outside it).

export function extractProfile() {
  const MAX_CHARS = 15000;
  // LinkedIn UI labels that carry no information about the person.
  const NOISE = new Set(
    [
      "connect", "message", "follow", "following", "more", "pending", "unfollow", "remove connection",
      "contact info", "show all", "see more", "…see more", "...see more", "see less", "show more", "show less",
      "show translation", "see translation", "send profile in a message", "save to pdf", "report / block",
      "about this profile", "open to", "add profile section", "enhance profile", "resources", "endorse",
      "like", "comment", "repost", "send", "visit my website", "view my portfolio", "· 1st", "· 2nd", "· 3rd",
      "1st", "2nd", "3rd", "3rd+", "he/him", "she/her", "they/them", "show credential", "show project",
      "verified", "premium", "message (opens in new window)", "loaded", "status is reachable", "status is offline",
    ].map((s) => s.toLowerCase())
  );
  const NOISE_PATTERNS = [
    /^show all\b/i,
    /^(?:connect|message|follow|more|pending|save)+$/i, // adjacent action buttons run together

    /^(…|\.\.\.)\s*(see|show) more$/i,
    /^\d+\s+(endorsements?|comments?|reposts?)$/i,
    /^(feed post number|posts? loaded)/i,
  ];

  const clean = (s) => (s || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();

  const root = document.querySelector("main") || document.body;
  const lines = [];
  for (const raw of (root.innerText || "").split("\n")) {
    const line = clean(raw);
    if (!line) continue;
    const lower = line.toLowerCase();
    if (NOISE.has(lower) || NOISE_PATTERNS.some((re) => re.test(line))) continue;
    // LinkedIn renders much text twice (visible span + screen-reader span), back to back.
    if (lines.length && lines[lines.length - 1] === line) continue;
    lines.push(line);
  }

  const h1 = root.querySelector("h1");
  let name = clean(h1 && h1.innerText);
  if (!name) name = clean((document.title || "").split("|")[0]).replace(/^\(\d+\)\s*/, "");

  let headline = "";
  const nameIdx = name ? lines.indexOf(name) : -1;
  if (nameIdx >= 0) {
    headline = lines.slice(nameIdx + 1).find((l) => l.length > 3 && !/^(·|\(|\d)/.test(l)) || "";
  }
  if (!headline) {
    const el = root.querySelector(".text-body-medium");
    headline = clean(el && el.innerText);
  }

  let rawText = "";
  for (const line of lines) {
    if (rawText.length + line.length + 1 > MAX_CHARS) break;
    rawText += line + "\n";
  }

  // Current company, for the backend's company research. Read from attributes already on the page: the top
  // card's "Current company: X" button, and the /company/<slug>/ link of the first Experience entry. The slug
  // is only a cache key; nothing ever opens it.
  const company = { name: "", linkedin_slug: null };
  const current = root.querySelector('[aria-label^="Current company" i], [aria-label^="Entreprise actuelle" i]');
  if (current) {
    company.name = clean(current.getAttribute("aria-label"))
      .replace(/^(current company|entreprise actuelle)\s*:\s*/i, "")
      .replace(/\.\s*(click|cliquez).*$/i, "");
  }
  const anchor = document.getElementById("experience");
  const section = anchor && anchor.closest("section");
  const link = section && section.querySelector('a[href*="/company/"]');
  const slugMatch = link && (link.getAttribute("href") || "").match(/\/company\/([^/?#]+)/);
  if (slugMatch) company.linkedin_slug = decodeURIComponent(slugMatch[1]).toLowerCase().slice(0, 200);

  const slug = (location.pathname.match(/^\/in\/([^/]+)/) || [])[1] || "";
  return {
    url: `https://www.linkedin.com/in/${slug}/`,
    name: name.slice(0, 300),
    headline: headline.slice(0, 1000),
    raw_text: rawText.trim(),
    company: { name: company.name.slice(0, 300), linkedin_slug: company.linkedin_slug },
    truncated: lines.join("\n").length > rawText.length,
  };
}
