/**
 * Tailors the master resume to one posting, on demand, with Claude.
 *
 * The board is a static page, so it can't hold an API key -- the same reason
 * the workflow trigger next door exists. It calls this instead, once per role,
 * the first time you press the button; the answer is stored in the board's
 * Firebase and never regenerated. That is the whole cost model: you pay for
 * the resumes you actually asked for.
 *
 * **The model never reads or writes LaTeX.** It is shown `site/master.json` --
 * the master parsed into plain text by `scripts/parse_resume.py` -- and answers
 * in plain text against a JSON schema. The board escapes its way back to LaTeX
 * when you download the `.tex`. So a bad answer can be a bad sentence; it
 * cannot be an unbalanced brace, a broken document, or an injected macro.
 *
 * It also cannot be a job you never had. Every entry is returned by `id` and
 * checked against the master, every bullet names the master bullet it came
 * from, and every number in a rewritten bullet must appear in that source
 * bullet -- which is the fabrication that actually matters on a resume, since
 * an invented "40%" reads exactly like a real one. A failing answer is sent
 * back once with its errors and then refused. See `validate` below.
 */

import Anthropic from "@anthropic-ai/sdk";

// Opus 5: the strongest judgment about what to cut and how to reframe a
// bullet, which is the whole job here. Thinking is on by default on this
// model, and `max_tokens` caps thinking *and* answer together -- hence the
// headroom. Roughly $0.18 a resume.
const MODEL = "claude-opus-5";
const MAX_TOKENS = 16000;

// One page, in body lines, for everything except the header, education and
// the skills run -- those are fixed. Derived from the master's own geometry
// (letterpaper, 0.4in top and bottom, 12pt leading) and measured against a
// real browser render, so it's the same budget the board's page-overflow
// marker draws. 126 characters is where a bullet wraps at 10pt Times.
const LINE_BUDGET = 40;
const BULLET_CPL = 126;

// A backstop, not a ledger. Two clicks in the same second can both read the
// same count and both proceed -- fine for a personal board, where the point
// is that a stranger who finds the endpoint can't run up a bill overnight.
const DAILY_CAP = 40;

const SCHEMA = {
  type: "object",
  properties: {
    emphasis: {
      type: "array",
      items: { type: "string" },
      description: "One to three words for what this version leads with, e.g. ['cloud infrastructure', 'CI/CD'].",
    },
    note: {
      type: "string",
      description: "One sentence, for the reader, on what you cut and why.",
    },
    sections: {
      type: "array",
      description: "Sections to include, in the order they should appear. Omit EDUCATION and TECHNICAL SKILLS.",
      items: {
        type: "object",
        properties: {
          name: { type: "string", description: "Exactly as spelled in the master." },
          entries: {
            type: "array",
            items: {
              type: "object",
              properties: {
                id: { type: "string", description: "The master entry's id." },
                bullets: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      source: {
                        type: "integer",
                        description: "0-based index of the master bullet this came from.",
                      },
                      text: {
                        type: "string",
                        description: "The bullet as it should read. Plain text, no LaTeX.",
                      },
                    },
                    required: ["source", "text"],
                    additionalProperties: false,
                  },
                },
              },
              required: ["id", "bullets"],
              additionalProperties: false,
            },
          },
        },
        required: ["name", "entries"],
        additionalProperties: false,
      },
    },
    skills: {
      type: "array",
      description: "Skills to keep, spelled exactly as in the master, in the master's order.",
      items: { type: "string" },
    },
  },
  required: ["emphasis", "note", "sections", "skills"],
  additionalProperties: false,
};

