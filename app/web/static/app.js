// Every change a page makes is a JSON call to /api/v1, the same API scripts and CI use.
// Elements with data-api send one: forms on submit, buttons on click.

function csrfToken() {
  const found = document.cookie.split("; ").find((c) => c.startsWith("agent_eval_csrf="));
  return found ? decodeURIComponent(found.split("=")[1]) : "";
}

// Only a path on this site, so a crafted link cannot send someone elsewhere after sign-in.
function safeNext() {
  const next = new URLSearchParams(location.search).get("next") || "/settings";
  return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/settings";
}

async function fieldValue(field) {
  if (field.dataset.type === "int") return Number(field.value);
  if (field.dataset.type === "list") return field.value.split(/[\s,]+/).filter(Boolean);
  // A JSON Lines file, one object a line, as the SDK's Eval reads it.
  if (field.dataset.type === "jsonl") {
    const text = await field.files[0].text();
    return text.split("\n").filter((line) => line.trim()).map((line, n) => {
      try {
        return JSON.parse(line);
      } catch {
        throw new Error(`Line ${n + 1} of the file is not valid JSON.`);
      }
    });
  }
  // A date is the start of that day in UTC, which is how windows are stored.
  if (field.dataset.type === "date") return `${field.value}T00:00:00Z`;
  return field.value;
}

// A field named "baseline.experiment_id" fills body.baseline.experiment_id, and a number in
// the name makes a list: "labels.0.answer" fills body.labels[0].answer. A field the form's
// URL names, as in /datasets/{dataset_id}/cases, goes into the URL instead.
async function formBody(form, used) {
  const body = {};
  for (const field of form.elements) {
    if (!field.name || field.value === "" || used.has(field.name)) continue;
    if ((field.type === "radio" || field.type === "checkbox") && !field.checked) continue;
    const path = field.name.split(".");
    let target = body;
    path.slice(0, -1).forEach((key, n) => {
      target = target[key] = target[key] || (/^\d+$/.test(path[n + 1]) ? [] : {});
    });
    target[path[path.length - 1]] = await fieldValue(field);
  }
  return body;
}

function describeError(status, payload) {
  const detail = payload && (payload.detail || payload);
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  if (detail && detail.message) return detail.message;
  if (detail && detail.error) return detail.error.replaceAll("_", " ");
  return `Request failed (${status})`;
}

async function send(element) {
  if (element.dataset.confirm && !window.confirm(element.dataset.confirm)) return;
  const form = element.tagName === "FORM" ? element : null;
  const scope = form || element.closest("td, .actions, form") || element.parentElement;
  const errorBox = scope.querySelector(".error");
  if (errorBox) errorBox.textContent = "";
  const method = element.dataset.method || "POST";
  const options = {
    method,
    credentials: "same-origin",
    headers: { "x-csrf-token": csrfToken(), accept: "application/json" },
  };
  const used = new Set();
  const url = element.dataset.api.replace(/\{([a-z_]+)\}/g, (_, name) => {
    used.add(name);
    return encodeURIComponent(form.elements[name].value);
  });
  if (form || element.dataset.body) {
    options.headers["content-type"] = "application/json";
    try {
      options.body = form ? JSON.stringify(await formBody(form, used)) : element.dataset.body;
    } catch (error) {
      if (errorBox) errorBox.textContent = error.message;
      return;
    }
  }
  const response = await fetch(url, options);
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401 && !url.endsWith("/login")) {
      location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      return;
    }
    if (errorBox) errorBox.textContent = describeError(response.status, payload);
    return;
  }
  if (element.dataset.done) {
    const box = scope.querySelector(".done");
    box.textContent = element.dataset.done;
    box.hidden = false;
    return;
  }
  if (element.dataset.show) {
    const box = scope.querySelector(".secret");
    box.querySelector("code").textContent = payload[element.dataset.show];
    box.hidden = false;
    return;
  }
  const redirect = element.dataset.redirect;
  if (redirect === "next") location.href = safeNext();
  else if (redirect) location.href = redirect.replace("{id}", payload ? encodeURIComponent(payload.id) : "");
  else location.reload();
}

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-api]");
  if (!form) return;
  event.preventDefault();
  send(form);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-api]");
  if (!button || button.closest("form[data-api]")) return;
  send(button);
});

// While something is being worked on, such as a report, its page checks back on its own.
const poll = document.querySelector("[data-poll]");
if (poll) setTimeout(() => location.reload(), Number(poll.dataset.poll) || 3000);
