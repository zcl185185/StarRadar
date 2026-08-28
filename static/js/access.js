// StarRadar 本地访问码门禁：服务端会话验证，刷新/服务重启后按会话状态重新确认。
(function () {
  "use strict";
  var resolveReady;
  window.StarRadarAccessReady = new Promise(function (resolve) { resolveReady = resolve; });
  var gate = document.querySelector("#accessGate");
  var form = document.querySelector("#accessForm");
  var input = document.querySelector("#accessCode");
  var error = document.querySelector("#accessError");

  function unlock() {
    document.body.classList.remove("access-locked");
    gate.classList.add("unlocked");
    gate.setAttribute("aria-hidden", "true");
    // 先做淡出，再从布局中移除，避免搜索结果被透明遮罩继续拦截点击。
    setTimeout(function () { gate.hidden = true; }, 300);
    // 下一步必须完成 GitHub 登录，个人雷达才会开始加载。
    try { window.dispatchEvent(new Event("sr:access-granted")); } catch (e) {}
    resolveReady();
  }
  function showError(message) { error.textContent = message || "暂时无法验证，请确认本地服务正在运行。"; input.focus(); }
  function check() {
    fetch("/api/access/status", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (body) { if (body && body.authorized) unlock(); else input.focus(); })
      .catch(function () { showError("连不上本地服务 — 请先运行 python src/main.py --serve，再刷新页面"); });
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var code = input.value;
    if (!code) { showError("请输入访问码"); return; }
    var button = form.querySelector("button");
    button.disabled = true; button.textContent = "验证中…"; error.textContent = "";
    fetch("/api/access", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code }) })
      .then(function (response) {
        if (response.status === 401) { input.value = ""; showError("访问码不正确"); button.disabled = false; button.innerHTML = "进入 <span>→</span>"; return null; }
        return response.json().then(function (body) { return { response: response, body: body }; });
      })
      .then(function (result) {
        if (!result) return;  // 401 已处理
        if (!result.response.ok || !result.body.authorized) throw new Error(result.body.error || "访问码不正确");
        unlock();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        input.value = "";
        showError(err.message || "暂时无法验证，请确认本地服务正在运行");
        button.disabled = false; button.innerHTML = "进入 <span>→</span>";
      });
  });
  check();
})();