const SYSTEM = `You tailor one candidate's resume to one job posting. You are given their master resume as structured data and the posting; you return the subset that fits this posting best, with bullets rewritten where a rewrite genuinely helps.

This resume gets sent to an employer, so these are absolute:

- Every entry you return must be one of the master's entries, referenced by its id. Never invent an employer, a project, a job title, a date, or a degree.
- Every bullet names the master bullet it came from. Reword it, change what it leads with, cut detail, adopt the posting's vocabulary where it describes the same work. Do not add a technology, a responsibility, a scale, an outcome, or a number that isn't in the source bullet. If the posting wants something the candidate hasn't done, the answer is to leave it out, not to imply it.
- Every number in a bullet must appear in its source bullet, unchanged. Don't round it, don't convert it, don't add one.
- Skills come from the master's list, spelled exactly as given.

Shaping the page. It has to fit on one, and you are given a line budget:

- A job entry costs 2 lines before its bullets (organisation and role); a project costs 1.
- A bullet costs one line per ${BULLET_CPL} characters, rounded up. A 130-character bullet costs 2 lines and wastes most of the second, so tightening it to ${BULLET_CPL} is worth a whole line.
- Stay at or under the budget, and get close to it. A half-empty page reads as a thin candidate, not a focused one.

Judgement:

- Keep at least three entries under RELEVANT EXPERIENCE, and always keep the most recent one whatever the posting is about. A resume that skips the candidate's current internship reads as a gap, not as focus.
- Experience stays in the master's order, which is reverse-chronological. Out-of-order jobs read as hiding something.
- Projects carry no dates, so reorder them freely and lead with the most relevant.
- Sections you return nothing for are dropped. That is a real option for a section this posting has no use for.

Return only the object the schema asks for.`;

function bulletLines(text) {
  return Math.max(1, Math.ceil(text.length / BULLET_CPL));
}

/** Cost of one entry, in body lines, in the same units as LINE_BUDGET. */
function entryLines(kind, bullets) {
  const head = kind === "project" ? 1 : 2;
  return head + bullets.reduce((n, b) => n + bulletLines(b.text || ""), 0);
}

/** Every number in `text`, normalised so "2,400" and "2400" compare equal. */
function numbers(text) {
  return (text.match(/\d[\d,.]*/g) || []).map((n) => n.replace(/[,.]+$/, "").replace(/,/g, ""));
}

/**
 * Check the model's answer against the master.
 *
 * Returns a list of human-readable errors, which is also what gets handed
 * back to the model on the single retry -- so they're phrased for it to act
 * on, not just for a log.
 */
export function validate(master, answer) {
  const errors = [];
  const byId = new Map();
  const sectionNames = new Set();
  for (const sec of master.sections) {
    if (sec.kind !== "entries") continue;
    sectionNames.add(sec.name);
    for (const e of sec.entries) byId.set(e.id, { ...e, section: sec.name });
  }
  const skills = new Map(master.sections
    .filter((s) => s.kind === "skills")
    .flatMap((s) => s.skills)
    .map((s) => [s.toLowerCase(), s]));

  let lines = 0;
  let experience = 0;
  const seen = new Set();

  for (const sec of answer.sections || []) {
    if (!sectionNames.has(sec.name)) {
      errors.push(`Section "${sec.name}" is not in the master.`);
      continue;
    }
    if (/EDUCATION|SKILL/i.test(sec.name)) {
      errors.push(`Don't return "${sec.name}" — it's added automatically.`);
      continue;
    }
    lines += 2;                                    // the section heading + rule
    for (const entry of sec.entries || []) {
      const master_entry = byId.get(entry.id);
      if (!master_entry) {
        errors.push(`No entry with id "${entry.id}" exists in the master.`);
        continue;
      }
      if (master_entry.section !== sec.name) {
        errors.push(`Entry "${entry.id}" belongs to ${master_entry.section}, not ${sec.name}.`);
        continue;
      }
      if (seen.has(entry.id)) {
        errors.push(`Entry "${entry.id}" appears more than once.`);
        continue;
      }
      seen.add(entry.id);
      if (/EXPERIENCE/i.test(sec.name)) experience++;

      const bullets = entry.bullets || [];
      if (!bullets.length) {
        errors.push(`Entry "${entry.id}" has no bullets — drop the entry instead.`);
      }
      for (const b of bullets) {
        const src = master_entry.bullets[b.source];
        if (src === undefined) {
          errors.push(`Entry "${entry.id}" cites source bullet ${b.source}, which doesn't exist ` +
                      `(it has ${master_entry.bullets.length}).`);
          continue;
        }
        const invented = numbers(b.text).filter((n) => !numbers(src).includes(n));
        if (invented.length) {
          errors.push(`In "${entry.id}", the rewritten bullet has ${invented.join(", ")}, ` +
                      `which is not in its source bullet. Every number must come from the source.`);
        }
      }
      lines += entryLines(master_entry.kind, bullets);
    }
  }

  for (const s of answer.skills || []) {
    if (!skills.has(s.toLowerCase())) {
      errors.push(`"${s}" is not in the master's skills list.`);
    }
  }

  if (!seen.size) errors.push("No entries were returned.");
  if (experience < 3) {
    errors.push(`Only ${experience} entries under RELEVANT EXPERIENCE — keep at least three.`);
  }
  if (lines > LINE_BUDGET) {
    errors.push(`This runs to about ${lines} lines against a budget of ${LINE_BUDGET}. ` +
                `Cut a bullet or an entry, or tighten the longest bullets.`);
  }
  return errors;
}

