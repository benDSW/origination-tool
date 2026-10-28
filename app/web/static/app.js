/*
 * DSW Origination dashboard: copy-to-clipboard buttons, page search and the template live preview.
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

  // ------------------------------------------------------------ page search
  // Filters the items already on the page as you type: no Enter, no reload. Every
  // whitespace-separated keyword must appear somewhere in an item's data-search-text
  // (founder + company name), ignoring case and accents, so "k" shows everything with
  // a k in it and each extra letter narrows it further.
  var search = document.querySelector("[data-search]");
  if (search) {
    var searchItems = Array.prototype.slice.call(document.querySelectorAll("[data-search-text]"));
    var searchGroups = Array.prototype.slice.call(document.querySelectorAll("[data-search-group]"));
    var searchShown = document.querySelector("[data-search-shown]");
    var searchKeys = document.querySelector("[data-search-keys]");
    var searchClear = document.querySelector("[data-search-clear]");
    var searchEmpty = document.querySelector("[data-search-empty]");
    var searchTerm = document.querySelector("[data-search-term]");

    var normalise = function (text) {
      return String(text || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    };
    searchItems.forEach(function (item) {
      item.searchText = normalise(item.getAttribute("data-search-text"));
    });

    var applySearch = function () {
      var keywords = normalise(search.value).split(/\s+/).filter(Boolean);
      var shown = 0;
      searchItems.forEach(function (item) {
        var match = keywords.every(function (k) { return item.searchText.indexOf(k) !== -1; });
        item.hidden = !match;
        if (match) {
          shown += 1;
        }
      });
      // Pipeline: each status section shows its own match count, and drops out
      // entirely while searching if nothing in it matches.
      searchGroups.forEach(function (group) {
        var hits = group.querySelectorAll("[data-search-text]:not([hidden])").length;
        var count = group.querySelector("[data-search-count]");
        if (count) {
          count.textContent = hits;
        }
        group.hidden = keywords.length > 0 && hits === 0;
      });
      if (searchShown) {
        searchShown.textContent = shown;
      }
      if (searchEmpty) {
        searchEmpty.hidden = !(keywords.length && shown === 0);
      }
      if (searchTerm) {
        searchTerm.textContent = "\u201c" + search.value.trim() + "\u201d";
      }
      if (searchClear) {
        searchClear.classList.toggle("on", !!search.value);
      }
      if (searchKeys) {
        searchKeys.classList.toggle("on", document.activeElement === search && !search.value);
      }
    };

    var clearSearch = function () {
      search.value = "";
      applySearch();
    };

    search.addEventListener("input", applySearch);
    search.addEventListener("focus", applySearch);
    search.addEventListener("blur", function () {
      if (searchKeys) {
        searchKeys.classList.remove("on");
      }
    });
    search.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault(); // the list is already filtered
      } else if (event.key === "Escape") {
        if (search.value) {
          clearSearch();
        } else {
          search.blur();
        }
      }
    });
    if (searchClear) {
      searchClear.addEventListener("click", function () {
        clearSearch();
        search.focus();
      });
    }
    // "/" jumps to the search box unless the user is already typing in a field.
    document.addEventListener("keydown", function (event) {
      var el = event.target;
      var typing = el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
      if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    });
    applySearch(); // browsers may restore the box's value on back/forward
  }

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
