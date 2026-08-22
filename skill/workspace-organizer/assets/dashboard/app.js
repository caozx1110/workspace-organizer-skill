/* workspace-organizer:dashboard-asset schema=1 asset=app */
(function () {
  "use strict";

  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-view-target]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-view-panel]"));
  var filters = Array.prototype.slice.call(document.querySelectorAll("[data-priority-filter]"));
  var taskSheets = Array.prototype.slice.call(document.querySelectorAll("[data-task-sheet]"));

  function selectView(viewName, focusPanel) {
    tabs.forEach(function (tab) {
      var selected = tab.getAttribute("data-view-target") === viewName;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("tabindex", selected ? "0" : "-1");
    });
    panels.forEach(function (panel) {
      var selected = panel.getAttribute("data-view-panel") === viewName;
      panel.hidden = !selected;
      if (selected && focusPanel) {
        panel.focus();
      }
    });
  }

  function selectPriority(priority) {
    filters.forEach(function (filter) {
      filter.setAttribute(
        "aria-pressed",
        filter.getAttribute("data-priority-filter") === priority ? "true" : "false"
      );
    });
    taskSheets.forEach(function (sheet) {
      sheet.hidden = priority !== "all" && sheet.getAttribute("data-priority") !== priority;
    });
  }

  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () {
      selectView(tab.getAttribute("data-view-target"), false);
    });
    tab.addEventListener("keydown", function (event) {
      var nextIndex = index;
      if (event.key === "ArrowRight") {
        nextIndex = (index + 1) % tabs.length;
      } else if (event.key === "ArrowLeft") {
        nextIndex = (index - 1 + tabs.length) % tabs.length;
      } else {
        return;
      }
      event.preventDefault();
      tabs[nextIndex].focus();
      selectView(tabs[nextIndex].getAttribute("data-view-target"), false);
    });
  });

  filters.forEach(function (filter) {
    filter.addEventListener("click", function () {
      selectPriority(filter.getAttribute("data-priority-filter"));
    });
  });
}());
