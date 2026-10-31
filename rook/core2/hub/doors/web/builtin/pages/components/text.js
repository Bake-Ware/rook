// A note on a layout page: plain words, blank lines split paragraphs.
rook.component(function (el, props) {
  el.innerHTML = String(props.text || "").split(/\n\s*\n/)
    .map(function (p) { return "<p>" + rook.esc(p.trim()).replace(/\n/g, "<br>") + "</p>"; }).join("");
});
