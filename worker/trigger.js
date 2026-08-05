/**
 * Lets the public board start this repo's workflows without shipping a
 * GitHub token to the browser.
 *
 * The board is deliberately unauthenticated -- anyone with the URL can tick
 * a checkbox -- so this endpoint is reachable by anyone too. What it does
 * *not* do is hand out credentials: the token lives in Worker secrets and
 * never crosses the wire to a client. That is the whole point. A token
 * embedded in the page would not merely be readable, it would be
 * short-lived: GitHub scans public repositories for its own tokens and
 * revokes them automatically.
 *
 * Two further limits keep a stranger who finds the page from being able to
 * do much with it:
 *
 *   - Only the two workflows named below can be started. A request naming
 *     anything else is rejected, so this can't be turned into a general
 *     "run arbitrary CI" button.
 *   - Each has a cooldown, and GitHub's own run history is the store that
 *     enforces it. That means no KV namespace to create and bind, and the
 *     limit can't drift out of sync with what actually ran -- a run started
 *     from the Actions tab counts against the cooldown too.
 */

const REPO = "Connor5190/internshipTracker";
const REF = "main";

// workflow file -> seconds that must pass before it can be started again.
// The recap is the stricter of the two because its side effect lands in
// somebody's inbox; a re-scan only costs a couple of CI minutes.
const ALLOWED = {
  "daily-recap.yml": 600,
  "update-board.yml": 300,
};

const headers = (origin) => ({
  "Access-Control-Allow-Origin": origin,
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
  "Cache-Control": "no-store",
});

const json = (body, status, origin) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers(origin) },
  });

function gh(env, path, init) {
  return fetch("https://api.github.com" + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub rejects API calls with no User-Agent.
      "User-Agent": "internship-tracker-board",
      ...((init && init.headers) || {}),
    },
  });
}

async function latestRun(env, workflow) {
  const r = await gh(env, `/repos/${REPO}/actions/workflows/${workflow}/runs?per_page=1`);
  if (!r.ok) return null;
  const d = await r.json().catch(() => ({}));
  return (d.workflow_runs || [])[0] || null;
}

const known = (w) => typeof w === "string" && Object.prototype.hasOwnProperty.call(ALLOWED, w);

export default {
  async fetch(request, env) {
    const origin = env.ALLOWED_ORIGIN || "*";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: headers(origin) });
    }

    const url = new URL(request.url);

    // ---- how's it going?
    if (url.pathname === "/status") {
      const workflow = url.searchParams.get("workflow");
      if (!known(workflow)) return json({ error: "unknown workflow" }, 400, origin);

      const run = await latestRun(env, workflow);
      if (!run) return json({ run: null }, 200, origin);

      // A run takes a few seconds to appear after dispatch. Without `since`
      // the caller would see the *previous* run sitting at completed and
      // conclude instantly that its own had finished.
      const since = Number(url.searchParams.get("since") || 0);
      if (since && Date.parse(run.created_at) < since - 60000) {
        return json({ run: null }, 200, origin);
      }
      return json(
        {
          run: { status: run.status, conclusion: run.conclusion, url: run.html_url },
        },
        200,
        origin
      );
    }

    // ---- start one
    if (request.method !== "POST") return json({ error: "POST only" }, 405, origin);

    const body = await request.json().catch(() => ({}));
    const workflow = body && body.workflow;
    if (!known(workflow)) return json({ error: "unknown workflow" }, 400, origin);

    const last = await latestRun(env, workflow);
    if (last) {
      if (last.status === "queued" || last.status === "in_progress") {
        // Not an error -- the caller wanted a run and there is one, so hand
        // it back to watch rather than starting a second.
        return json({ ok: false, reason: "running", url: last.html_url }, 200, origin);
      }
      const age = (Date.now() - Date.parse(last.created_at)) / 1000;
      if (age < ALLOWED[workflow]) {
        return json(
          { ok: false, reason: "cooldown", retryAfter: Math.ceil(ALLOWED[workflow] - age) },
          200,
          origin
        );
      }
    }

    const r = await gh(env, `/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: REF }),
    });
    if (r.status !== 204) {
      const detail = await r.text().catch(() => "");
      return json(
        { ok: false, reason: "github", status: r.status, detail: detail.slice(0, 200) },
        200,
        origin
      );
    }
    return json({ ok: true, dispatchedAt: Date.now() }, 200, origin);
  },
};
