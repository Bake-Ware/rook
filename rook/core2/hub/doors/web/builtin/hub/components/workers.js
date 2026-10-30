// The band's workers on a layout page.
rook.component(function (el, props) {
  async function load() {
    var r = await rook.call("hub.workers");
    var only = props.only || [];
    var rows = r.workers.filter(function (w) { return !only.length || only.indexOf(w.name) >= 0; });
    el.innerHTML = rows.length ? "<table>" + rows.map(function (w) {
      var state = w.online ? '<span class="ok">online</span>' : '<span class="bad">offline</span>';
      return '<tr class="click"><td>' + rook.esc(w.name) + "</td><td>" + state + '</td><td class="muted">' +
        (w.hub ? "hub" : rook.ago(w.last_seen)) + "</td></tr>";
    }).join("") + "</table>" : '<p class="muted">No workers.</p>';
    el.onclick = function () { rook.open("hub/workers"); };
  }
  load().catch(function (x) { el.innerHTML = '<p class="bad">' + rook.esc(x.message) + "</p>"; });
  setInterval(function () { load().catch(function () {}); }, 30000);
});
