// Headless check of the cross-page column-width binding UI.  Requires a live
// server.  Usage: node tests/ui_binding.js <baseUrl>
const { JSDOM } = require("jsdom");

const base = process.argv[2] || "http://127.0.0.1:5057";
const CSV = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,跨格,90,10,110,40
`;

function assert(condition, message) {
  if (!condition) throw new Error("FAIL: " + message);
  console.log("ok - " + message);
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function upload(filename) {
  const form = new FormData();
  form.append("file", new Blob([CSV], { type: "text/csv" }), filename);
  const response = await fetch(`${base}/upload`, {
    method: "POST", body: form, redirect: "manual",
  });
  return response.headers.get("location").split("/").pop();
}

async function setLines(jobId, horizontal, vertical) {
  const response = await fetch(`${base}/api/jobs/${jobId}/lines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ horizontal, vertical }),
  });
  return response.json();
}

async function state(jobId) {
  return fetch(`${base}/api/jobs/${jobId}`).then((r) => r.json());
}

async function open(jobId) {
  const page = await fetch(`${base}/jobs/${jobId}`).then((r) => r.text());
  const dom = new JSDOM(page, {
    url: `${base}/jobs/${jobId}`,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = (url, options) => fetch(new URL(url, base), options);
  window.alert = (message) => { throw new Error("UI alert: " + message); };
  await new Promise((resolve) => window.addEventListener("load", resolve));
  await wait(300);
  return window;
}

function click(window, node) {
  node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

async function makeJob(filename) {
  const jobId = await upload(filename);
  await setLines(jobId, [0, 100, 200], [0, 100, 200, 300]);
  return jobId;
}

(async () => {
  const reference = await makeJob("ref.csv");
  const target = await makeJob("target.csv");

  // --- Bind from the target page's binding card ---------------------------
  let window = await open(target);
  let doc = window.document;
  const select = doc.getElementById("reference-select");
  assert(select, "target page offers a reference picker");
  select.value = String(reference);
  click(window, doc.getElementById("bind"));
  await wait(600);

  window = await open(target);
  doc = window.document;
  const bound = await state(target);
  assert(bound.binding.follows === Number(reference), "target now follows the reference");
  const verticalInput = doc.getElementById("vertical-input");
  assert(verticalInput.readOnly, "follower's vertical input is read-only");
  assert(doc.getElementById("add-vertical").disabled,
    "follower cannot add vertical lines");
  assert(!doc.getElementById("unbind").hidden, "unbind control is available");

  // --- Follower can still edit horizontals --------------------------------
  const horizontalInput = doc.getElementById("horizontal-input");
  horizontalInput.value = "0, 120, 240";
  horizontalInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  await wait(80);
  click(window, doc.getElementById("apply-lines"));
  await wait(500);
  const edited = await state(target);
  assert(JSON.stringify(edited.horizontal) === JSON.stringify([0, 120, 240]),
    "follower applies its own horizontal lines");
  assert(JSON.stringify(edited.vertical) === JSON.stringify([0, 100, 200, 300]),
    "follower keeps the reference-derived verticals");

  // --- Reference vertical change flows through ----------------------------
  await setLines(reference, [0, 100, 200], [0, 250, 500, 1000]);
  const followed = await state(target);
  assert(JSON.stringify(followed.vertical) === JSON.stringify([0, 75, 150, 300]),
    "reference vertical change is converted onto the target borders");

  // --- Reference page shows its follower and refuses deletion -------------
  const refWindow = await open(reference);
  const refDoc = refWindow.document;
  const followerLinks = refDoc.querySelectorAll(".follower-list a");
  assert(followerLinks.length === 1, "reference page lists one follower");
  assert(!refDoc.getElementById("bind"), "reference page cannot bind to another page");
  const deleteForm = refDoc.querySelector('form[action$="/delete"]');
  assert(deleteForm, "delete form is present");
  deleteForm.dispatchEvent(new refWindow.Event("submit", { bubbles: true }));
  await wait(400);
  const stillThere = await fetch(`${base}/api/jobs/${reference}`);
  assert(stillThere.status === 200, "bound reference page cannot be deleted");

  // --- Pause, then confirm from the UI ------------------------------------
  await setLines(reference, [0, 100, 200], [0, 0.01, 500, 1000]);
  const paused = await state(target);
  assert(paused.binding.paused, "invalid conversion pauses the follower");
  assert(JSON.stringify(paused.vertical) === JSON.stringify([0, 75, 150, 300]),
    "paused follower keeps its previous column widths");

  const pausedWindow = await open(target);
  const pausedDoc = pausedWindow.document;
  const notice = pausedDoc.getElementById("binding-paused");
  assert(notice, "paused page explains why following stopped");
  assert(notice.textContent.includes("暂停"), "pause notice is visible");
  assert(pausedDoc.getElementById("confirm-binding"),
    "paused page offers a confirm control");

  // Fix the reference, then confirm: following resumes.
  await setLines(reference, [0, 100, 200], [0, 200, 500, 1000]);
  const readyWindow = await open(target);
  const readyDoc = readyWindow.document;
  click(readyWindow, readyDoc.getElementById("confirm-binding"));
  await wait(600);
  const resumed = await state(target);
  assert(!resumed.binding.paused, "confirming resumes following");
  assert(JSON.stringify(resumed.vertical) === JSON.stringify([0, 60, 150, 300]),
    "confirm adopts the reference's current widths");

  // --- Unbind keeps the lines of that moment ------------------------------
  const unbindWindow = await open(target);
  const unbindDoc = unbindWindow.document;
  const linesBefore = (await state(target)).vertical;
  unbindWindow.confirm = () => true;
  click(unbindWindow, unbindDoc.getElementById("unbind"));
  await wait(600);
  const unbound = await state(target);
  assert(unbound.binding.follows === null, "unbind removes the relation");
  assert(JSON.stringify(unbound.vertical) === JSON.stringify(linesBefore),
    "unbind keeps the current lines");
  const freeDoc = (await open(target)).document;
  assert(!freeDoc.getElementById("vertical-input").readOnly,
    "unbound page can edit verticals again");

  console.log("done");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
