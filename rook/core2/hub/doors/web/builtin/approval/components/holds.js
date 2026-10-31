// What waits on a person's answer, with the way to Approvals.
rook.component(function (el) {
  async function load() {
    var holds = (await rook.call("approval.list")).holds;
    el.innerHTML = (holds.length ? "<table>" + holds.map(function (h) {
      return "<tr><td>" + rook.esc(h.kind === "ask" ? h.question : h.cap) + '</td><td class="muted">' + rook.esc(h.why || "") + "</td></tr>";
    }).join("") + "</table>" : '<p class="muted">Nothing is waiting for you.</p>') +
      '<p><button class="go">Open Approvals</button></p>';
    el.querySelector("button").onclick = function () { rook.open("approval/approvals"); };
  }
  load().catch(function (x) { el.innerHTML = '<p class="bad">' + rook.esc(x.message) + "</p>"; });
  setInterval(function () { load().catch(function () {}); }, 30000);
});
