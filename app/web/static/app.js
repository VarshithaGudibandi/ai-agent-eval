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

function formBody(form) {
  const body = {};
  for (const field of form.elements) {
    if (!field.name || field.value === "") continue;
    body[field.name] = field.dataset.type === "int" ? Number(field.value) : field.value;
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
  if (form) {
    options.headers["content-type"] = "application/json";
    options.body = JSON.stringify(formBody(form));
  }
  const response = await fetch(element.dataset.api, options);
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    if (response.status === 401 && !element.dataset.api.endsWith("/login")) {
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
