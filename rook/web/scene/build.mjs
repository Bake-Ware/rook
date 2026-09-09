import {build} from 'esbuild';
import {appendFile, readFile} from 'node:fs/promises';

await build({entryPoints:['rook-scene.js'],bundle:true,minify:true,format:'esm',target:'es2020',legalComments:'linked',outfile:'../rook-scene.js'});
await appendFile('../rook-scene.js.LEGAL.txt', '\n' + await readFile('node_modules/three/LICENSE','utf8'));
