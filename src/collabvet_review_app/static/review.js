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
