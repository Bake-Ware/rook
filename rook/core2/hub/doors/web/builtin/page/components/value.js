// Any cap's result on a layout page: one value big, a list as a table, a
// map as rows. The page must be allowed the cap (its caps).
rook.component(function (el, props) {
  function pick(value, path) {
    String(path || "").split(".").filter(Boolean).forEach(function (k) {
      value = value == null ? undefined : value[k];
    });
    return value;
  }
  function cell(v) {
    if (v == null) return "";
    return rook.esc(typeof v === "object" ? JSON.stringify(v) : v);
  }
  function table(rows) {
    var cols = [];
    rows.slice(0, 20).forEach(function (r) {
      Object.keys(r || {}).forEach(function (k) { if (cols.indexOf(k) < 0 && cols.length < 6) cols.push(k); });
    });
    return '<div class="scroll"><table><tr>' + cols.map(function (c) { return "<th>" + rook.esc(c) + "</th>"; }).join("") +
      "</tr>" + rows.map(function (r) {
        return "<tr>" + cols.map(function (c) { return "<td>" + cell(r && r[c]) + "</td>"; }).join("") + "</tr>";
      }).join("") + "</table></div>";
  }
  function show(v) {
    if (Array.isArray(v)) {
      if (!v.length) return '<p class="muted">Nothing.</p>';
      if (typeof v[0] !== "object") return "<p>" + v.map(cell).join(", ") + "</p>";
      return table(v);
    }
    if (v && typeof v === "object") {
      return table(Object.keys(v).map(function (k) { return { name: k, value: v[k] }; }));
    }
    return '<div class="big">' + cell(v) + "</div>";
  }
  async function load() {
    try {
      var r = await rook.call(props.cap, props.args || {}, { worker: props.worker || "" });
      el.innerHTML = show(pick(r, props.field));
    } catch (x) {
      el.innerHTML = '<p class="bad">' + rook.esc(x.message) + "</p>";
    }
  }
  load();
  if (props.every) setInterval(load, Math.max(5, props.every) * 1000);
});
