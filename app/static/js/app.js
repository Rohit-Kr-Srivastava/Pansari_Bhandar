/* Pansari Bhandar: small progressive enhancements. Every page works without this file. */
(function () {
  "use strict";

  /* ---- toasts (server sends HX-Trigger: {"toast": {...}}) ---------------------------- */
  var toastBox = document.getElementById("toasts");

  function toast(message, kind, link) {
    if (!toastBox || !message) return;
    var el = document.createElement("div");
    el.className = "toast" + (kind === "error" ? " error" : "");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    var text = document.createElement("span");
    text.textContent = message;
    el.appendChild(text);
    if (link && typeof link.href === "string" && link.href.charAt(0) === "/") {
      var a = document.createElement("a");
      a.href = link.href;
      a.textContent = link.text || "Open";
      el.appendChild(a);
    }
    toastBox.appendChild(el);
    setTimeout(function () { el.remove(); }, kind === "error" ? 6000 : 3500);
  }
  window.pbToast = toast;

  document.body.addEventListener("toast", function (e) {
    var d = e.detail || {};
    toast(d.message, d.kind, d.link);
  });

  /* ---- htmx failures: network down, server error, expired session -------------------- */
  document.body.addEventListener("htmx:sendError", function () {
    toast("No internet connection. Please check your network and try again.", "error");
  });
  document.body.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (xhr && xhr.getResponseHeader("HX-Trigger")) return; // server already explained
    toast("Something went wrong. Please try again.", "error");
  });
  document.body.addEventListener("htmx:timeout", function () {
    toast("The request took too long. Please try again.", "error");
  });

  /* ---- prevent double submits (checkout, product form) -------------------------------- */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var who = e.submitter;
    if (who && (who.hasAttribute("formaction") || who.hasAttribute("formnovalidate"))) return;
    if (form.hasAttribute("data-once")) {
      if (form.dataset.sent === "1") { e.preventDefault(); return; }
      form.dataset.sent = "1";
      var btns = form.querySelectorAll("button[type=submit], button:not([type])");
      btns.forEach(function (b) {
        b.classList.add("is-busy");
        if (b.dataset.busy) { b.dataset.label = b.textContent; b.textContent = b.dataset.busy; }
      });
      // If the browser restores this page from history, allow submitting again.
      window.addEventListener("pageshow", function () {
        form.dataset.sent = "";
        btns.forEach(function (b) {
          b.classList.remove("is-busy");
          if (b.dataset.label) b.textContent = b.dataset.label;
        });
      }, { once: true });
    }
  }, true);

  /* ---- quantity steppers ---------------------------------------------------------------- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-step]");
    if (!btn) return;
    var box = btn.closest(".qty");
    var input = box && box.querySelector("input");
    if (!input) return;
    var min = parseInt(input.min || "1", 10);
    var max = parseInt(input.max || "99", 10);
    var v = (parseInt(input.value, 10) || min) + parseInt(btn.dataset.step, 10);
    input.value = Math.max(min, Math.min(max, v));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  /* ---- product gallery ------------------------------------------------------------------ */
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-gallery-src]");
    if (!t) return;
    var main = document.getElementById("gallery-main-img");
    if (!main) return;
    main.src = t.dataset.gallerySrc;
    document.querySelectorAll("[data-gallery-src]").forEach(function (b) {
      b.setAttribute("aria-pressed", b === t ? "true" : "false");
    });
  });

  /* ---- product form: quick sizes and unit hint ----------------------------------------- */
  document.addEventListener("click", function (e) {
    var q = e.target.closest("[data-quick-size]");
    if (!q) return;
    e.preventDefault();
    var row = q.closest(".variant");
    var parts = q.dataset.quickSize.split(" ");
    row.querySelector("[name=v_amount]").value = parts[0];
    row.querySelector("[name=v_unit]").value = parts[1];
    var label = row.querySelector("[name=v_label]");
    if (label && !label.dataset.touched) label.value = q.textContent.trim();
  });
  document.addEventListener("input", function (e) {
    if (e.target.name === "v_label") e.target.dataset.touched = "1";
  });

  /* ---- product form: add another size row from a <template> ------------------------- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-add-variant]");
    if (!btn) return;
    var box = document.getElementById(btn.dataset.addVariant);
    var tpl = document.getElementById("variant-tpl");
    if (!box || !tpl) return;
    var idx = box.querySelectorAll(".variant").length;
    box.insertAdjacentHTML("beforeend", tpl.innerHTML.split("__I__").join(String(idx)));
    var first = box.lastElementChild && box.lastElementChild.querySelector("input[name=v_amount]");
    if (first) first.focus();
  });

  /* ---- copy link (order number etc.) ---------------------------------------------------- */
  document.addEventListener("click", function (e) {
    var c = e.target.closest("[data-copy]");
    if (!c || !navigator.clipboard) return;
    navigator.clipboard.writeText(c.dataset.copy).then(function () { toast("Copied"); });
  });
})();
