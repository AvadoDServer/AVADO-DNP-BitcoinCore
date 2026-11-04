// Draws the page model from /api/status. Shared by the AVADO Bitcoin packages
// (Bitcoin node, Electrum server, Bitcoin explorer); keep the copies identical.
// Everything is inserted as text (never as HTML), so nothing the server or a
// peer sends can run as code on this page. URLs are relative, so the page also
// works under a path (the explorer serves it at /avado/).
"use strict";

(function () {
  var main = document.getElementById("main");
  var timer = null;
  var busy = false;
  var scrolled = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function paragraphs(parent, text, cls) {
    if (!text) return;
    var list = Array.isArray(text) ? text : [text];
    list.forEach(function (t) {
      if (t) parent.appendChild(el("p", cls, t));
    });
  }

  function toast(message) {
    var old = document.querySelector(".toast");
    if (old) old.remove();
    var node = el("div", "toast", message);
    node.setAttribute("role", "status");
    document.body.appendChild(node);
    setTimeout(function () {
      node.remove();
    }, 6000);
  }

  function copyText(text, button) {
    function done() {
      var label = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () {
        button.textContent = label;
      }, 1500);
    }
    // navigator.clipboard needs https; my.ava.do pages are plain http, so fall
    // back to a hidden text field.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {});
      return;
    }
    var area = el("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      done();
    } catch (e) {
      /* the text is still selectable on the page */
    }
    area.remove();
  }

  function runAction(button, spec) {
    if (busy) return;
    if (spec.confirm && !window.confirm(spec.confirm)) return;
    busy = true;
    button.disabled = true;
    fetch("api/action/" + encodeURIComponent(spec.action), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Avado": "1" },
      body: JSON.stringify(spec.body || {}),
      cache: "no-store",
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (r) {
        toast(r.ok ? r.data.message || "Done." : r.data.error || "That did not work. Please try again.");
      })
      .catch(function () {
        toast("The app did not answer. Please try again in a minute.");
      })
      .then(function () {
        busy = false;
        button.disabled = false;
        load();
      });
  }

  function renderHero(model) {
    var card = el("section", "card hero state-" + (model.state || "working"));
    card.appendChild(el("span", "dot"));
    var body = el("div", "hero-body");
    body.appendChild(el("h1", null, model.headline || ""));
    paragraphs(body, model.text, "muted");
    if (model.progress && typeof model.progress.percent === "number") {
      var bar = el("div", "bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      var pct = Math.max(0, Math.min(100, model.progress.percent));
      bar.setAttribute("aria-valuenow", pct.toFixed(1));
      var fill = el("span");
      fill.style.width = pct.toFixed(2) + "%";
      bar.appendChild(fill);
      body.appendChild(bar);
      var label = el("div", "bar-label");
      label.appendChild(el("span", null, model.progress.left || pct.toFixed(1) + "%"));
      if (model.progress.right) label.appendChild(el("span", null, model.progress.right));
      body.appendChild(label);
    }
    card.appendChild(body);
    return card;
  }

  function renderFacts(facts) {
    var grid = el("section", "facts");
    facts.forEach(function (f) {
      var box = el("div", "fact" + (f.tone ? " tone-" + f.tone : ""));
      box.appendChild(el("div", "k", f.k));
      box.appendChild(el("div", "v", f.v));
      grid.appendChild(box);
    });
    return grid;
  }

  function renderSection(s) {
    var card = el("section", "card" + (s.tone ? " tone-" + s.tone : ""));
    if (s.id) card.id = s.id;
    if (s.title) card.appendChild(el("h2", null, s.title));
    paragraphs(card, s.text);
    // Values first: the steps below refer to them ("the address and port above").
    if (s.fields && s.fields.length) {
      var fields = el("div", "fields");
      s.fields.forEach(function (f) {
        var row = el("div", "field");
        row.appendChild(el("span", "k", f.k));
        row.appendChild(el("span", "v", f.v));
        if (f.copy) {
          var b = el("button", "small", "Copy");
          b.type = "button";
          b.addEventListener("click", function () {
            copyText(String(f.v), b);
          });
          row.appendChild(b);
        }
        fields.appendChild(row);
      });
      card.appendChild(fields);
    }
    paragraphs(card, s.howto);
    if (s.steps && s.steps.length) {
      var ol = el("ol", "steps");
      s.steps.forEach(function (t) {
        ol.appendChild(el("li", null, t));
      });
      card.appendChild(ol);
    }
    paragraphs(card, s.after, "muted");
    var hasButtons = (s.buttons && s.buttons.length) || (s.links && s.links.length);
    if (hasButtons) {
      var btns = el("div", "btns");
      (s.links || []).forEach(function (l) {
        var a = el("a", "btn" + (l.style ? " " + l.style : ""), l.label);
        a.href = l.href;
        if (/^https?:/.test(l.href) && l.external) {
          a.target = "_blank";
          a.rel = "noopener noreferrer";
        }
        btns.appendChild(a);
      });
      (s.buttons || []).forEach(function (spec) {
        var b = el("button", spec.style || null, spec.label);
        b.type = "button";
        b.addEventListener("click", function () {
          runAction(b, spec);
        });
        btns.appendChild(b);
      });
      card.appendChild(btns);
    }
    // "Details": extra steps and notes ("more"), then a table ("advanced"), folded away.
    var more = s.more || [];
    var adv = s.advanced || [];
    if (more.length || adv.length) {
      var det = el("details", "adv");
      det.appendChild(el("summary", null, s.advancedTitle || "Details"));
      more.forEach(function (m) {
        var box = el("div", "more");
        if (m.title) box.appendChild(el("h3", null, m.title));
        paragraphs(box, m.text);
        if (m.steps && m.steps.length) {
          var list = el("ol", "steps");
          m.steps.forEach(function (t) {
            list.appendChild(el("li", null, t));
          });
          box.appendChild(list);
        }
        det.appendChild(box);
      });
      if (adv.length) {
        var table = el("table", "kv");
        adv.forEach(function (row) {
          var tr = el("tr");
          tr.appendChild(el("td", null, row.k));
          tr.appendChild(el("td", null, row.v));
          table.appendChild(tr);
        });
        det.appendChild(table);
      }
      card.appendChild(det);
    }
    return card;
  }

  function render(model) {
    document.title = model.title || "AVADO";
    document.getElementById("app-title").textContent = model.title || "AVADO";
    document.getElementById("app-version").textContent = model.version ? "version " + model.version : "";
    // Keep open "Details" boxes open across refreshes.
    var open = {};
    main.querySelectorAll("section[id] details[open]").forEach(function (d) {
      open[d.closest("section").id] = true;
    });
    var frag = document.createDocumentFragment();
    frag.appendChild(renderHero(model));
    if (model.facts && model.facts.length) frag.appendChild(renderFacts(model.facts));
    (model.sections || []).forEach(function (s) {
      var card = renderSection(s);
      if (s.id && open[s.id]) {
        var d = card.querySelector("details");
        if (d) d.open = true;
      }
      frag.appendChild(card);
    });
    main.replaceChildren(frag);
    // A link from another AVADO page can point at one card (#connect): show it once.
    if (!scrolled) {
      scrolled = true;
      var target = /^#[A-Za-z][\w-]*$/.test(location.hash) && document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView({ block: "start" });
    }
    schedule(model.refresh || 10);
  }

  function schedule(seconds) {
    clearTimeout(timer);
    timer = setTimeout(load, Math.max(3, seconds) * 1000);
  }

  function load() {
    fetch("api/status", { cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(render)
      .catch(function () {
        var card = el("section", "card hero state-warn");
        card.appendChild(el("span", "dot"));
        var body = el("div", "hero-body");
        body.appendChild(el("h1", null, "The app is starting"));
        body.appendChild(el("p", "muted", "This page updates by itself. If it stays like this for more than 10 minutes, restart the app in the AVADO Admin."));
        card.appendChild(body);
        main.replaceChildren(card);
        schedule(5);
      });
  }

  load();
})();
