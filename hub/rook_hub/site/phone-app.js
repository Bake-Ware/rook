/* The phone app's QR code, download button and install line (brief AP1), drawn into a container:
   on the public page /app, in Settings > Phone app, and in the dock's work pane ([[open: app]]).
   What it shows comes from GET /app as JSON: whether the hub has the APK, and, for a person signed in,
   where it goes.
   The QR code is drawn here, from this page's own address, with no outside service. */
import qrcodegen from '/site/vendor/qrcodegen.js';

const SVG = 'http://www.w3.org/2000/svg';

/* An SVG of `text` as a QR code: dark modules on a light square with the standard quiet zone. */
export function qrSvg(text) {
  const code = qrcodegen.QrCode.encodeText(text, qrcodegen.QrCode.Ecc.MEDIUM);
  const edge = code.size + 8;
  let path = '';
  for (let y = 0; y < code.size; y++)
    for (let x = 0; x < code.size; x++)
      if (code.getModule(x, y)) path += `M${x + 4} ${y + 4}h1v1h-1z`;
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${edge} ${edge}`);
  svg.setAttribute('shape-rendering', 'crispEdges');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'QR code for ' + text);
  svg.innerHTML = `<rect width="${edge}" height="${edge}" fill="#fff"/><path d="${path}" fill="#000"/>`;
  return svg;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function drawPhoneApp(container) {
  const state = await (await fetch('/app', { headers: { Accept: 'application/json' }, credentials: 'same-origin' })).json();
  const link = location.origin + '/app/rook.apk';
  const box = el('div', 'phone-app');
  if (state.apk) {
    const qr = el('div', 'phone-app-qr');
    qr.append(qrSvg(link));
    const button = el('a', 'btn primary phone-app-get', 'Download the app');
    button.href = '/app/rook.apk';
    button.setAttribute('download', 'rook.apk');
    box.append(qr, button,
      el('p', 'phone-app-note', 'Scan the code with the phone, or open this page on it and tap the button. ' +
        'The first time, Android asks to allow installs from your browser: allow it, then open the file.'),
      el('p', 'phone-app-link mono', link));
  } else {
    // Where the APK goes is told to a person signed in only: it names the server's folders (brief AP6).
    const note = el('p', 'callout phone-app-missing', 'This hub has no app file yet.');
    if (state.file) note.append(' Put the APK at ', el('b', 'mono', state.file), ' and open this page again.');
    else note.append(' Whoever runs this hub can put it on: Settings > Phone app says where.');
    box.append(note);
  }
  container.replaceChildren(box);
  return state;
}
