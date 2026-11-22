(() => {
  const minor = document.querySelector("#is_minor");
  const guardianConsent = document.querySelector("[data-guardian-consent]");
  if (!minor || !guardianConsent) return;

  const syncGuardianConsent = () => {
    guardianConsent.required = minor.checked;
    if (!minor.checked) guardianConsent.checked = false;
  };

  minor.addEventListener("change", syncGuardianConsent);
  syncGuardianConsent();
})();
