"use strict";

document.querySelectorAll("[data-document-url]").forEach((button) => {
  button.addEventListener("click", () => {
    const frame = document.getElementById("source-frame");
    if (frame) frame.src = button.dataset.documentUrl;
  });
});

document.querySelectorAll(".review-form").forEach((form) => {
  let dirty = false;
  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
});

document.querySelectorAll("[data-timeline-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    const filter = button.dataset.timelineFilter;
    document.querySelectorAll("[data-timeline-filter]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    document.querySelectorAll(".timeline-event").forEach((event) => {
      const status = event.dataset.reviewStatus;
      event.hidden =
        filter !== "all" &&
        (filter === "pending" ? status === "approved" : status !== filter);
    });
  });
});

const headerMenus = [...document.querySelectorAll(".topbar details")];
headerMenus.forEach((menu) => {
  menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    headerMenus.forEach((other) => {
      if (other !== menu) other.open = false;
    });
  });
});
document.addEventListener("click", (event) => {
  if (event.target.closest(".topbar details")) return;
  headerMenus.forEach((menu) => { menu.open = false; });
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  headerMenus.forEach((menu) => { menu.open = false; });
});
