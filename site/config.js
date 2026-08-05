// Where the applied-checkbox state is stored.
//
// Paste your Firebase Realtime Database URL below -- it looks like
//   https://your-project-default-rtdb.firebaseio.com
// or, outside us-central1,
//   https://your-project-default-rtdb.europe-west1.firebasedatabase.app
//
// Setup, once (see README "Applied board" for the walkthrough):
//   1. console.firebase.google.com -> add project (no Analytics needed)
//   2. Build -> Realtime Database -> Create Database -> start in TEST mode
//   3. Rules tab -> paste:
//        {"rules":{"applied":{".read":true,".write":true},
//                  "ignored":{".read":true,".write":true}}}
//   4. Copy the URL shown at the top of the Data tab into databaseURL below
//
// This URL is meant to be public -- it is the whole point that anyone with
// the page can tick a box without signing in. It grants access to nothing
// but the `applied` and `ignored` lists. Do not widen the rules above to the
// database root: Firebase's "test mode" default is root-open, which lets
// anyone with this URL write anywhere in the database, not just these keys.
//
// Left blank, the page still works: it falls back to storing state in this
// browser only, and says so in a banner.
//
// ---------------------------------------------------------------------
// Where the board's "Re-scan now" and "Email me the recap" buttons send
// their request. This is the Cloudflare Worker in `worker/` -- it holds the
// GitHub token so the page never has to. Left blank, both buttons are
// hidden and everything else works as before. See README "Run it from the
// board" for the deploy steps.
window.TRACKER_CONFIG = {
  databaseURL: "https://internship-tracker-b0a79-default-rtdb.firebaseio.com/",
  triggerURL: "https://internship-tracker-trigger.connor5190.workers.dev",
};
