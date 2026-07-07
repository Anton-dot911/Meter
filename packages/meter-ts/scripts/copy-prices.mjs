// Bundle the shared price table next to the built module. The package ships
// only dist/ (see package.json "files"), so a git-dependency install has no
// access to the repo-root spec/prices.json; copying it into dist/ lets
// pricing.ts resolve it relative to the compiled module at runtime.
//
// Run from the build/prepare script after tsc has created dist/.
import { copyFileSync } from "node:fs";

const src = new URL("../../../spec/prices.json", import.meta.url);
const dest = new URL("../dist/prices.json", import.meta.url);
copyFileSync(src, dest);
