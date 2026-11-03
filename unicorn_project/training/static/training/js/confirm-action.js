/**
 * Shared confirmation modal for destructive actions.
 *
 * Usage:
 *   await window.confirmAction("Delete this item?");
 *   await window.confirmAction("Delete?", { title, confirmLabel, cancelLabel, danger });
 *
 * Declarative markup:
 *   <form data-confirm="Delete this?" ...>
 *   <button type="submit" data-confirm="Delete this?">...</button>
 *   <a href="..." data-confirm="Delete this?">...</a>
 *   <form class="js-delete-form" ...>  (default delete message)
 */
(function () {
  var DEFAULT_DELETE_MSG = "Are you sure you want to delete this? This cannot be undone.";
  var modalEl = null;
  var msgEl = null;
  var titleEl = null;
  var okBtn = null;
  var cancelBtn = null;
  var bsModal = null;
  var pendingResolve = null;

  function ensureModal() {
    if (modalEl && document.body.contains(modalEl)) return;

    modalEl = document.getElementById("confirmActionModal");
    if (!modalEl) {
      modalEl = document.createElement("div");
      modalEl.id = "confirmActionModal";
      modalEl.className = "modal fade";
      modalEl.tabIndex = -1;
      modalEl.setAttribute("aria-hidden", "true");
      modalEl.innerHTML =
        '<div class="modal-dialog modal-dialog-centered">' +
        '<div class="modal-content">' +
        '<div class="modal-header">' +
        '<h5 class="modal-title" id="confirmActionModalTitle">Please confirm</h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
        "</div>" +
        '<div class="modal-body">' +
        '<p class="mb-0" id="confirmActionModalMessage"></p>' +
        "</div>" +
        '<div class="modal-footer">' +
        '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal" id="confirmActionModalCancel">Cancel</button>' +
        '<button type="button" class="btn btn-danger" id="confirmActionModalOk">Delete</button>' +
        "</div>" +
        "</div>" +
        "</div>";
      document.body.appendChild(modalEl);
    }

    msgEl = document.getElementById("confirmActionModalMessage");
    titleEl = document.getElementById("confirmActionModalTitle");
    okBtn = document.getElementById("confirmActionModalOk");
    cancelBtn = document.getElementById("confirmActionModalCancel");

    if (typeof bootstrap !== "undefined") {
      bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    }

    okBtn.addEventListener("click", function () {
      finish(true);
    });

    modalEl.addEventListener("hidden.bs.modal", function () {
      if (pendingResolve) finish(false);
    });
  }

  function finish(ok) {
    var resolve = pendingResolve;
    pendingResolve = null;
    if (resolve) resolve(!!ok);
    if (bsModal) {
      bsModal.hide();
    } else if (modalEl) {
      modalEl.classList.remove("show");
      modalEl.style.display = "none";
    }
  }

  window.confirmAction = function (message, options) {
    options = options || {};
    ensureModal();

    if (titleEl) titleEl.textContent = options.title || "Please confirm";
    if (msgEl) msgEl.textContent = message || DEFAULT_DELETE_MSG;
    if (okBtn) {
      okBtn.textContent = options.confirmLabel || "Delete";
      okBtn.className = "btn " + (options.danger === false ? "btn-primary" : "btn-danger");
    }
    if (cancelBtn) cancelBtn.textContent = options.cancelLabel || "Cancel";

    return new Promise(function (resolve) {
      pendingResolve = resolve;
      if (bsModal) {
        bsModal.show();
      } else if (window.confirm(message || DEFAULT_DELETE_MSG)) {
        resolve(true);
      } else {
        resolve(false);
      }
    });
  };

  function checkedDeleteCount(form) {
    return form.querySelectorAll('input[type="checkbox"][name$="-DELETE"]:checked').length;
  }

  function resolveSubmitMessage(form, submitter) {
    if (submitter && submitter.getAttribute("data-confirm")) {
      return submitter.getAttribute("data-confirm");
    }
    if (form.getAttribute("data-confirm")) {
      return form.getAttribute("data-confirm");
    }
    if (form.getAttribute("data-confirm-delete")) {
      return form.getAttribute("data-confirm-delete");
    }
    if (form.classList.contains("js-delete-form")) {
      return form.getAttribute("data-confirm-message") || "Delete this item? This cannot be undone.";
    }

    if (submitter && submitter.getAttribute("data-confirm-dynamic-delete") === "1") {
      var selected = form.querySelectorAll('input[name="ids"]:checked').length;
      if (!selected) {
        window.alert("Select at least one item to delete.");
        return "__abort__";
      }
      return (
        "Delete " +
        selected +
        " selected item" +
        (selected === 1 ? "" : "s") +
        "? This cannot be undone."
      );
    }

    // Formset rows marked for deletion on save
    var deleteCount = checkedDeleteCount(form);
    if (deleteCount > 0) {
      return (
        "Delete " +
        deleteCount +
        " selected item" +
        (deleteCount === 1 ? "" : "s") +
        "? This cannot be undone."
      );
    }

    return null;
  }

  function resumeFormSubmit(form, submitter) {
    form.dataset.confirmSkip = "1";
    try {
      if (submitter && typeof form.requestSubmit === "function") {
        form.requestSubmit(submitter);
      } else if (submitter && submitter.name) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = submitter.name;
        hidden.value = submitter.value || "1";
        form.appendChild(hidden);
        if (submitter.getAttribute("formaction")) {
          form.setAttribute("action", submitter.getAttribute("formaction"));
        }
        if (submitter.getAttribute("formmethod")) {
          form.setAttribute("method", submitter.getAttribute("formmethod"));
        }
        HTMLFormElement.prototype.submit.call(form);
      } else {
        HTMLFormElement.prototype.submit.call(form);
      }
    } finally {
      window.setTimeout(function () {
        delete form.dataset.confirmSkip;
      }, 0);
    }
  }

  document.addEventListener(
    "submit",
    function (e) {
      var form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.confirmSkip === "1") return;

      var submitter = e.submitter || null;
      var message = resolveSubmitMessage(form, submitter);
      if (!message) return;
      if (message === "__abort__") {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }

      e.preventDefault();
      e.stopImmediatePropagation();

      var confirmLabel =
        (submitter && submitter.getAttribute("data-confirm-label")) ||
        form.getAttribute("data-confirm-label") ||
        "Delete";
      var danger =
        !((submitter && submitter.getAttribute("data-confirm-danger") === "0") ||
          form.getAttribute("data-confirm-danger") === "0");

      window.confirmAction(message, { confirmLabel: confirmLabel, danger: danger }).then(function (ok) {
        if (ok) resumeFormSubmit(form, submitter);
      });
    },
    true
  );

  document.addEventListener(
    "click",
    function (e) {
      var el = e.target.closest("[data-confirm]");
      if (!el) return;

      // data-confirm on a <form> is handled by the submit listener. Stopping the
      // click here (the button, or an icon inside it) cancels the submit, so the
      // dialog can appear and then do nothing.
      if (el.tagName === "FORM") return;

      // Booking accident-report buttons confirm and submit in their own handler.
      if (el.classList.contains("js-accident-report-bulk")) return;

      // Submit controls are handled by the submit interceptor
      if (el.matches('button[type="submit"], input[type="submit"]')) return;
      if (el.tagName === "BUTTON" && !el.getAttribute("type") && el.closest("form")) return;

      var message = el.getAttribute("data-confirm");
      if (!message) return;

      e.preventDefault();
      e.stopImmediatePropagation();

      var opts = {
        confirmLabel: el.getAttribute("data-confirm-label") || "Delete",
        danger: el.getAttribute("data-confirm-danger") !== "0",
        title: el.getAttribute("data-confirm-title") || "Please confirm",
      };

      window.confirmAction(message, opts).then(function (ok) {
        if (!ok) return;
        if (el.tagName === "A" && el.href) {
          window.location.href = el.href;
          return;
        }
        if (typeof el.onclick === "function") {
          el.onclick();
        }
        el.dispatchEvent(new CustomEvent("confirm-accepted", { bubbles: true }));
      });
    },
    true
  );
})();