/** Canonicalise: master spelling for skills, master order for entries. */
export function normalise(master, answer) {
  const byId = new Map();
  for (const sec of master.sections) {
    for (const e of sec.entries || []) byId.set(e.id, e);
  }
  const skillCase = new Map(master.sections
    .filter((s) => s.kind === "skills")
    .flatMap((s) => s.skills)
    .map((s) => [s.toLowerCase(), s]));

  const chosen = new Map();
  for (const sec of answer.sections || []) {
    for (const entry of sec.entries || []) chosen.set(entry.id, entry);
  }

  const sections = [];
  for (const sec of master.sections) {
    if (sec.kind === "skills") {
      const keep = (answer.skills || [])
        .map((s) => skillCase.get(s.toLowerCase()))
        .filter(Boolean);
      // Master order, deduped — the list is grouped (languages, then ML, then
      // cloud), and a resorted run reads as a keyword dump. `open`/`close`
      // are the master's own `{\small \justifying ... \par}` wrapper: drop
      // them and the line sets at 10pt instead of 9pt and runs wider, which
      // is exactly the kind of thing that quietly costs you the page.
      const wanted = new Set(keep);
      sections.push({
        name: sec.name,
        kind: "skills",
        skills: sec.skills.filter((s) => wanted.has(s)),
        open: sec.open || "",
        close: sec.close || "",
      });
      continue;
    }
    // Education is never tailored, so it's taken from the master whole and
    // the model is told not to return it.
    const isEducation = /EDUCATION/i.test(sec.name);
    let entries;
    if (isEducation) {
      entries = sec.entries.map((e) => ({ ...e }));
    } else {
      const order = sec.entries.filter((e) => chosen.has(e.id));
      const isProject = sec.entries.every((e) => e.kind === "project");
      // Projects carry no dates, so the model's ordering stands; dated
      // sections keep the master's reverse-chronological order.
      const source = isProject
        ? [...chosen.keys()].map((id) => byId.get(id)).filter((e) => e && sec.entries.includes(e))
        : order;
      entries = source.map((e) => ({
        ...e,
        bullets: chosen.get(e.id).bullets
          .filter((b) => e.bullets[b.source] !== undefined)
          .map((b) => b.text),
      }));
    }
    if (entries.length) sections.push({ name: sec.name, kind: "entries", entries });
  }
  return sections;
}

// ------------------------------------------------------------------ prompt
function describeMaster(master) {
  const out = [];
  for (const sec of master.sections) {
    if (sec.kind === "skills") {
      out.push(`## ${sec.name}\n${sec.skills.join(", ")}`);
      continue;
    }
    if (/EDUCATION/i.test(sec.name)) continue;      // always kept, never sent
    out.push(`## ${sec.name}`);
    for (const e of sec.entries) {
      const head = [e.org, e.place].filter(Boolean).join(" — ");
      const subs = e.subs.map(([l, r]) => `${l} (${r})`).join("; ");
      out.push(`\n### ${e.id}\n${head}${subs ? `\n${subs}` : ""}`);
      e.bullets.forEach((b, i) => out.push(`  [${i}] ${b}`));
    }
  }
  return out.join("\n");
}

