# Rook background study

Procedural Three.js rook with faceted shading, object-space cross-hatching,
ink edges, and drafting guides. No model or raster assets. The mesh and artwork
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
