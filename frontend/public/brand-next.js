(() => {
  const markUrl = "/lead-hunter-orbit.svg";
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  function scene() {
    const root = document.createElement("div");
    root.className = "lh-scene";
    root.setAttribute("aria-hidden", "true");
    root.innerHTML = `<div class="lh-scene-grid"></div><div class="lh-scene-halo"></div><div class="lh-orbit lh-orbit-one"></div><div class="lh-orbit lh-orbit-two"></div><div class="lh-orbit lh-orbit-three"></div><div class="lh-scene-core"><div class="lh-core-back"></div><img src="${markUrl}" alt=""></div><div class="lh-node lh-node-a"></div><div class="lh-node lh-node-b"></div><div class="lh-scene-caption"><i></i><span>INTELLIGENCE ENGINE</span><strong>ACTIVE SIGNAL</strong></div>`;
    return root;
  }
  function apply() {
    document.documentElement.classList.add("lh-next");
    document.querySelectorAll(".brand-mark img, .auth-page > div > div:first-child img").forEach((img) => {
      if (img.getAttribute("src") !== markUrl) img.setAttribute("src", markUrl);
    });
    const hero = document.querySelector(".dashboard-hero");
    if (hero && !hero.querySelector(".lh-scene")) hero.append(scene());
    const auth = document.querySelector(".auth-page");
    if (auth && !auth.querySelector(".lh-scene")) auth.append(scene());
  }
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; apply(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply, { once: true });
  else apply();
  if (!reducedMotion.matches) document.addEventListener("pointermove", (event) => {
    const hero = document.querySelector(".dashboard-hero");
    if (!hero || !hero.contains(event.target)) return;
    const box = hero.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width - .5) * 2;
    const y = ((event.clientY - box.top) / box.height - .5) * 2;
    hero.style.setProperty("--lh-pointer-x", `${x * 7}deg`);
    hero.style.setProperty("--lh-pointer-y", `${y * -7}deg`);
  }, { passive: true });
})();
