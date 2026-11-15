// The margin drawing behind every page, as on the 0.4 dashboard: the SVG
// sketch at once, the three.js rook (rook-scene.js, from rook/web/scene) once
// the browser is idle. Bake, Nov 9: "I liked the 3d rook from the old version".
// "Pause artwork" in the sidebar stops it (rook-scene.js keeps the choice).
const host = document.getElementById("rook-art"), toggle = document.getElementById("art-toggle");
const slow = navigator.connection && navigator.connection.saveData;
function load() {
  import("/web/static/rook-scene.js").then(({ mountRook }) => mountRook(host, toggle)).catch(() => {});
}
if (host && toggle && !slow) {
  if ("requestIdleCallback" in window) requestIdleCallback(load, { timeout: 1800 });
  else setTimeout(load, 300);
}
