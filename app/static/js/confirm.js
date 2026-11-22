(() => {
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog) return;

  const message = dialog.querySelector("[data-confirm-message]");
  const cancelButton = dialog.querySelector("[data-confirm-cancel]");
  const acceptButton = dialog.querySelector("[data-confirm-accept]");
  const approvedForms = new WeakSet();
  let pendingSubmission = null;

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (approvedForms.has(form)) {
      approvedForms.delete(form);
      return;
    }

    const submitter = event.submitter;
    const confirmation = submitter?.dataset.confirm || form.dataset.confirm;
    if (!confirmation) return;
    if (typeof dialog.showModal !== "function") {
      if (!window.confirm(confirmation)) event.preventDefault();
      return;
    }

    event.preventDefault();
    pendingSubmission = { form, submitter };
    message.textContent = confirmation;
    dialog.showModal();
    cancelButton.focus();
  });

  cancelButton.addEventListener("click", () => {
    pendingSubmission = null;
    dialog.close();
  });

  acceptButton.addEventListener("click", () => {
    if (!pendingSubmission) return;
    const { form, submitter } = pendingSubmission;
    pendingSubmission = null;
    dialog.close();
    approvedForms.add(form);
    if (submitter) form.requestSubmit(submitter);
    else form.requestSubmit();
  });

  dialog.addEventListener("close", () => {
    if (pendingSubmission?.submitter) pendingSubmission.submitter.focus();
    pendingSubmission = null;
  });
})();
