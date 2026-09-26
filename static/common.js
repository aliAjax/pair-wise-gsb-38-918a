// 页面共享逻辑：身份（localStorage 同步）、请求封装、状态标签、转义。
const STATUS_LABELS = { open: "待处理", pending_confirmation: "待确认", closed: "已关闭" };
const EVENT_LABELS = {
  triaged: "登记责任人",
  replied: "回复处理说明",
  confirmed: "确认关闭",
  returned: "退回返修",
  reopened: "重新打开",
};

function saveIdentity() {
  localStorage.setItem("sqc_user", document.getElementById("user").value);
  localStorage.setItem("sqc_role", document.getElementById("role").value);
}

function initIdentity() {
  const user = document.getElementById("user");
  const role = document.getElementById("role");
  if (localStorage.getItem("sqc_user")) user.value = localStorage.getItem("sqc_user");
  if (localStorage.getItem("sqc_role")) role.value = localStorage.getItem("sqc_role");
  user.addEventListener("change", saveIdentity);
  role.addEventListener("change", saveIdentity);
}

function headers() {
  return {
    "Content-Type": "application/json",
    "X-User": document.getElementById("user").value,
    "X-Role": document.getElementById("role").value,
  };
}

async function call(url, opts = {}) {
  const r = await fetch(url, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function badge(status) {
  return `<span class="badge badge-${status}">${STATUS_LABELS[status] || status}</span>`;
}

function versionId() {
  return Number(document.getElementById("version").value);
}

function show(id, value) {
  document.getElementById(id).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
