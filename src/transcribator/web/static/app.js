// Behavior on top of htmx: drag-and-drop upload, queue and status polling, inline edits,
// the delete dialog and the "/" key. Handlers are delegated to the document and keep
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

  function line(text, isError) {
    const p = document.createElement("p");
    p.className = isError ? "msg msg-error" : "msg";
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

  // Finishing an edit: Enter and leaving the form save changes, Esc cancels them.
  // An empty or invalid value is reverted on leaving. Without changes the speaker form
  // collapses back into a chip.
  function finish(el, how) {
    const form = el.form;
    if (how === "cancel") revert(form, false);
    if (how === "blur") revert(form, true);
    if (how !== "cancel" && dirty(form) && form.checkValidity()) save(form);
    else if (el.classList.contains("inline-speaker")) toggleSpeaker(el, false, how !== "blur");
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
    if (isInline(el) && !(event.relatedTarget && el.form.contains(event.relatedTarget))) finish(el, "blur");
  });

  // After an error (an empty name, say) the form can be submitted again.
  document.addEventListener("htmx:afterRequest", function (event) {
    const form = event.detail.elt;
    if (form instanceof HTMLFormElement && event.detail.failed) delete form.dataset.saving;
  });

  // --- clicks: speakers and the delete dialog -----------------------------------------

  document.addEventListener("click", function (event) {
    const target = event.target instanceof Element ? event.target : null;
    const chip = target?.closest(".speaker-chip");
    const opener = target?.closest("[data-dialog]");
    if (chip) {
      toggleSpeaker(chip, true, false);
    } else if (opener) {
      byId(opener.dataset.dialog)?.showModal();
    } else if (target?.closest("dialog [data-close]")) {
      target.closest("dialog").close();
    } else if (target instanceof HTMLDialogElement && target.open) {
      // A click on the backdrop, outside the dialog box, closes the dialog.
      const box = target.getBoundingClientRect();
      const inside =
        event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) target.close();
    }
  });

  // The close event doesn't bubble, so it's caught in the capture phase.
  document.addEventListener("close", (event) => event.target.querySelector?.("#delete-msg")?.replaceChildren(), true);

  // --- search -------------------------------------------------------------------------

  // Enter in search refreshes results through htmx, without reloading the page.
  document.addEventListener("submit", function (event) {
    if (event.target.id !== "search" || !window.htmx) return;
    event.preventDefault();
    window.htmx.trigger(byId("q"), "search");
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea.inline").forEach(fit);
    // Arrived through the "Search" link or the "/" key from another page.
    if (window.location.hash === "#search") {
      byId("q")?.focus();
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  });
})();
