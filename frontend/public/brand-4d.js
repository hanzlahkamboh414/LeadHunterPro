/* Adapted from the supplied 4D redesign. Adds one decorative cube to the
   existing hero scene without changing app state, navigation, or controls. */
(() => {
  const face = () => "<i></i><i></i><i></i><i></i><i></i><i></i>";
  function enhance() {
    document.documentElement.classList.add("lh4d-refined");
    document.querySelectorAll(".lh-scene:not([data-lh4d-refined])").forEach((scene) => {
      scene.setAttribute("data-lh4d-refined", "true");
      const cube = document.createElement("div");
      cube.className = "lh4d-tess";
      cube.setAttribute("aria-hidden", "true");
      cube.innerHTML = `<div class="lh4d-cube lh4d-cube-outer">${face()}</div><div class="lh4d-cube lh4d-cube-inner">${face()}</div>`;
      const core = scene.querySelector(".lh-scene-core");
      scene.insertBefore(cube, core);
    });
  }
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; enhance(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", enhance, { once: true });
  else enhance();
})();
