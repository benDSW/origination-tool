/*
 * DSW Origination dashboard: copy-to-clipboard buttons and the template live preview.
 * Loaded from 'self' only (CSP script-src 'self'); no inline handlers, and server
 * text is only ever written with textContent / value, never parsed as HTML.
 */
(function () {
  "use strict";

  function flashButton(button, text) {
    var original = button.getAttribute("data-label") || button.textContent;
    button.setAttribute("data-label", original);
    button.textContent = text;
    button.classList.add("copied");
    window.setTimeout(function () {
      button.textContent = original;
      button.classList.remove("copied");
    }, 1500);
  }

  function fallbackCopy(element) {
    // Older browsers / non-secure contexts: select the (readonly) field and copy.
    element.focus();
    element.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    element.blur();
    return ok;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) {
      return;
    }
    var target = document.getElementById(button.getAttribute("data-copy-target"));
    if (!target) {
      return;
    }
    var text = target.value;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { flashButton(button, "Copied"); },
        function () { flashButton(button, fallbackCopy(target) ? "Copied" : "Copy failed"); }
      );
    } else {
      flashButton(button, fallbackCopy(target) ? "Copied" : "Copy failed");
    }
  });

  // ------------------------------------------------------------ live preview
  var form = document.getElementById("template-form");
  if (!form) {
    return;
  }
  var subject = form.querySelector("[name=subject]");
  var body = form.querySelector("[name=body]");
  var tokenInput = form.querySelector("[name=csrf_token]");
  var url = form.getAttribute("data-preview-url");
  var statusEl = document.getElementById("preview-status");
  var problemsEl = document.getElementById("preview-problems");
  var textEl = document.getElementById("preview-text");
  var timer = null;
  var sequence = 0;

  function showProblems(problems) {
    while (problemsEl.firstChild) {
      problemsEl.removeChild(problemsEl.firstChild);
    }
    (problems || []).forEach(function (problem) {
      var li = document.createElement("li");
      li.textContent = String(problem);
      problemsEl.appendChild(li);
    });
  }

  function refresh() {
    var mine = ++sequence;
    var params = new URLSearchParams();
    params.set("subject", subject.value);
    params.set("body", body.value);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "X-CSRF-Token": tokenInput ? tokenInput.value : ""
      },
      body: params.toString()
    })
      .then(function (response) {
        return response.json().catch(function () { return { ok: false, problems: ["Preview unavailable"] }; });
      })
      .then(function (data) {
        if (mine !== sequence) {
          return; // a newer request is in flight
        }
        if (data && data.ok) {
          statusEl.textContent = "Preview for a sample founder:";
          showProblems([]);
          textEl.textContent = data.text;
        } else {
          statusEl.textContent = "The template has problems:";
          showProblems((data && (data.problems || [data.error])) || ["Preview unavailable"]);
          textEl.textContent = "";
        }
      })
      .catch(function () {
        statusEl.textContent = "Preview unavailable.";
      });
  }

  function schedule() {
    if (timer) {
      window.clearTimeout(timer);
    }
    timer = window.setTimeout(refresh, 300);
  }

  subject.addEventListener("input", schedule);
  body.addEventListener("input", schedule);
  refresh();
})();
