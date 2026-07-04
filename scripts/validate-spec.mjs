// pnpm test:spec — validates spec/record.example.json against spec/record.schema.json (ajv).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";
import addFormats from "ajv-formats";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const schema = JSON.parse(readFileSync(join(root, "spec/record.schema.json"), "utf8"));
const example = JSON.parse(readFileSync(join(root, "spec/record.example.json"), "utf8"));

const ajv = new Ajv({ allErrors: true, strict: true });
addFormats(ajv);

const validate = ajv.compile(schema);
if (!validate(example)) {
  console.error("spec/record.example.json does NOT validate against spec/record.schema.json:");
  for (const err of validate.errors) {
    console.error(`  ${err.instancePath || "(root)"} ${err.message}`);
  }
  process.exit(1);
}
console.log("OK: spec/record.example.json validates against spec/record.schema.json");
