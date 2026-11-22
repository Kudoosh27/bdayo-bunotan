(() => {
  const root = document.documentElement;
  const button = document.querySelector(".theme-toggle");
  if (!button) return;
  const saved = localStorage.getItem("bunotan-theme");
  const initial = saved === "dark" ? "dark" : "light";
  root.dataset.theme = initial;
  button.setAttribute("aria-pressed", String(initial === "dark"));
  button.addEventListener("click", () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    button.setAttribute("aria-pressed", String(next === "dark"));
    localStorage.setItem("bunotan-theme", next);
    document.dispatchEvent(new CustomEvent("bunotan:theme-change", { detail: { theme: next } }));
  });
})();
