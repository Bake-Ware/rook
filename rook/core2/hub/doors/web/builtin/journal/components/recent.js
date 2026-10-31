// The latest journal entries on a layout page.
rook.component(async function (el, props) {
  var args = { limit: props.limit || 10 };
  if (props.cap) args.cap = props.cap;
  if (props.worker) args.worker = props.worker;
  var entries = (await rook.call("journal.search", args)).entries;
  el.innerHTML = entries.length ? '<div class="scroll"><table>' + entries.map(function (e) {
    var who = [e.agent, e.person].filter(Boolean).join(" for ");
    return '<tr><td class="muted">' + rook.ago(e.ts) + "</td><td>" + rook.esc(e.cap) + "</td><td>" +
      rook.esc(who) + "</td><td>" + (e.ok === false ? '<span class="bad">failed</span>' : "") + "</td></tr>";
  }).join("") + "</table></div>" : '<p class="muted">No calls yet.</p>';
});