function describePosting(posting, jobText) {
  const bits = [
    `Company: ${posting.company}`,
    `Role: ${posting.title}`,
    posting.location ? `Location: ${posting.location}` : "",
    `URL: ${posting.url}`,
  ].filter(Boolean);
  if (jobText) {
    bits.push(`\nThe posting itself:\n"""\n${jobText}\n"""`);
  } else {
    bits.push(
      "\nThe posting's own text couldn't be fetched — that job board blocks " +
      "automated access or renders in the browser. Work from the title, the " +
      "company, and what that company is known to do."
    );
  }
  return bits.join("\n");
}

// --------------------------------------------------------- the job posting
const BLOCKED_HOST = /^(localhost$|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|.*\.internal$)/i;

/**
 * Best effort, and genuinely optional: plenty of job boards are JavaScript
 * shells or block anything without a browser. When it works the tailoring is
 * markedly better, so it's worth eight seconds; when it doesn't, the model is
 * told so explicitly rather than being left to guess from a title alone.
 */
async function fetchPosting(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return "";
  }
  // The caller is authenticated, but this still turns a URL from a request
  // body into an outbound fetch — keep it on the public web.
  if (url.protocol !== "https:" || BLOCKED_HOST.test(url.hostname)) return "";

  try {
    const r = await fetch(url, {
      signal: AbortSignal.timeout(8000),
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        Accept: "text/html,application/xhtml+xml",
      },
    });
    if (!r.ok) return "";
    const type = r.headers.get("content-type") || "";
    if (!/text\/html|text\/plain|json/.test(type)) return "";

    const html = (await r.text()).slice(0, 400000);
    const text = html
      .replace(/<(script|style|noscript|svg|head)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&#\d+;/g, " ")
      .replace(/[ \t ]+/g, " ")
      .replace(/\n\s*\n\s*\n+/g, "\n\n")
      .trim();
    // Under a few hundred characters it's a cookie banner or a JS shell, not
    // a description — better to say "couldn't fetch" than to feed it noise.
    return text.length < 400 ? "" : text.slice(0, 12000);
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------- the call
async function ask(client, master, posting, jobText, priorAnswer, priorErrors) {
  const messages = [{
    role: "user",
    content:
      `Here is the master resume. Ids are what you reference; the numbers in ` +
      `brackets are the bullet indexes you cite as \`source\`.\n\n` +
      `${describeMaster(master)}\n\n` +
      `---\n\nThe posting:\n\n${describePosting(posting, jobText)}\n\n` +
      `---\n\nYour line budget is ${LINE_BUDGET}.`,
  }];
  if (priorAnswer) {
    messages.push({ role: "assistant", content: priorAnswer });
    messages.push({
      role: "user",
      content: `That answer was rejected:\n\n- ${priorErrors.join("\n- ")}\n\n` +
               `Fix those and return the whole object again.`,
    });
  }

  const resp = await client.beta.messages.create({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    // Opus 5's classifiers can decline a request; a security-flavoured
    // posting is exactly the sort of benign thing that occasionally trips
    // one. "default" routes by refusal category rather than pinning a model.
    betas: ["server-side-fallback-2026-07-01"],
    fallbacks: "default",
    system: SYSTEM,
    output_config: { format: { type: "json_schema", schema: SCHEMA } },
    messages,
  });

  if (resp.stop_reason === "refusal") {
    const why = (resp.stop_details && resp.stop_details.category) || "unspecified";
    throw new Error(`the model declined this request (${why})`);
  }
  if (resp.stop_reason === "max_tokens") {
    throw new Error("the answer ran past the token limit before it finished");
  }
  const text = (resp.content.find((b) => b.type === "text") || {}).text;
  if (!text) throw new Error("the model returned no answer");
  return text;
}

// ------------------------------------------------------------------ limits
function dayKey() {
  return `gen:${new Date().toISOString().slice(0, 10)}`;
}

/** Claim one generation against today's cap, or return how many are left. */
async function claim(env) {
  const key = dayKey();
  const used = Number(await env.RESUME_KV.get(key)) || 0;
  if (used >= DAILY_CAP) return { ok: false, used };
  // Claimed before the model is called, not after: a slow generation must
  // not leave a window where the cap can be raced.
  await env.RESUME_KV.put(key, String(used + 1), { expirationTtl: 172800 });
  return { ok: true, used: used + 1 };
}

/**
 * Timing-safe string compare. `a === b` on a secret leaks its length and its
 * matching prefix through how long the comparison takes.
 */
function sameSecret(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual
    ? crypto.subtle.timingSafeEqual(x, y)
    : x.reduce((d, v, i) => d | (v ^ y[i]), 0) === 0;
}

// ----------------------------------------------------------------- handler
/**
 * Streams newline-delimited JSON rather than answering in one shot.
 *
 * Opus 5 thinks before it answers, so a generation can take a couple of
 * minutes. A plain request that sends nothing for that long is a request
 * Cloudflare will cut off and a user will assume has hung — so progress goes
 * out as it happens, and the connection stays alive because it's being used.
 */
export async function handleResume(request, env, origin, cors) {
  const say = (o) => new TextEncoder().encode(JSON.stringify(o) + "\n");
  const fail = (reason, status = 200) =>
    new Response(JSON.stringify({ type: "error", reason }), {
      status,
      headers: { "Content-Type": "application/json", ...cors(origin) },
    });

  if (!env.ANTHROPIC_API_KEY) return fail("this board has no ANTHROPIC_API_KEY set", 501);
  if (!env.BOARD_KEY) return fail("this board has no BOARD_KEY set", 501);
  // The cap is the spend guard. Running without it because a binding is
  // missing would quietly turn a capped endpoint into an uncapped one, so it
  // refuses instead and says which piece is missing.
  if (!env.RESUME_KV) return fail("no RESUME_KV namespace is bound — see worker/README", 501);

  if (!sameSecret(request.headers.get("X-Board-Key") || "", env.BOARD_KEY)) {
    return fail("passphrase", 401);
  }

  const body = await request.json().catch(() => ({}));
  const posting = {
    id: String(body.id || ""),
    company: String(body.company || "").slice(0, 200),
    title: String(body.title || "").slice(0, 400),
    location: String(body.location || "").slice(0, 200),
    url: String(body.url || "").slice(0, 2000),
  };
  if (!posting.id || !posting.title) return fail("that request named no role", 400);

  const budget = await claim(env);
  if (!budget.ok) {
    return fail(`today's cap of ${DAILY_CAP} resumes is used up — it resets at midnight UTC`);
  }

  const { readable, writable } = new TransformStream();
  const w = writable.getWriter();

  (async () => {
    try {
      await w.write(say({ type: "progress", step: "Reading your master resume…" }));
      const mr = await fetch(`${env.BOARD_URL || origin}/master.json`, { cf: { cacheTtl: 300 } });
      if (!mr.ok) throw new Error(`master.json came back ${mr.status}`);
      const master = await mr.json();

      await w.write(say({ type: "progress", step: "Fetching the job posting…" }));
      const jobText = await fetchPosting(posting.url);

      await w.write(say({
        type: "progress",
        step: jobText
          ? `Tailoring against ${jobText.length.toLocaleString()} characters of posting…`
          : "That board blocks scrapers — tailoring from the title and company…",
      }));

      const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
      let text = await ask(client, master, posting, jobText);
      let answer = JSON.parse(text);
      let errors = validate(master, answer);

      if (errors.length) {
        await w.write(say({ type: "progress", step: "Checking it against your master…" }));
        text = await ask(client, master, posting, jobText, text, errors);
        answer = JSON.parse(text);
        errors = validate(master, answer);
      }
      if (errors.length) {
        throw new Error(`the answer didn't check out: ${errors[0]}`);
      }

      await w.write(say({
        type: "done",
        resume: {
          id: posting.id,
          company: posting.company,
          title: posting.title,
          url: posting.url,
          model: MODEL,
          at: Date.now(),
          used_posting: Boolean(jobText),
          emphasis: answer.emphasis || [],
          note: answer.note || "",
          sections: normalise(master, answer),
        },
        remaining: DAILY_CAP - budget.used,
      }));
    } catch (e) {
      await w.write(say({ type: "error", reason: (e && e.message) || "unknown error" }));
    } finally {
      await w.close();
    }
  })();

  return new Response(readable, {
    headers: {
      "Content-Type": "application/x-ndjson",
      "Cache-Control": "no-store",
      ...cors(origin),
    },
  });
}
