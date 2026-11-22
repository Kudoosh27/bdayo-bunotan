(() => {
  const menuButton = document.querySelector(".admin-nav-toggle");
  const sidebar = document.getElementById("admin-sidebar");
  const closeButton = document.querySelector(".admin-nav-close");
  const overlay = document.querySelector(".admin-nav-overlay");
  if (!menuButton || !sidebar || !closeButton || !overlay) return;

  const mobile = window.matchMedia("(max-width: 760px)");
  function setOpen(open) {
    const isOpen = mobile.matches && open;
    menuButton.setAttribute("aria-expanded", String(isOpen));
    sidebar.classList.toggle("is-open", isOpen);
    overlay.hidden = !isOpen;
    document.body.classList.toggle("menu-open", isOpen);
    if (isOpen) closeButton.focus();
    else if (document.activeElement === closeButton) menuButton.focus();
  }

  menuButton.addEventListener("click", () => setOpen(true));
  closeButton.addEventListener("click", () => setOpen(false));
  overlay.addEventListener("click", () => setOpen(false));
  sidebar.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      if (mobile.matches) setOpen(false);
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menuButton.getAttribute("aria-expanded") === "true") {
      setOpen(false);
    }
    if (event.key !== "Tab" || !mobile.matches || !sidebar.classList.contains("is-open")) {
      return;
    }
    const focusable = [...sidebar.querySelectorAll("a, button")].filter(
      (element) => !element.disabled,
    );
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  mobile.addEventListener("change", () => setOpen(false));
})();
