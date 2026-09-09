# Rook background study

Procedural Three.js rook with toon lighting, real self-shadowing, irregular
black structural wire edges, and drafting guides. Edge jitter is generated once
in object space, so the drawing turns with the mesh without temporal noise. No model or raster assets. The mesh and artwork
are decorative and never intercept dashboard input.

Build the checked-in browser bundle:

```sh
cd rook/web/scene
npm ci
npm run build
```

The browser runs at at most 20 fps and device pixel ratio 1.25. Hidden tabs stop
rendering; reduced-motion and small-screen defaults render a still. The user can
pause/resume with the sidebar control. WebGL failure/context loss uses the inline
SVG sketch. Page teardown releases geometry, materials, renderer, and listeners.
Save-data connections retain the SVG without downloading Three.js.
