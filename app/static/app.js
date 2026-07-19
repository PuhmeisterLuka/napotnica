(function () {
  "use strict";

  var strip = document.getElementById("status-strip");

  function serverSaysRunning() {
    if (!strip) return false;
    return strip.dataset.scrapeStatus === "running" || strip.dataset.scoreRunning === "1";
  }

  // A just-clicked Scrape/Score flashes "... started."; start polling even if the
  // background row has not been committed yet, to avoid a start-of-run race.
  function justStarted() {
    var flashes = document.querySelectorAll(".flash");
    for (var i = 0; i < flashes.length; i++) {
      if (/started/i.test(flashes[i].textContent)) return true;
    }
    return false;
  }

  var sawRunning = serverSaysRunning();

  function tick() {
    Promise.all([
      fetch("/api/scrape/status").then(function (r) { return r.json(); }),
      fetch("/api/score/status").then(function (r) { return r.json(); }),
    ]).then(function (res) {
      var running = res[0].status === "running" || !!res[1].running;
      if (running) {
        sawRunning = true;
        setTimeout(tick, 2000);
      } else if (sawRunning) {
        window.location.reload();
      }
      // else: nothing was ever running, stop quietly.
    }).catch(function () {
      setTimeout(tick, 2000);
    });
  }

  if (serverSaysRunning() || justStarted()) {
    setTimeout(tick, 1500);
  }

  // Submit the filter form when a select or checkbox changes.
  var form = document.querySelector("form.filters");
  if (form) {
    form.querySelectorAll("select, input[type=checkbox]").forEach(function (el) {
      el.addEventListener("change", function () { form.submit(); });
    });
  }
})();
