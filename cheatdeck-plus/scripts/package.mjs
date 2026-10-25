// Empacota o plugin em out/CheatDeck-Plus.zip (sem depender de Python).
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const name = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8")).name;
const folder = name.replace(/ /g, "-");
const required = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js"];
const optional = ["dist/index.js.map"];

for (const f of required) {
  if (!existsSync(join(root, f))) {
    console.error(`Arquivo ausente: ${f} (rode 'pnpm build' antes)`);
    process.exit(1);
  }
}

const out = join(root, "out");
const stage = join(out, folder);
const zip = join(out, `${folder}.zip`);
rmSync(out, { recursive: true, force: true });
mkdirSync(stage, { recursive: true });
for (const f of [...required, ...optional.filter((o) => existsSync(join(root, o)))]) {
  mkdirSync(dirname(join(stage, f)), { recursive: true });
  cpSync(join(root, f), join(stage, f));
}

let r;
if (process.platform === "win32") {
  // tar.exe (Windows 10+) grava caminhos com "/", ao contrário do Compress-Archive.
  r = spawnSync("tar", ["-a", "-c", "-f", zip, folder], { cwd: out, stdio: "inherit" });
} else {
  r = spawnSync("zip", ["-qr", zip, folder], { cwd: out, stdio: "inherit" });
}
if (r.status !== 0) {
  console.error("Falha ao criar o zip.");
  process.exit(1);
}
rmSync(stage, { recursive: true, force: true });
console.log(`Gerado: ${zip}`);
