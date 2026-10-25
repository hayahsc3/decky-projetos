// Gera SaveBackupSD.zip pronto para "Instalar plugin de um ZIP" do Decky.
// Estrutura: SaveBackupSD/{plugin.json,package.json,main.py,LICENSE,README.md,dist/index.js}
import AdmZip from "adm-zip";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const folder = "SaveBackupSD";
const files = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md"];

if (!existsSync(join(root, "dist", "index.js"))) {
  console.error("dist/index.js não existe. Rode `npm run build` antes.");
  process.exit(1);
}

const zip = new AdmZip();
for (const f of files) {
  if (existsSync(join(root, f))) zip.addLocalFile(join(root, f), folder);
}
zip.addLocalFile(join(root, "dist", "index.js"), `${folder}/dist`);

const out = join(root, `${folder}.zip`);
zip.writeZip(out);
console.log(`ZIP criado: ${out}`);
