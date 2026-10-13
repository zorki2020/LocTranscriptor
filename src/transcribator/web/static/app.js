// Behavior on top of htmx: drag-and-drop upload, the app window bridge (native file
// dialog, Finder drops by path), queue and status polling, inline edits, the delete
// dialog, the "/" key and the first-run tips. Handlers are delegated to the document and keep
// working after htmx swaps markup.
//
// Upload: the file goes as the request body to this same service (127.0.0.1), without
// multipart. The file name is sent only for the default title. XMLHttpRequest instead of
// fetch: only it reports upload progress.
//
// Messages come from data-msg-* attributes on #drop in the interface language (CSP
// forbids inline scripts, so there is no other way to hand them over).
(function () {
  "use strict";

  const MAX_BYTES = 8 * 1024 * 1024 * 1024;
  const byId = (id) => document.getElementById(id);

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // Message from #drop's data-msg-* attribute with {name} placeholders filled in.
  function msg(name, values) {
    let text = byId("drop")?.dataset[name] || "";
    Object.entries(values || {}).forEach(function ([key, value]) {
      text = text.split("{" + key + "}").join(String(value));
    });
    return text;
  }

  // Every message the script shows is an outcome: a recording queued or an error.
  function line(text, isError) {
    const p = document.createElement("p");
    p.className = isError ? "msg msg-error" : "msg msg-ok";
    p.textContent = text;
    return p;
  }

  // Blocks with data-poll (queue, meeting status) refresh on the queue-changed event.
  function refreshQueue() {
    if (window.htmx && document.querySelector("[data-poll]")) window.htmx.trigger(document.body, "queue-changed");
  }

  // --- upload -------------------------------------------------------------------------

  function setProgress(pct) {
    byId("upload-progress").value = pct;
    byId("upload-pct").textContent = pct + "%";
  }

  function uploadOne(file, label) {
    return new Promise(function (resolve) {
      if (file.size > MAX_BYTES) {
        resolve(line(msg("msgTooLarge"), true));
        return;
      }
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/upload");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-CSRF-Token", csrfToken());
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name.slice(0, 300)));
      const language = byId("language");
      if (language) xhr.setRequestHeader("X-Language", language.value);
      if (file.lastModified) {
        xhr.setRequestHeader("X-File-Mtime", String(Math.floor(file.lastModified)));
      }
      byId("upload-label").textContent = label;
      setProgress(0);
      xhr.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable) setProgress(Math.round((event.loaded / event.total) * 100));
      });
      xhr.addEventListener("load", function () {
        let data = {};
        try {
          data = JSON.parse(xhr.responseText);
        } catch (e) {
          data = {};
        }
        if (xhr.status === 200) {
          refreshQueue();
          resolve(line(msg("msgQueued", { title: data.title || msg("msgUntitled") }), false));
        } else {
          resolve(line(data.error || msg("msgFailed"), true));
        }
      });
      xhr.addEventListener("error", function () {
        resolve(line(msg("msgAborted"), true));
      });
      xhr.send(file);
    });
  }

  // New files wait for the previous ones to finish: one transfer at a time.
  let chain = Promise.resolve();

  function uploadAll(files) {
    const zone = byId("drop");
    if (!zone) return;
    chain = chain.then(async function () {
      const results = [];
      zone.classList.add("is-uploading");
      byId("upload-box").hidden = false;
      byId("drop-status").replaceChildren();
      for (let i = 0; i < files.length; i += 1) {
        const label = files.length > 1 ? msg("msgUploadingN", { i: i + 1, n: files.length }) : msg("msgUploading");
        results.push(await uploadOne(files[i], label));
      }
      byId("upload-box").hidden = true;
      zone.classList.remove("is-uploading");
      byId("drop-status").replaceChildren(...results);
    });
  }

  // A file can be dropped anywhere on the page. Outside the zone the browser would open it
  // as a page, so the default action is cancelled everywhere. The counter is needed
  // because dragenter and dragleave fire for every nested element.
  let dragDepth = 0;
  const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  const setDragover = (on) => byId("drop")?.classList.toggle("is-dragover", on);

  document.addEventListener("dragenter", function (event) {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    setDragover(true);
  });
  document.addEventListener("dragleave", function (event) {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    setDragover(dragDepth > 0);
  });
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", function (event) {
    event.preventDefault();
    dragDepth = 0;
    setDragover(false);
    if (event.dataTransfer?.files.length) uploadAll(Array.from(event.dataTransfer.files));
  });

  // --- app window (Transcribator.app) -------------------------------------------------
  //
  // In the app the page talks to the native side through the "transcribator" message
  // handler: "pick" opens the native file dialog and replies with paths, "revealData"
  // shows the data folder in Finder. Files dropped from Finder are intercepted natively
  // and come in through window.transcribatorDrop(paths). Recordings with a path are added
  // with POST /enqueue: the service reads them where they are, nothing is copied.

  const ENQUEUE_BATCH = 40;

  function bridge(message) {
    const handler = window.webkit?.messageHandlers?.transcribator;
    if (!handler) return Promise.reject(new Error("no bridge"));
    return handler.postMessage(message);
  }

  async function enqueueBatch(paths, language) {
    const body = new URLSearchParams();
    paths.forEach((path) => body.append("path", path));
    if (language) body.append("language", language);
    let response;
    try {
      response = await fetch("/enqueue", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrfToken() },
        body: body,
        cache: "no-store",
        credentials: "same-origin",
        redirect: "error",
      });
    } catch (e) {
      return [line(msg("msgAborted"), true)];
    }
    let data = null;
    try {
      data = await response.json();
    } catch (e) {
      data = null;
    }
    if (!response.ok || !data || !Array.isArray(data.results)) {
      return [line((data && data.error) || msg("msgAddFailed") || msg("msgFailed"), true)];
    }
    if (data.results.some((r) => r && r.ok)) refreshQueue();
    return data.results.map(function (r) {
      if (r && r.ok) return line(msg("msgQueued", { title: r.title || msg("msgUntitled") }), false);
      return line((r && r.error) || msg("msgAddFailed"), true);
    });
  }

  // Paths are added one batch at a time, on the same chain as uploads.
  function enqueuePaths(paths) {
    const zone = byId("drop");
    if (!zone || !paths.length) return;
    const language = byId("language")?.value || "";
    chain = chain.then(async function () {
      const results = [];
      zone.classList.add("is-adding");
      byId("drop-status").replaceChildren();
      for (let i = 0; i < paths.length; i += ENQUEUE_BATCH) {
        results.push(...(await enqueueBatch(paths.slice(i, i + ENQUEUE_BATCH), language)));
      }
      zone.classList.remove("is-adding");
      byId("drop-status").replaceChildren(...results);
    });
  }

  const cleanPaths = (paths) =>
    Array.isArray(paths) ? paths.filter((p) => typeof p === "string" && p.startsWith("/")) : [];

  // Called by the app for files dropped from Finder. Returns whether the page took them:
  // only the upload page adds recordings, as with drops in a browser.
  window.transcribatorDrop = function (paths) {
    dragDepth = 0;
    setDragover(false);
    const clean = cleanPaths(paths);
    if (!byId("drop") || !clean.length) return false;
    enqueuePaths(clean);
    return true;
  };

  async function nativePick(button) {
    button.disabled = true;
    try {
      const reply = await bridge({ cmd: "pick" });
      enqueuePaths(cleanPaths(reply && reply.paths));
    } catch (e) {
      byId("drop-status")?.replaceChildren(line(msg("msgNoBridge"), true));
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", function (event) {
    const target = event.target instanceof Element ? event.target : null;
    const pick = target?.closest("[data-native-pick]");
    const command = target?.closest("[data-bridge]");
    if (pick) {
      event.preventDefault();
      nativePick(pick);
    } else if (command && command.dataset.bridge === "revealData") {
      event.preventDefault();
      bridge({ cmd: "revealData" }).catch(function () {});
    }
  });

  // --- queue and status ---------------------------------------------------------------

  // While the tab is hidden, polling doesn't hit the server; on return blocks refresh at once.
  document.addEventListener("htmx:beforeRequest", function (event) {
    if (document.hidden && event.detail.elt?.hasAttribute?.("data-poll")) event.preventDefault();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshQueue();
  });

  // Rows that weren't there before the refresh fade in once (class is-new).
  let knownRows = null;
  const rowIds = (root) => new Set(Array.from(root.querySelectorAll("[data-id]"), (r) => r.dataset.id));
  document.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.target?.id === "queue") knownRows = rowIds(event.detail.target);
  });
  document.addEventListener("htmx:afterSwap", function () {
    const queue = byId("queue");
    if (knownRows && queue) {
      queue.querySelectorAll("[data-id]").forEach((row) => row.classList.toggle("is-new", !knownRows.has(row.dataset.id)));
    }
    knownRows = null;
  });

  // --- inline edits -------------------------------------------------------------------

  const fields = (form) => Array.from(form.querySelectorAll(".inline"));
  const dirty = (form) => fields(form).some((field) => field.value !== field.defaultValue);
  const isInline = (el) => el.classList?.contains("inline") && el.form;

  function revert(form, onlyInvalid) {
    fields(form).forEach(function (field) {
      const invalid = !field.checkValidity() || (field.required && !field.value.trim());
      if (!onlyInvalid || invalid) field.value = field.defaultValue;
    });
  }

  function save(form) {
    if (form.dataset.saving) return;
    form.dataset.saving = "1";
    form.requestSubmit();
  }

  // The title wraps: the textarea grows with its content (field-sizing, and where that's
  // missing the script fits the height). A line break in the title becomes a space.
  function fit(area) {
    if (CSS.supports("field-sizing", "content")) return;
    area.style.height = "auto";
    area.style.height = area.scrollHeight + "px";
  }

  document.addEventListener("input", function (event) {
    const area = event.target;
    if (!(area instanceof HTMLTextAreaElement) || !area.classList.contains("inline")) return;
    if (area.value.includes("\n")) area.value = area.value.replace(/\s*\n\s*/g, " ");
    fit(area);
  });

  // A speaker chip and its rename form are shown one at a time.
  function toggleSpeaker(el, editing, focusChip) {
    const item = el.closest(".speaker");
    if (!item) return;
    const chip = item.querySelector(".speaker-chip");
    const input = item.querySelector(".inline-speaker");
    chip.hidden = editing;
    chip.setAttribute("aria-expanded", String(editing));
    input.form.hidden = !editing;
    if (editing) {
      input.focus();
      input.select();
    } else if (focusChip) {
      chip.focus();
    }
  }

  // The recording date reads as text in the interface language; pressing it shows the
  // native field (its format follows the system locale) until the edit ends.
  function toggleDate(el, editing, focusText) {
    const form = el.closest("form");
    const text = form?.querySelector(".date-text");
    const input = form?.querySelector(".inline-date");
    if (!text || !input) return;
    text.hidden = editing;
    text.setAttribute("aria-expanded", String(editing));
    input.hidden = !editing;
    if (editing) input.focus();
    else if (focusText) text.focus();
  }

  // Finishing an edit: Enter and leaving the form save changes, Esc cancels them.
  // An empty or invalid value is reverted on leaving. Without changes the speaker form
  // collapses back into a chip.
  function finish(el, how) {
    const form = el.form;
    if (how === "cancel") revert(form, false);
    if (how === "blur") revert(form, true);
    if (how !== "cancel" && dirty(form) && form.checkValidity()) save(form);
    else if (el.classList.contains("inline-speaker")) toggleSpeaker(el, false, how !== "blur");
    else if (el.classList.contains("inline-date")) toggleDate(el, false, how !== "blur");
    else if (how === "cancel") el.blur();
  }

  document.addEventListener("keydown", function (event) {
    const el = event.target;
    if (isInline(el)) {
      if (event.key === "Escape" || (event.key === "Enter" && !event.isComposing)) {
        event.preventDefault();
        finish(el, event.key === "Escape" ? "cancel" : "enter");
      }
      return;
    }
    // "/" focuses search; on other pages it goes to the archive.
    const typing = el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey || typing) return;
    if (document.querySelector("dialog[open]")) return;
    event.preventDefault();
    const search = byId("q");
    if (search) {
      search.focus();
      search.select();
    } else {
      window.location.assign("/meetings#search");
    }
  });

  document.addEventListener("focusout", function (event) {
    const el = event.target;
    if (!isInline(el)) return;
    // The date field closes whenever it loses focus, even to the title in the same form.
    const stays = event.relatedTarget && el.form.contains(event.relatedTarget);
    if (!stays || el.classList.contains("inline-date")) finish(el, "blur");
  });

  // After an error (an empty name, say) the form can be submitted again.
  document.addEventListener("htmx:afterRequest", function (event) {
    const form = event.detail.elt;
    if (form instanceof HTMLFormElement && event.detail.failed) delete form.dataset.saving;
  });

  // --- clicks: speakers and the delete dialog -----------------------------------------

  const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // The dialog plays its exit animation (class is-closing) before it really closes.
  function closeDialog(dialog) {
    if (!dialog.open || dialog.classList.contains("is-closing")) return;
    if (reducedMotion()) {
      dialog.close();
      return;
    }
    let finished = false;
    const finish = function () {
      if (finished) return;
      finished = true;
      dialog.classList.remove("is-closing");
      dialog.close();
    };
    dialog.classList.add("is-closing");
    dialog.addEventListener("animationend", function done(event) {
      if (event.target !== dialog) return;
      dialog.removeEventListener("animationend", done);
      finish();
    });
    window.setTimeout(finish, 250);
  }

  document.addEventListener("click", function (event) {
    const target = event.target instanceof Element ? event.target : null;
    const chip = target?.closest(".speaker-chip");
    const date = target?.closest(".date-text");
    const opener = target?.closest("[data-dialog]");
    if (chip) {
      toggleSpeaker(chip, true, false);
    } else if (date) {
      toggleDate(date, true, false);
    } else if (opener) {
      byId(opener.dataset.dialog)?.showModal();
    } else if (target?.closest("dialog [data-close]")) {
      closeDialog(target.closest("dialog"));
    } else if (target instanceof HTMLDialogElement && target.open) {
      // A click on the backdrop, outside the dialog box, closes the dialog.
      const box = target.getBoundingClientRect();
      const inside =
        event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) closeDialog(target);
    }
  });

  // Esc closes the dialog with the same animation. The cancel event doesn't bubble.
  document.addEventListener(
    "cancel",
    function (event) {
      if (!(event.target instanceof HTMLDialogElement)) return;
      event.preventDefault();
      closeDialog(event.target);
    },
    true,
  );

  // The close event doesn't bubble, so it's caught in the capture phase.
  document.addEventListener("close", (event) => event.target.querySelector?.("#delete-msg")?.replaceChildren(), true);

  // --- select: a listbox over the native element --------------------------------------
  //
  // Each <select class="select"> gets a select-only combobox (WAI-ARIA APG pattern): a
  // button with role="combobox" and a role="listbox" popup in the top layer, keyboard
  // and typeahead included. The native select stays in the form, hidden: it keeps the
  // value for form posts, hx-include and the upload code, and gets a bubbling change
  // event, so htmx triggers keep working. Without script or the Popover API the native
  // select is used as is.

  const canPopover = typeof HTMLElement.prototype.showPopover === "function";
  const canAnchor = Boolean(window.CSS?.supports?.("top: anchor(bottom)"));
  const openCombos = new Set();
  let comboCount = 0;

  function icon(name) {
    const span = document.createElement("span");
    span.className = "i i-" + name;
    span.setAttribute("aria-hidden", "true");
    return span;
  }

  function enhanceSelect(select) {
    if (!canPopover || select.dataset.enhanced || select.multiple || select.size > 1) return;
    select.dataset.enhanced = "1";
    comboCount += 1;
    const base = "combo-" + comboCount;
    const label = select.id ? document.querySelector('label[for="' + CSS.escape(select.id) + '"]') : null;
    if (label && !label.id) label.id = base + "-label";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "combo";
    button.id = base;
    button.disabled = select.disabled;
    button.setAttribute("role", "combobox");
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", base + "-listbox");
    if (label) button.setAttribute("aria-labelledby", label.id);
    else if (select.hasAttribute("aria-label")) button.setAttribute("aria-label", select.getAttribute("aria-label"));
    if (select.hasAttribute("aria-describedby")) {
      button.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
    }
    const shown = document.createElement("span");
    shown.className = "combo-value";
    button.append(shown, icon("chevrons"));

    const list = document.createElement("div");
    list.className = "listbox";
    list.id = base + "-listbox";
    list.setAttribute("role", "listbox");
    list.setAttribute("popover", "manual");
    if (label) list.setAttribute("aria-labelledby", label.id);
    const options = Array.from(select.options, function (opt, i) {
      const el = document.createElement("div");
      el.className = "option";
      el.id = base + "-opt-" + i;
      el.setAttribute("role", "option");
      el.setAttribute("aria-selected", "false");
      if (opt.lang) el.lang = opt.lang;
      if (opt.disabled) el.setAttribute("aria-disabled", "true");
      const text = document.createElement("span");
      text.textContent = opt.textContent;
      el.append(text, icon("check"));
      list.append(el);
      return el;
    });
    if (canAnchor) {
      button.style.setProperty("anchor-name", "--" + base);
      list.style.setProperty("position-anchor", "--" + base);
    }
    select.hidden = true;
    select.after(button, list);

    let open = false;
    let active = -1;
    let typed = "";
    let typedAt = 0;
    let viaKeys = false; // the active option was reached with the keyboard, not the pointer
    const enabled = (i) => i >= 0 && i < options.length && !select.options[i].disabled;
    const labels = () => options.map((el) => el.textContent.trim().toLocaleLowerCase());

    function sync() {
      const i = select.selectedIndex;
      shown.textContent = i >= 0 ? select.options[i].textContent : "";
      options.forEach((el, n) => el.setAttribute("aria-selected", String(n === i)));
    }

    function setActive(i) {
      if (!enabled(i)) return;
      active = i;
      options.forEach((el, n) => el.classList.toggle("is-active", n === i));
      button.setAttribute("aria-activedescendant", options[i].id);
      options[i].scrollIntoView({ block: "nearest" });
    }

    // The option itself if it can be chosen, otherwise the nearest one in a direction.
    const near = (i, delta) => (enabled(i) ? i : step(i, delta));

    // Moves from the current option in a direction, skipping disabled ones.
    function step(from, delta) {
      let i = from;
      for (let n = 0; n < options.length; n += 1) {
        const next = Math.min(options.length - 1, Math.max(0, i + delta));
        if (next === i) break;
        i = next;
        if (enabled(i)) return i;
      }
      return enabled(from) ? from : -1;
    }

    // Without anchor positioning the popup is placed by hand, below or above the button.
    function place() {
      if (canAnchor) return;
      const box = button.getBoundingClientRect();
      list.style.minWidth = box.width + "px";
      list.style.left = Math.max(8, Math.min(box.left, window.innerWidth - list.offsetWidth - 8)) + "px";
      const below = window.innerHeight - box.bottom;
      const above = box.top;
      const height = list.offsetHeight;
      const top = height + 14 > below && above > below ? box.top - height - 6 : box.bottom + 6;
      list.style.top = top + "px";
    }

    function openList(where) {
      if (open || button.disabled) return;
      open = true;
      viaKeys = false;
      openCombos.add(closeList);
      list.classList.remove("is-closing");
      if (!list.matches(":popover-open")) list.showPopover();
      place();
      button.setAttribute("aria-expanded", "true");
      const current = select.selectedIndex;
      if (where === "first") setActive(step(-1, 1));
      else if (where === "last") setActive(step(options.length, -1));
      else setActive(enabled(current) ? current : step(-1, 1));
    }

    function closeList() {
      if (!open) return;
      open = false;
      openCombos.delete(closeList);
      button.setAttribute("aria-expanded", "false");
      button.removeAttribute("aria-activedescendant");
      const hide = function () {
        if (open) return;
        list.classList.remove("is-closing");
        if (list.matches(":popover-open")) list.hidePopover();
      };
      if (reducedMotion()) {
        hide();
        return;
      }
      list.classList.add("is-closing");
      window.setTimeout(hide, 120);
    }

    function choose(i) {
      if (!enabled(i)) return;
      if (select.selectedIndex !== i) {
        select.selectedIndex = i;
        sync();
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    // Typing a name jumps to it; repeating one letter cycles through its options.
    function typeahead(char) {
      const now = Date.now();
      typed = (now - typedAt > 700 ? "" : typed) + char.toLocaleLowerCase();
      typedAt = now;
      const names = labels();
      const order = options.map((_, n) => (active + 1 + n) % options.length);
      let match = order.find((i) => enabled(i) && names[i].startsWith(typed));
      if (match === undefined && Array.from(typed).every((c) => c === typed[0])) {
        match = order.find((i) => enabled(i) && names[i].startsWith(typed[0]));
      }
      if (match !== undefined) setActive(match);
    }

    button.addEventListener("keydown", function (event) {
      const key = event.key;
      const printable = key.length === 1 && key !== " " && !event.altKey && !event.ctrlKey && !event.metaKey;
      const typing = typed !== "" && Date.now() - typedAt <= 700;
      const last = options.length - 1;
      let handled = true;
      if (!open) {
        if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") openList();
        else if (key === "Home") openList("first");
        else if (key === "End") openList("last");
        else if (printable) {
          openList();
          typeahead(key);
        } else handled = false;
      } else if (key === "ArrowDown" && !event.altKey) setActive(step(active, 1));
      else if (key === "ArrowUp" && event.altKey) {
        choose(active);
        closeList();
      } else if (key === "ArrowUp") setActive(step(active, -1));
      else if (key === "Home") setActive(step(-1, 1));
      else if (key === "End") setActive(step(options.length, -1));
      else if (key === "PageDown") setActive(near(Math.min(last, active + 10), -1));
      else if (key === "PageUp") setActive(near(Math.max(0, active - 10), 1));
      else if (key === "Enter" || (key === " " && !typing)) {
        choose(active);
        closeList();
      } else if (key === " ") typeahead(key);
      else if (key === "Escape") closeList();
      else if (key === "Tab") {
        if (viaKeys) choose(active);
        closeList();
        handled = false; // focus moves on as usual
      } else if (printable) typeahead(key);
      else handled = false;
      if (handled) {
        viaKeys = true;
        event.preventDefault();
        event.stopPropagation();
      }
    });

    // Space activates a button on key up; the keydown above already handled it.
    button.addEventListener("keyup", function (event) {
      if (event.key === " ") event.preventDefault();
    });
    button.addEventListener("click", function () {
      if (open) closeList();
      else openList();
    });
    button.addEventListener("blur", closeList);
    // The popup never takes focus: the button keeps it while options are clicked.
    list.addEventListener("pointerdown", (event) => event.preventDefault());
    list.addEventListener("mousedown", (event) => event.preventDefault());
    list.addEventListener("pointermove", function (event) {
      viaKeys = false;
      const option = event.target instanceof Element ? event.target.closest(".option") : null;
      if (option) setActive(options.indexOf(option));
    });
    list.addEventListener("click", function (event) {
      const option = event.target instanceof Element ? event.target.closest(".option") : null;
      if (!option || !enabled(options.indexOf(option))) return;
      choose(options.indexOf(option));
      closeList();
      button.focus();
    });
    label?.addEventListener("click", function (event) {
      event.preventDefault();
      button.focus();
    });
    select.addEventListener("change", sync);
    sync();
  }

  // A press outside an open popup closes it without changing the value.
  document.addEventListener(
    "pointerdown",
    function (event) {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(".combo, .listbox")) return;
      openCombos.forEach((close) => close());
    },
    true,
  );
  // Popups placed by hand would drift away from their button.
  if (!canAnchor) {
    window.addEventListener("resize", () => openCombos.forEach((close) => close()));
    window.addEventListener(
      "scroll",
      function (event) {
        if (event.target instanceof Element && event.target.closest(".listbox")) return;
        openCombos.forEach((close) => close());
      },
      true,
    );
  }

  const enhanceSelects = (root) => root?.querySelectorAll?.("select.select").forEach(enhanceSelect);
  document.addEventListener("htmx:load", (event) => enhanceSelects(event.detail?.elt));

  // --- first-run tips -----------------------------------------------------------------
  //
  // Four steps on the upload page, each pointing at an element with data-tour="name": a
  // dimmed page with a cutout around it and a card next to it. The card is a modal dialog
  // (WAI-ARIA APG): the rest of the page is inert, Tab stays in the card, Esc skips, Left
  // and Right go back and forth, and focus returns where it was. The cutout and the card
  // follow the target on scroll and resize; a missing or hidden target gets a centered
  // card. Text comes from data-* attributes of #tour and its steps.
  //
  // window.transcribatorTour.start() is called by the app's Help menu and returns whether
  // the tips are showing. In the app (data-remember) the service remembers once they were
  // finished or skipped, since the window keeps no web storage; until then they start by
  // themselves on this page (data-autostart).

  const tour = (function () {
    const GAP = 14; // between the cutout and the card: room for the arrow
    const EDGE = 12; // nearest the card comes to the window edge
    const SWITCH_MS = 140; // the old text fades out before the new one comes in
    const SETTLE_MS = 600; // after a step change the cutout glides; later it just follows

    let root = null;
    let card;
    let spot;
    let parts;
    let steps = [];
    let index = 0;
    let opened = false;
    let returnTo = null;
    let inerted = [];
    let observer = null;
    let frame = 0;
    let settleUntil = 0;
    let switchTimer = 0;
    let closeTimer = 0;
    let announceTimer = 0;

    function find() {
      root = byId("tour");
      if (!root) return false;
      card = root.querySelector(".tour-card");
      spot = root.querySelector(".tour-spot");
      parts = {
        title: byId("tour-title"),
        body: byId("tour-body"),
        count: byId("tour-count"),
        dots: Array.from(root.querySelectorAll(".tour-dots > span")),
        skip: root.querySelector('[data-tour-action="skip"]'),
        back: root.querySelector('[data-tour-action="back"]'),
        next: root.querySelector('[data-tour-action="next"]'),
      };
      steps = Array.from(root.querySelectorAll(".tour-steps > li"));
      return steps.length > 0;
    }

    const hook = (name) => (name ? document.querySelector('[data-tour="' + name + '"]') : null);
    const shown = (el) => Boolean(el) && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";

    // The area to light up: the target with its padding (and a second element on the same
    // step), cut to data-cap pixels for a tall one. Null when the target isn't on screen.
    function area(step) {
      const target = hook(step.dataset.target);
      if (!shown(target)) return null;
      const boxes = [target, hook(step.dataset.also)].filter(shown).map((el) => el.getBoundingClientRect());
      let top = Math.min(...boxes.map((b) => b.top));
      let bottom = Math.max(...boxes.map((b) => b.bottom));
      const left = Math.min(...boxes.map((b) => b.left));
      const right = Math.max(...boxes.map((b) => b.right));
      // A long list is lit only at the top, ending at the edge of a row.
      const cap = Number(step.dataset.cap) || 0;
      if (cap && bottom > top + cap) {
        const edges = Array.from(target.querySelectorAll("[data-id]"), (row) => row.getBoundingClientRect().bottom);
        const fitting = edges.filter((edge) => edge <= top + cap);
        bottom = fitting.length ? Math.max(...fitting) : top + cap;
      }
      const inNav = Boolean(target.closest(".nav"));
      const pad = inNav ? 5 : 8;
      const radius = Math.min(24, Math.max(10, parseFloat(getComputedStyle(target).borderTopLeftRadius) || 0) + pad);
      top -= pad;
      bottom += pad;
      return { top: top, bottom: bottom, left: left - pad, right: right + pad, radius: radius, inNav: inNav };
    }

    const clamp = (value, low, high) => Math.min(Math.max(value, low), Math.max(low, high));

    // Where the card goes: the step's preferred side if it fits, then any side that fits,
    // otherwise over the page at the bottom of the window.
    function place(r, w, h, vw, vh, preferred) {
      const cx = (r.left + r.right) / 2;
      const cy = (r.top + r.bottom) / 2;
      const x = clamp(cx - w / 2, EDGE, vw - EDGE - w);
      const y = clamp(cy - h / 2, EDGE, vh - EDGE - h);
      const sides = {
        bottom: () => r.bottom + GAP + h <= vh - EDGE && { x: x, y: r.bottom + GAP },
        top: () => r.top - GAP - h >= EDGE && { x: x, y: r.top - GAP - h },
        right: () => r.right + GAP + w <= vw - EDGE && { x: r.right + GAP, y: y },
        left: () => r.left - GAP - w >= EDGE && { x: r.left - GAP - w, y: y },
      };
      const order = [preferred, "bottom", "top", "right", "left"].filter((side, i, all) => sides[side] && all.indexOf(side) === i);
      for (const side of order) {
        const spotAt = sides[side]();
        if (spotAt) {
          const vertical = side === "bottom" || side === "top";
          const arrow = vertical ? clamp(cx - spotAt.x, 22, w - 22) : clamp(cy - spotAt.y, 22, h - 22);
          return { side: side, x: spotAt.x, y: spotAt.y, arrow: arrow };
        }
      }
      return { side: "over", x: x, y: vh - EDGE - h, arrow: 0 };
    }

    function setSpot(left, top, width, height, radius) {
      spot.style.transform = "translate(" + Math.round(left) + "px, " + Math.round(top) + "px)";
      spot.style.width = Math.round(width) + "px";
      spot.style.height = Math.round(height) + "px";
      spot.style.borderRadius = radius + "px";
    }

    function layout() {
      const step = steps[index];
      const vw = document.documentElement.clientWidth;
      const vh = window.innerHeight;
      const w = card.offsetWidth;
      const h = card.offsetHeight;
      const r = area(step);
      root.classList.toggle("is-centered", !r);
      if (!r) {
        setSpot(vw / 2, vh / 2, 0, 0, 12);
        card.removeAttribute("data-side");
        card.style.transform = "translate(" + Math.round((vw - w) / 2) + "px, " + Math.round((vh - h) / 2) + "px)";
        return;
      }
      // A target partly off screen is lit only where it is visible.
      const lit = {
        top: Math.max(r.top, 2),
        bottom: Math.min(r.bottom, vh - 2),
        left: Math.max(r.left, 2),
        right: Math.min(r.right, vw - 2),
      };
      setSpot(lit.left, lit.top, lit.right - lit.left, Math.max(0, lit.bottom - lit.top), r.radius);
      const at = place(lit, w, h, vw, vh, step.dataset.place);
      card.dataset.side = at.side;
      card.style.setProperty("--tour-arrow", Math.round(at.arrow) + "px");
      card.style.transform = "translate(" + Math.round(at.x) + "px, " + Math.round(at.y) + "px)";
    }

    // Scrolls the target, with room for the card, into the middle of the visible part of
    // the page. Nothing moves when it's already in view with room beside it.
    function reveal(step, smooth) {
      const r = area(step);
      if (!r || r.inNav) return;
      const vh = window.innerHeight;
      const vw = document.documentElement.clientWidth;
      const top = (document.querySelector(".nav")?.getBoundingClientRect().bottom || 0) + EDGE;
      const bottom = vh - EDGE;
      const h = card.offsetHeight;
      const inView = r.top >= top && r.bottom <= bottom;
      if (inView && place(r, card.offsetWidth, h, vw, vh, step.dataset.place).side !== "over") return;
      const block = r.bottom - r.top + GAP + h;
      const want = block <= bottom - top ? top + (bottom - top - block) / 2 : top;
      window.scrollBy({ top: r.top - want, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
    }

    // Scrolling and resizing re-place everything once per frame. Right after a step change
    // the cutout glides to its target; after that it sticks to it without a transition.
    function relayout() {
      frame = 0;
      if (!opened) return;
      const instant = performance.now() > settleUntil;
      if (instant) root.classList.add("is-tracking");
      layout();
      if (instant) {
        void root.offsetWidth; // commit the new place before transitions return
        root.classList.remove("is-tracking");
      }
    }

    // A hidden page gets no animation frames, so it is laid out at once.
    function follow() {
      if (frame || !opened) return;
      if (document.hidden) relayout();
      else frame = window.requestAnimationFrame(relayout);
    }

    function watch(step) {
      observer?.disconnect();
      if (typeof ResizeObserver !== "function") return;
      observer = new ResizeObserver(follow);
      [hook(step.dataset.target), hook(step.dataset.also), card].forEach((el) => el && observer.observe(el));
    }

    function fill(i) {
      const step = steps[i];
      const last = i === steps.length - 1;
      const count = (root.dataset.count || "").split("{n}").join(String(i + 1)).split("{m}").join(String(steps.length));
      parts.title.textContent = step.dataset.title || "";
      parts.body.textContent = step.dataset.body || "";
      parts.count.textContent = count;
      parts.dots.forEach(function (dot, n) {
        dot.hidden = n >= steps.length;
        dot.classList.toggle("is-current", n === i);
        dot.classList.toggle("is-past", n < i);
      });
      parts.skip.hidden = last;
      parts.back.hidden = i === 0;
      parts.next.textContent = last ? root.dataset.done : root.dataset.next;
      // Cleared first, so the same text would be announced again.
      const live = byId("tour-live");
      if (live) live.textContent = "";
      window.clearTimeout(announceTimer);
      announceTimer = window.setTimeout(function () {
        if (opened && live) live.textContent = count + ": " + parts.title.textContent;
      }, 60);
    }

    function show(i, animate) {
      index = i;
      window.clearTimeout(switchTimer);
      const apply = function () {
        card.classList.remove("is-changing");
        fill(i);
        watch(steps[i]);
        settleUntil = performance.now() + SETTLE_MS;
        reveal(steps[i], animate);
        layout();
        // The primary button takes focus unless another button of the card has it.
        const focused = document.activeElement;
        if (!card.contains(focused) || focused === card || focused.hidden) parts.next.focus({ preventScroll: true });
      };
      if (!animate || reducedMotion()) apply();
      else {
        card.classList.add("is-changing");
        switchTimer = window.setTimeout(apply, SWITCH_MS);
      }
    }

    function go(delta) {
      if (!opened) return;
      const i = index + delta;
      if (i >= steps.length) finish();
      else if (i >= 0) show(i, true);
    }

    // In the app the service remembers that the tips were seen (POST /tour/seen).
    function remember() {
      if (!("remember" in root.dataset)) return;
      delete root.dataset.autostart;
      fetch("/tour/seen", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken() },
        cache: "no-store",
        credentials: "same-origin",
        redirect: "error",
      }).catch(function () {});
    }

    function onKey(event) {
      if (!opened) return;
      const key = event.key;
      let handled = true;
      if (key === "Escape") finish();
      else if (key === "ArrowRight") {
        if (index < steps.length - 1) go(1);
      } else if (key === "ArrowLeft") go(-1);
      else if (key === "Tab") {
        const buttons = Array.from(card.querySelectorAll("button")).filter((b) => !b.hidden && !b.disabled);
        const at = buttons.indexOf(document.activeElement);
        const to = event.shiftKey ? (at <= 0 ? buttons.length - 1 : at - 1) : at === buttons.length - 1 ? 0 : at + 1;
        buttons[to]?.focus();
      } else if (key !== "/") handled = false; // "/" (search) waits until the tips close
      if (handled) {
        event.preventDefault();
        event.stopPropagation();
      }
    }

    function onClick(event) {
      const action = event.target instanceof Element ? event.target.closest("[data-tour-action]") : null;
      if (!action) return;
      if (action.dataset.tourAction === "skip") finish();
      else if (action.dataset.tourAction === "back") go(-1);
      else go(1);
    }

    function start() {
      if (!root && !find()) return false;
      if (opened) return true;
      if (document.querySelector("dialog[open]")) return false;
      window.clearTimeout(closeTimer);
      openCombos.forEach((close) => close());
      opened = true;
      returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      // Everything but the tips is inert: no focus, no clicks, hidden from screen readers.
      inerted = Array.from(document.body.children).filter(
        (el) => el !== root && el.id !== "tour-live" && el.tagName !== "SCRIPT" && !el.inert,
      );
      inerted.forEach((el) => (el.inert = true));
      root.classList.remove("is-closing");
      root.classList.add("is-tracking"); // the first place is taken without gliding to it
      root.hidden = false;
      root.addEventListener("click", onClick);
      window.addEventListener("keydown", onKey, true);
      window.addEventListener("resize", follow);
      window.addEventListener("scroll", follow, true);
      try {
        show(0, false);
      } catch (error) {
        finish();
        return false;
      }
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => root.classList.remove("is-tracking")));
      return true;
    }

    // Done, Skip and Esc all end the tips the same way: they count as seen.
    function finish() {
      if (!opened) return;
      opened = false;
      window.clearTimeout(switchTimer);
      window.clearTimeout(announceTimer);
      window.cancelAnimationFrame(frame);
      frame = 0;
      observer?.disconnect();
      root.removeEventListener("click", onClick);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", follow);
      window.removeEventListener("scroll", follow, true);
      inerted.forEach((el) => (el.inert = false));
      inerted = [];
      const live = byId("tour-live");
      if (live) live.textContent = "";
      remember();
      const hide = function () {
        root.hidden = true;
        root.classList.remove("is-closing");
        card.classList.remove("is-changing");
      };
      if (reducedMotion()) hide();
      else {
        root.classList.add("is-closing");
        closeTimer = window.setTimeout(hide, 180);
      }
      if (returnTo && returnTo.isConnected && returnTo !== document.body) returnTo.focus({ preventScroll: true });
      returnTo = null;
    }

    // In the app the tips start by themselves once the page has settled, while the window
    // is visible and no dialog is open.
    function autostart() {
      if (!find() || !("autostart" in root.dataset)) return;
      const attempt = function () {
        if (document.hidden) {
          document.addEventListener("visibilitychange", attempt, { once: true });
          return;
        }
        if ("autostart" in root.dataset && !document.querySelector("dialog[open]")) start();
      };
      window.setTimeout(attempt, reducedMotion() ? 0 : 450);
    }

    return {
      start: start,
      next: () => go(1),
      back: () => go(-1),
      skip: finish,
      isOpen: () => opened,
      autostart: autostart,
    };
  })();

  window.transcribatorTour = {
    start: tour.start,
    next: tour.next,
    back: tour.back,
    skip: tour.skip,
    isOpen: tour.isOpen,
  };

  // --- search -------------------------------------------------------------------------

  // Enter in search refreshes results through htmx, without reloading the page.
  document.addEventListener("submit", function (event) {
    if (event.target.id !== "search" || !window.htmx) return;
    event.preventDefault();
    window.htmx.trigger(byId("q"), "search");
  });

  // Once everything has loaded, transitions turn on and the title fits its final width.
  window.addEventListener("load", function () {
    document.querySelectorAll("textarea.inline").forEach(fit);
    window.requestAnimationFrame(() => document.documentElement.classList.remove("is-loading"));
    tour.autostart();
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea.inline").forEach(fit);
    enhanceSelects(document);
    // Arrived through the "Search" link or the "/" key from another page.
    if (window.location.hash === "#search") {
      byId("q")?.focus();
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  });
})();
