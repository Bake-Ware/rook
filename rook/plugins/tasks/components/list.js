// Tasks on a layout page, filtered by project, state or assignee.
rook.component(async function (el, props) {
  var where = {};
  ["project", "state", "assignee"].forEach(function (k) { if (props[k]) where[k] = props[k]; });
  var open = ["todo", "in_progress", "blocked", "paused"];
  var tasks = (await rook.call("task.list", { where: where, limit: 200 })).filter(function (t) {
    return props.state || open.indexOf(t.state) >= 0;
  }).slice(0, props.limit || 10);
  el.innerHTML = tasks.length ? "<table>" + tasks.map(function (t) {
    return "<tr><td>" + rook.esc(t.title) + '</td><td class="muted">' + rook.esc(t.state) + "</td></tr>";
  }).join("") + "</table>" : '<p class="muted">No open tasks.</p>';
});
