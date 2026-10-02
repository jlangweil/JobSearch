/* jobhunt UI helpers: theme switch, mobile nav, command palette (Ctrl+K), "/" to search, show-if fields. */
(function () {
  "use strict";
  var root = document.documentElement;
  var THEMES = ["dark", "light", "system"];

  function applyTheme(choice) {
    var dark = choice === "dark" || (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.toggle("dark", dark);
    root.dataset.themeChoice = choice;
    document.querySelectorAll("[data-theme-icon]").forEach(function (el) {
      el.hidden = el.getAttribute("data-theme-icon") !== choice;
    });
    var btn = document.querySelector("[data-theme-cycle]");
    if (btn) btn.title = "Theme: " + choice + " (click to change)";
  }

  function typing(target) {
    var tag = (target && target.tagName) || "";
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable);
  }

  /* ---------------------------------------------------------------- palette */
  var palette, input, list, entries = [], selected = 0;

  function loadEntries() {
    try {
      var data = JSON.parse(document.getElementById("palette-data").textContent);
      entries = data.pages.map(function (e) { return { label: e[0], href: e[1], hint: e[2], kind: "page" }; })
        .concat(data.actions.map(function (e) { return { label: e[0], href: e[1], hint: e[2], kind: "action" }; }));
    } catch (e) { entries = []; }
  }

  function score(entry, q) {
    var hay = (entry.label + " " + (entry.hint || "")).toLowerCase();
    if (!q) return 1;
    if (entry.label.toLowerCase().indexOf(q) === 0) return 3;
    if (hay.indexOf(q) >= 0) return 2;
    var i = 0;
    for (var c = 0; c < hay.length && i < q.length; c++) if (hay[c] === q[i]) i++;
    return i === q.length ? 1 : 0;
  }

  function render() {
    var q = input.value.trim().toLowerCase();
    var results = entries.map(function (e) { return { e: e, s: score(e, q) }; })
      .filter(function (r) { return r.s > 0; })
      .sort(function (a, b) { return b.s - a.s; })
      .map(function (r) { return r.e; });
    var id = q.match(/^#?(\d+)$/);
    if (id) results.unshift({ label: "Open job #" + id[1], href: "/jobs/" + id[1], hint: "", kind: "job" });
    if (q && !id) results.push({ label: "Search jobs for “" + input.value.trim() + "”", href: "/search?q=" + encodeURIComponent(input.value.trim()), hint: "", kind: "search" });
    selected = Math.min(selected, Math.max(0, results.length - 1));
    list.innerHTML = "";
    results.slice(0, 40).forEach(function (r, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === selected ? "true" : "false");
      var a = document.createElement("a");
      a.href = r.href;
      a.appendChild(document.createTextNode(r.label));
      if (r.hint) { var s = document.createElement("small"); s.textContent = r.hint; a.appendChild(s); }
      var k = document.createElement("span"); k.className = "kind"; k.textContent = r.kind; a.appendChild(k);
      li.appendChild(a);
      li.addEventListener("mousemove", function () { if (selected !== i) { selected = i; mark(); } });
      list.appendChild(li);
    });
  }

  function mark() {
    Array.prototype.forEach.call(list.children, function (li, i) {
      li.setAttribute("aria-selected", i === selected ? "true" : "false");
      if (i === selected) li.scrollIntoView({ block: "nearest" });
    });
  }

  function openPalette() {
    if (!palette) return;
    input.value = ""; selected = 0; render();
    if (!palette.open) palette.showModal();
    input.focus();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var choice = "dark";
    try { choice = localStorage.getItem("jh-theme") || "dark"; } catch (e) {}
    applyTheme(choice);
    document.querySelectorAll("[data-theme-cycle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var next = THEMES[(THEMES.indexOf(root.dataset.themeChoice || "dark") + 1) % THEMES.length];
        try { localStorage.setItem("jh-theme", next); } catch (e) {}
        applyTheme(next);
      });
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (root.dataset.themeChoice === "system") applyTheme("system");
    });

    var sidebar = document.querySelector("[data-sidebar]");
    var scrim = document.querySelector(".app-scrim");
    document.querySelectorAll("[data-nav-toggle]").forEach(function (el) {
      el.addEventListener("click", function () {
        var open = !sidebar.classList.contains("open");
        sidebar.classList.toggle("open", open);
        if (scrim) scrim.hidden = !open;
      });
    });

    palette = document.getElementById("palette");
    input = document.getElementById("palette-input");
    list = document.getElementById("palette-list");
    if (palette) {
      loadEntries();
      input.addEventListener("input", function () { selected = 0; render(); });
      input.addEventListener("keydown", function (ev) {
        var n = list.children.length;
        if (ev.key === "ArrowDown") { selected = (selected + 1) % Math.max(1, n); mark(); ev.preventDefault(); }
        else if (ev.key === "ArrowUp") { selected = (selected - 1 + n) % Math.max(1, n); mark(); ev.preventDefault(); }
        else if (ev.key === "Enter") {
          var a = list.children[selected] && list.children[selected].querySelector("a");
          if (a) window.location.href = a.href;
          ev.preventDefault();
        }
      });
      palette.addEventListener("click", function (ev) { if (ev.target === palette) palette.close(); });
      document.querySelectorAll("[data-palette-open]").forEach(function (b) { b.addEventListener("click", openPalette); });
    }

    /* Fields shown only when another control has a value: data-show-if="when_kind=days" (| separates options). */
    function syncShowIf(scope) {
      (scope || document).querySelectorAll("[data-show-if]").forEach(function (el) {
        var rule = el.getAttribute("data-show-if").split("=");
        var form = el.closest("form") || document;
        var checked = form.querySelector('[name="' + rule[0] + '"]:checked');
        var field = checked || form.querySelector('[name="' + rule[0] + '"]');
        var value = field ? field.value : "";
        el.hidden = rule[1].split("|").indexOf(value) < 0;
      });
    }
    document.addEventListener("change", function (ev) { if (ev.target.name) syncShowIf(); });
    syncShowIf();
  });

  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); openPalette(); return; }
    if (ev.key === "/" && !typing(ev.target) && !(palette && palette.open)) {
      var s = document.getElementById("nav-q");
      if (s && s.offsetParent !== null) { ev.preventDefault(); s.focus(); } else { ev.preventDefault(); openPalette(); }
    }
  });
})();
